import hashlib
import operator
import uuid

from .alerting import resource_alerts_enabled
from .config import ROUTES
from .notifiers import sms_enabled


COMPARATORS = {"lt": operator.lt, "le": operator.le, "gt": operator.gt,
               "ge": operator.ge, "eq": operator.eq}


def rule_matches(rule, metric):
    if rule["metric"] != metric.metric:
        return False
    fields = {**metric.dimensions, "provider": metric.provider, "account": metric.account,
              "resource_id": metric.resource_id}
    return all(fields.get(key) == value for key, value in rule.get("match", {}).items())


class Engine:
    def __init__(self, store, config):
        self.store = store
        self.config = config

    def evaluate(self, job, metric, now, rules=None):
        if metric.observed_at.timestamp() > now.timestamp() + 60:
            raise ValueError("Metric timestamp is in the future")
        if (now - metric.observed_at).total_seconds() > job.get(
            "max_age_seconds", max(900, job["interval_seconds"] * 2)
        ):
            raise ValueError("Metric is stale")
        if not resource_alerts_enabled(job, metric):
            self._suppress_metrics(job, [metric])
            return
        selected = job.get("rules")
        for rule in rules if rules is not None else self.config["rules"]:
            if selected is not None and rules is None and rule["id"] not in selected:
                continue
            if rule_matches(rule, metric):
                with self.store.delivery_lock:
                    self._evaluate_rule(job, metric, rule, now)

    def _evaluate_rule(self, job, metric, rule, now):
        self._evaluate_samples(job, [metric], rule, now)

    def evaluate_batch(self, job, metrics, now):
        """Replay overlapping history, then notify only the final state of each series."""
        groups = {}
        selected = job.get("rules")
        rules = [rule for rule in self.config["rules"]
                 if selected is None or rule["id"] in selected]
        suppressed = []
        for metric in metrics:
            if metric.observed_at.timestamp() > now.timestamp() + 60:
                raise ValueError("Metric timestamp is in the future")
            if not resource_alerts_enabled(job, metric):
                suppressed.append(metric)
                continue
            for rule in rules:
                if rule_matches(rule, metric):
                    groups.setdefault((rule["id"], metric.key()), (rule, []))[1].append(metric)
        prepared = []
        for rule, samples in groups.values():
            samples.sort(key=lambda item: item.observed_at)
            if (now - samples[-1].observed_at).total_seconds() > job.get("max_age_seconds", 300):
                raise ValueError("Metric is stale")
            prepared.append((rule, samples))
        with self.store.delivery_lock:
            self._suppress_metrics(job, suppressed)
            for rule, samples in prepared:
                self._evaluate_samples(job, samples, rule, now)

    @staticmethod
    def _state_key(job, metric, rule):
        return hashlib.sha256(f"{job['id']}:{rule['id']}:{metric.key()}".encode()).hexdigest()

    def _suppress_metrics(self, job, metrics):
        keys = {self._state_key(job, metric, rule) for metric in metrics
                for rule in self.config["rules"] if rule["metric"] == metric.metric}
        self.store.suppress_alerts(keys)

    def _evaluate_samples(self, job, samples, rule, now):
        metric = samples[-1]
        key = self._state_key(job, metric, rule)
        with self.store.transaction_state(key) as (db, stored_state):
            state = stored_state or {
                "candidates": {}, "active": None, "last_observed": 0, "last_notified": 0,
                "healthy_samples": 0,
            }
            previous = state["active"]
            candidate_samples = state.setdefault("candidate_samples", {})
            failure_at = job.get("_collection_failure_at", 0)
            if (failure_at > state.get("last_failure_seen", 0)
                    and metric.metric not in {"api_query_success", "api_query_duration_seconds",
                                              "api_last_success_timestamp_seconds",
                                              "collection_failed"}):
                state["candidates"] = {}
                candidate_samples.clear()
                state["healthy_samples"] = 0
                state["last_failure_seen"] = failure_at
            processed = False
            matching = []
            for metric in samples:
                observed = metric.observed_at.timestamp()
                if observed <= state["last_observed"]:
                    continue
                processed = True
                max_gap = metric.dimensions.get("sample_max_gap_seconds",
                                               job.get("max_gap_seconds", job["interval_seconds"] * 2.5))
                if observed - state["last_observed"] > max_gap:
                    state["candidates"] = {}
                    candidate_samples.clear()
                    state["healthy_samples"] = 0
                state["last_observed"] = observed
                state["metric"] = metric.to_dict()
                state["rule_id"] = rule["id"]
                state["job_id"] = job["id"]
                eligible = []
                matching = []
                for level in sorted(rule["levels"], key=lambda x: x["severity"]):
                    severity = level["severity"]
                    if COMPARATORS[level["op"]](metric.value, level["threshold"]):
                        matching.append(severity)
                        first = state["candidates"].setdefault(severity, observed)
                        candidate_samples[severity] = candidate_samples.get(severity, 0) + 1
                        if (observed - first >= level.get("for_seconds", 0)
                                and candidate_samples[severity] >= level.get("for_samples", 1)):
                            eligible.append(severity)
                    else:
                        state["candidates"].pop(severity, None)
                        candidate_samples.pop(severity, None)
                active = state["active"]
                current = eligible[0] if eligible else None
                if active in matching and (current is None or active < current):
                    current = active
                if matching:
                    state["healthy_samples"] = 0
                    if current is None:
                        current = active
                else:
                    state["healthy_samples"] += 1
                    if state["healthy_samples"] < self.config["service"]["recovery_samples"]:
                        current = active
                state["active"] = current
            if not processed:
                return
            if current != previous:
                # An obsolete retry must not fire after recovery or severity change.
                self.store.cancel_messages(db, key)
                state["active"] = current
                if current or self.config["service"].get("notify_recovery", True):
                    self._enqueue(db, key, state, metric, rule, current or previous,
                                  "firing" if current else "recovery", now)
            elif current and matching and now.timestamp() - state["last_notified"] >= self.config[
                "service"]["repeat_seconds"][current]:
                if not self.store.has_pending(db, key):
                    self._enqueue(db, key, state, metric, rule, current, "firing", now)
            self.store.save_state(db, key, state)

    def _enqueue(self, db, key, state, metric, rule, severity, phase, now):
        self.store.cancel_messages(db, key, statuses=("failed",))
        event = metric.to_dict()
        event.update({"id": str(uuid.uuid4()), "phase": phase, "severity": severity,
                      "summary": rule.get("summary", rule["id"]), "rule_id": rule["id"],
                      "created_at": now.isoformat()})
        channel = ROUTES[severity]
        skipped = channel == "sms" and not sms_enabled(self.config.get("notifications", {}))
        self.store.enqueue(db, key, channel, event, now, status="skipped" if skipped else "pending")
        state["last_notified"] = now.timestamp()
