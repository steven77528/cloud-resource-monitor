"""Prometheus exposition of persisted samples; scraping never calls cloud APIs."""

from collections import Counter
from datetime import datetime

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily

from .models import Metric, utcnow
from .resource_selection import network_estimate_key, network_resource_selected


RESOURCE_LABELS = (
    "provider", "account", "job_id", "resource_id", "resource_name", "metric", "unit",
    "region", "product", "engine", "namespace", "metric_name", "scope", "dimension_id", "metric_key",
    "source", "currency", "billing_period", "spu_id", "package_id", "cost_time_type",
)
JOB_LABELS = ("provider", "account", "job_id", "kind")
ALERT_LABELS = (
    "provider", "account", "job_id", "resource_id", "resource_name", "metric", "rule_id", "severity", "metric_key",
)
JOB_STATUSES = ("ok", "no_data", "insufficient_data", "degraded", "error", "stale", "waiting")
OUTBOX_STATUSES = ("pending", "failed", "sent", "cancelled", "skipped")


def _gauge(name, description, labels=()):
    return GaugeMetricFamily("cloud_monitor_" + name, description, labels=list(labels))


def _resource_labels(row):
    metric = row["payload"]
    dimensions = metric.get("dimensions", {})
    # Only allow known label keys. Raw API payloads and arbitrary dimensions stay in MySQL.
    values = {key: dimensions.get(key, "") for key in RESOURCE_LABELS}
    values.update({key: metric.get(key, "") for key in (
        "provider", "account", "resource_id", "resource_name", "metric", "unit",
    )})
    values.update(job_id=row["job_id"], metric_key=row["metric_key"])
    return [str(values[key]) if values[key] is not None else "" for key in RESOURCE_LABELS]


class PersistedCollector:
    def __init__(self, service, now=None):
        self.service = service
        self.now = now

    def collect(self):
        service = self.service
        now = self.now or utcnow()
        jobs = {job["id"]: job for job in service.jobs}
        state = service.store.status()
        job_states = {row["id"]: row for row in state["jobs"]}

        alive = _gauge("scheduler_alive", "Whether the collection scheduler heartbeat is current.")
        alive.add_metric([], int(service.started and (now - service.last_tick).total_seconds() < 120))
        yield alive

        status = _gauge("job_status", "One-hot collection status for each enabled job.", (*JOB_LABELS, "status"))
        last_run = _gauge("job_last_run_timestamp_seconds", "Last collection attempt, as Unix seconds.", JOB_LABELS)
        interval = _gauge("job_interval_seconds", "Configured collection interval in seconds.", JOB_LABELS)
        samples = _gauge("job_samples", "Samples returned by the last collection attempt.", JOB_LABELS)
        effective_job_states = {}
        for job_id, job in jobs.items():
            labels = [service.accounts[job["account"]]["provider"], job["account"], job_id, job["kind"]]
            row = job_states.get(job_id)
            current = row["status"] if row else "waiting"
            if row and now.timestamp() - row["updated_at"] > job["interval_seconds"] * 2.5 + 60:
                current = "stale"
            effective_job_states[job_id] = current
            for value in JOB_STATUSES:
                status.add_metric([*labels, value], int(current == value))
            interval.add_metric(labels, job["interval_seconds"])
            if row:
                last_run.add_metric(labels, row["updated_at"])
                samples.add_metric(labels, row["sample_count"])
        yield from (status, last_run, interval, samples)

        estimate_available = _gauge(
            "network_estimate_available",
            "Whether a network estimate has valid paired samples after its latest negative difference.",
            ("provider", "account", "job_id", "resource_id", "region", "reason"),
        )
        unavailable_estimates = set()
        estimate_invalid_observed = {}
        for job_id, quality in state.get("estimate_quality", {}).items():
            job = jobs.get(job_id)
            if (not job or job["kind"] != "network_traffic"
                    or job.get("options", {}).get("scope") != "intranet_estimated"):
                continue
            provider = service.accounts[job["account"]]["provider"]
            for resource in quality.values():
                resource_id, region = resource["resource_id"], resource["region"]
                metric = {"account": job["account"], "resource_id": resource_id, "metric": "network_in_bps",
                          "dimensions": {"region": region, "scope": "intranet_estimated"}}
                if not network_resource_selected(job, metric):
                    continue
                identity = (job_id, network_estimate_key(resource_id, region))
                if resource.get("in_scope", True) is False:
                    unavailable_estimates.add(identity)
                    continue
                estimate_invalid_observed[identity] = resource["invalid_observed_at"]
                available = resource.get("valid_observed_at", 0) > resource["invalid_observed_at"]
                if not available:
                    unavailable_estimates.add(identity)
                reason = "negative_difference" if resource["invalid_observed_at"] else ""
                estimate_available.add_metric(
                    [provider, job["account"], job_id, resource_id, region, reason], int(available),
                )
        yield estimate_available

        value = _gauge("resource_value", "Latest fresh collected value in the source unit; select metric and unit.",
                       RESOURCE_LABELS)
        observed = _gauge("resource_observed_timestamp_seconds", "Source observation time, as Unix seconds.",
                          RESOURCE_LABELS)
        freshness = _gauge("resource_fresh", "Whether the persisted sample is within its job freshness limit.",
                           RESOURCE_LABELS)
        for row in service.store.latest_samples(list(jobs)):
            job = jobs[row["job_id"]]
            metric = row["payload"]
            account = service.accounts[job["account"]]
            if metric["account"] != job["account"] or metric["provider"] != account["provider"]:
                continue
            if not network_resource_selected(job, metric):
                continue
            age = now.timestamp() - row["observed_at"]
            default_age = (300 if job["kind"] in {"rds_performance", "rds_cloudmonitor"}
                           else max(900, job["interval_seconds"] * 2))
            fresh = -60 <= age <= job.get("max_age_seconds", default_age)
            collection_status = job_states.get(job["id"], {}).get("status")
            fresh = fresh and collection_status not in {"no_data", "insufficient_data"}
            if job["kind"] == "sms_statistics":
                fresh = fresh and collection_status == "ok"
            if job["kind"] == "network_traffic" and job.get("options", {}).get("scope") == "intranet_estimated":
                identity = (job["id"], network_estimate_key(
                    metric["resource_id"], metric.get("dimensions", {}).get("region")))
                fresh = (fresh and effective_job_states[job["id"]] in {"ok", "degraded"}
                         and identity not in unavailable_estimates
                         and row["observed_at"] > estimate_invalid_observed.get(identity, float("-inf")))
            labels = _resource_labels(row)
            observed.add_metric(labels, row["observed_at"])
            freshness.add_metric(labels, int(fresh))
            if fresh or metric["metric"] == "api_last_success_timestamp_seconds":
                value.add_metric(labels, metric["value"])
        yield from (value, observed, freshness)

        alerts = _gauge("active_alert", "An unresolved alert from the existing alert engine.", ALERT_LABELS)
        counts = Counter()
        for alert in state["active_alerts"]:
            if alert.get("job_id") not in jobs or alert.get("active") not in {"P0", "P1", "P2"}:
                continue
            metric = alert["metric"]
            job = jobs[alert["job_id"]]
            provider = service.accounts[job["account"]]["provider"]
            if metric["account"] != job["account"] or metric["provider"] != provider:
                continue
            if not network_resource_selected(job, metric):
                continue
            severity = alert["active"]
            # Same identity algorithm as storage, including node-specific identity dimensions.
            identity = Metric(**{**metric, "observed_at": datetime.fromisoformat(metric["observed_at"])}).key()
            labels = [provider, job["account"], job["id"], metric["resource_id"], metric["resource_name"],
                      metric["metric"], alert["rule_id"], severity, identity]
            alerts.add_metric(labels, 1)
            counts[(provider, job["account"], severity)] += 1
        yield alerts
        totals = _gauge("alerts", "Number of unresolved alerts for enabled jobs by account and severity.",
                        ("provider", "account", "severity"))
        for account in service.accounts.values():
            if account.get("enabled", False):
                for severity in ("P0", "P1", "P2"):
                    labels = (account["provider"], account["id"], severity)
                    totals.add_metric(labels, counts[labels])
        yield totals

        outbox = _gauge("outbox_messages", "Persisted notification queue rows by status, across all accounts.",
                        ("status",))
        for status_name in OUTBOX_STATUSES:
            outbox.add_metric([status_name], state["outbox"].get(status_name, 0))
        yield outbox


def render_metrics(service, now=None):
    registry = CollectorRegistry(auto_describe=False)
    registry.register(PersistedCollector(service, now))
    return generate_latest(registry)
