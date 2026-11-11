"""Replay sanitized collection logs through the production health alert engine.

This script uses only an in-memory store. It does not load credentials, connect
to a database, start Service, or dispatch notifications.
"""

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import threading
from types import SimpleNamespace

from cloud_monitor.engine import Engine
from cloud_monitor.service import Service


HEALTHY_STATUSES = {"ok", "no_data", "insufficient_data", "degraded"}


class MemoryStore:
    def __init__(self):
        self.delivery_lock = threading.RLock()
        self.states = {}
        self.events = []

    @contextmanager
    def transaction_state(self, key):
        yield self, deepcopy(self.states.get(key))

    def save_state(self, _db, key, state):
        self.states[key] = deepcopy(state)

    def enqueue(self, _db, key, channel, event, now, status="pending"):
        assert channel == "email" and status == "pending"
        self.events.append({"at": now.isoformat(), "job": event["resource_id"],
                            "phase": event["phase"], "incident_key": key})

    def cancel_messages(self, _db, _key, statuses=("pending", "failed")):
        # Assume immediate successful acceptance, as observed attempts=0 implies.
        return None

    def has_pending(self, _db, _key):
        return False


def parse_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Evidence timestamps must have an explicit timezone")
    return parsed


def phase_counts(events):
    counts = Counter(event["phase"] for event in events)
    return {"firing": counts["firing"], "recovery": counts["recovery"], "total": len(events)}


def error_runs(rows):
    sequences = []
    active = []
    for row in rows:
        if row["status"] == "error":
            active.append(row)
        elif active:
            sequences.append(active)
            active = []
    if active:
        sequences.append(active)
    return sequences


def replay(rows, job, failure_samples, settings):
    store = MemoryStore()
    config = {"service": {"recovery_samples": 2, "notify_recovery": True,
                          "repeat_seconds": settings["repeat_seconds"]}, "rules": []}
    service = SimpleNamespace(engine=Engine(store, config))
    account = {"id": "replay-account", "provider": job["id"].split("-", 1)[0]}
    selected = {**job, "collection_error_samples": failure_samples}
    for row in rows:
        status = row["status"]
        if status not in HEALTHY_STATUSES | {"error"}:
            raise ValueError(f"Unsupported captured status: {status}")
        Service._collection_health(service, selected, account, status == "error", parse_time(row["at"]))
    return store.events


def analyse(evidence):
    if not evidence["COMPLETE"].get("read_only"):
        raise ValueError("Expected a completed read-only evidence capture")
    settings = evidence["ALERT_SETTINGS"]
    assert settings["recovery_samples"] == 2 and settings["notify_recovery"] is True
    assert settings["collection_error_samples"] == 1
    rows_by_job = defaultdict(list)
    for row in evidence["LOG_RUNS"]:
        rows_by_job[row["job"]].append(row)
    error_jobs = {row["job"] for row in evidence["LOG_RUNS"] if row["status"] == "error"}
    timeline = evidence["COLLECTION_TIMELINE"]
    results = []
    combined = {value: [] for value in (1, 2, 3)}
    for job_id in sorted(error_jobs):
        rows = sorted(rows_by_job[job_id], key=lambda row: parse_time(row["at"]))
        # These intervals were confirmed from the production JOB_SETTINGS probe.
        if job_id == "huawei-ecs-network":
            interval = 300
        elif job_id.startswith("aliyun-"):
            interval = 60
        else:
            raise ValueError(f"No confirmed production interval for {job_id}")
        job = {"id": job_id, "kind": "network_traffic" if job_id.endswith("network") else "metrics",
               "account": "replay-account", "interval_seconds": interval}
        start, end = parse_time(rows[0]["at"]), parse_time(rows[-1]["at"])
        actual = [event for event in timeline if event["job"] == job_id
                  and start <= parse_time(event["at"]) <= end]
        prior = [event for event in timeline if event["job"] == job_id and parse_time(event["at"]) < start]
        if prior and prior[-1]["phase"] != "recovery":
            raise ValueError(f"{job_id} begins with an active incident; cold-start replay would be misleading")
        sequences = error_runs(rows)
        threshold_results = {}
        for threshold in (1, 2, 3):
            simulated = replay(rows, job, threshold, settings)
            threshold_results[str(threshold)] = phase_counts(simulated)
            combined[threshold].extend(simulated)
            if threshold == 1 and phase_counts(simulated) != phase_counts(actual):
                raise AssertionError(f"Default replay does not match observed email counts for {job_id}: "
                                     f"{phase_counts(simulated)} versus {phase_counts(actual)}")
        longest = max(sequences, key=len)
        results.append({"job": job_id, "interval_seconds": interval, "first": rows[0]["at"],
                        "last": rows[-1]["at"], "runs": len(rows),
                        "statuses": dict(Counter(row["status"] for row in rows)),
                        "error_run_lengths": dict(sorted(Counter(len(run) for run in sequences).items())),
                        "longest_error_run": {"runs": len(longest), "first": longest[0]["at"],
                                              "last": longest[-1]["at"]},
                        "actual_email_counts": phase_counts(actual), "thresholds": threshold_results})
    return {"assumptions": [
        "Production Service._collection_health and Engine execute against an in-memory fake store.",
        "Only jobs with captured errors are replayed; thresholds are compared over each job's retained log window.",
        "All replayed jobs start inactive; prior captured notification, when present, must be recovery.",
        "Collection completion log timestamps approximate health evaluation timestamps.",
        "Every generated event is treated as immediately accepted; captured retry attempts were all zero.",
        "Recovery requires two healthy runs; degraded counts as query-healthy, matching production behavior.",
        "Changing failure confirmation reduces notifications but does not repair stale source data.",
    ], "thresholds_total": {str(value): phase_counts(events) for value, events in combined.items()},
        "jobs": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    evidence = {}
    for line in arguments.evidence.read_text(encoding="utf-8-sig").splitlines():
        kind, value = line.split(" ", 1)
        evidence[kind] = json.loads(value)
    result = analyse(evidence)
    text = json.dumps(result, ensure_ascii=True, indent=2)
    if arguments.output:
        arguments.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
