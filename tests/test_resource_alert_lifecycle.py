from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from cloud_monitor.config import validate_config
from cloud_monitor.engine import Engine
from cloud_monitor.models import Metric
from cloud_monitor.service import Service
from cloud_monitor.store import STATES, Store


NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
JOB = {"id": "certificates", "account": "test", "kind": "certificates", "interval_seconds": 60}
RULE = {"id": "expiry", "metric": "expiry_days", "levels": [
    {"severity": "P1", "op": "le", "threshold": 30},
]}


def config(job=None, rule=None):
    return validate_config({
        "version": 1,
        "accounts": [{"id": "test", "provider": "aliyun", "enabled": True}],
        "jobs": [deepcopy(job or JOB)],
        "rules": [deepcopy(rule or RULE)],
        "service": {"recovery_samples": 1},
    })


def sample(resource="cert-a", value=5, seconds=0):
    return Metric("aliyun", "test", resource, resource, "expiry_days", value, "days",
                  NOW + timedelta(seconds=seconds), {"kind": "certificates", "domain": "api.example.com"})


def disabled_job(**values):
    return {**JOB, "alerting": {"default_enabled": False}, **values}


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "state.db")
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.mark.parametrize("batch", [False, True])
def test_selected_resource_alerts_and_other_resources_still_collect(store, batch):
    job = {**JOB, "kind": "rds_performance" if batch else "certificates", "alerting": {
        "default_enabled": False,
        "resources": [{"resource_id": "cert-a", "enabled": True}],
    }}
    cfg = config(job)

    class Collector:
        def collect(self, *_):
            return [sample("cert-a"), sample("cert-b")]

    service = Service(cfg, store, collector_factory=lambda _: Collector(), notifier=Mock())
    assert service.collect(job, NOW)
    assert {row["payload"]["resource_id"] for row in store.latest_samples([job["id"]])} == {
        "cert-a", "cert-b"
    }
    assert store.status()["jobs"][0]["sample_count"] == 2
    assert [json.loads(row["payload"])["resource_id"] for row in store.list_messages()] == ["cert-a"]


@pytest.mark.parametrize("delivery", ["pending", "failed", "sent"])
def test_restart_silently_suppresses_existing_alert_and_cancels_retries(store, delivery):
    Engine(store, config()).evaluate(JOB, sample(), NOW)
    original = store.list_messages()[0]
    if delivery == "failed":
        store.delivery_failed(original["id"], NOW, 1, "test_failure")
    elif delivery == "sent":
        store.delivered(original["id"])
    store.mark_collection_failure(JOB["id"], NOW)
    notifier = Mock()
    service = Service(config(disabled_job()), store, notifier=notifier)
    assert not store.status()["active_alerts"]
    assert store.last_collection_failure(JOB["id"]) == NOW.timestamp()
    assert [(row["id"], row["status"]) for row in store.list_messages()] == [
        (original["id"], "sent" if delivery == "sent" else "cancelled")
    ]
    assert store.retry_failed(NOW) == 0
    service.dispatch(NOW)
    notifier.send.assert_not_called()


def test_pending_recovery_is_cancelled_when_resource_is_disabled(store):
    engine = Engine(store, config())
    engine.evaluate(JOB, sample(), NOW)
    store.delivered(store.list_messages()[0]["id"])
    engine.evaluate(JOB, sample(value=365, seconds=60), NOW + timedelta(seconds=60))
    assert json.loads(store.list_messages()[-1]["payload"])["phase"] == "recovery"
    Service(config(disabled_job()), store, notifier=Mock())
    assert [row["status"] for row in store.list_messages()] == ["sent", "cancelled"]


def test_disabling_resource_clears_candidate_samples_before_reenabling(store):
    rule = deepcopy(RULE)
    rule["levels"][0]["for_samples"] = 2
    cfg = config(rule=rule)
    Engine(store, cfg).evaluate(JOB, sample(), NOW)
    assert not store.list_messages()
    Service(config(disabled_job(), rule), store, notifier=Mock())
    with store.connect() as connection:
        assert not connection.execute(select(STATES)).first()
    restarted = Service(cfg, store, notifier=Mock())
    restarted.engine.evaluate(JOB, sample(seconds=60), NOW + timedelta(seconds=60))
    assert not store.list_messages()
    restarted.engine.evaluate(JOB, sample(seconds=120), NOW + timedelta(seconds=120))
    assert len(store.list_messages()) == 1


@pytest.mark.parametrize("batch", [False, True])
def test_evaluation_clears_previously_active_resource_when_excluded(store, batch):
    engine = Engine(store, config())
    engine.evaluate(JOB, sample(), NOW)
    if batch:
        engine.evaluate_batch(disabled_job(), [sample(seconds=60)], NOW + timedelta(seconds=60))
    else:
        engine.evaluate(disabled_job(), sample(seconds=60), NOW + timedelta(seconds=60))
    assert not store.status()["active_alerts"]
    assert [row["status"] for row in store.list_messages()] == ["cancelled"]


def test_resource_suppression_is_scoped_to_job_and_preserves_unselected_policy(store):
    other_job = {**JOB, "id": "other-certificates"}
    cfg = config()
    cfg["jobs"].append(other_job)
    engine = Engine(store, cfg)
    engine.evaluate(JOB, sample(), NOW)
    engine.evaluate(other_job, sample(), NOW)
    cfg["jobs"][0] = disabled_job()
    Service(cfg, store, notifier=Mock())
    assert [state["job_id"] for state in store.status()["active_alerts"]] == [other_job["id"]]
    assert sorted(row["status"] for row in store.list_messages()) == ["cancelled", "pending"]


def test_collection_failure_still_alerts_and_recovers_when_all_resources_are_disabled(store):
    job = disabled_job()

    class Collector:
        def collect(self, kind, options, now):
            if now == NOW:
                raise RuntimeError("test_failure")
            return [replace(sample(), observed_at=now)]

    service = Service(config(job), store, collector_factory=lambda _: Collector(), notifier=Mock())
    assert not service.collect(job, NOW)
    assert [state["rule_id"] for state in store.status()["active_alerts"]] == ["collection-error"]
    store.delivered(store.list_messages()[0]["id"])
    service = Service(config(job), store, collector_factory=lambda _: Collector(), notifier=Mock())
    assert [state["rule_id"] for state in store.status()["active_alerts"]] == ["collection-error"]
    assert service.collect(job, NOW + timedelta(seconds=60))
    assert not store.status()["active_alerts"]
    assert [(json.loads(row["payload"])["metric"], json.loads(row["payload"])["phase"])
            for row in store.list_messages()] == [("collection_failed", "firing"),
                                                  ("collection_failed", "recovery")]


@pytest.mark.parametrize("metric_name", ["api_query_success", "api_query_duration_seconds",
                                        "api_last_success_timestamp_seconds", "api_signature_failed"])
def test_job_query_health_is_independent_of_resource_selection(store, metric_name):
    rule = {**RULE, "metric": metric_name}
    job = disabled_job()
    cfg = config(job, rule)
    metric = replace(sample(), resource_id=job["id"], metric=metric_name,
                     dimensions={"source": "local_probe", "product": "api", "scope": "certificates"})
    Engine(store, cfg).evaluate(job, metric, NOW)
    Service(cfg, store, notifier=Mock())
    assert len(store.status()["active_alerts"]) == 1
    assert store.list_messages()[0]["status"] == "pending"


@pytest.mark.parametrize("enabled", [False, True])
def test_legacy_certificate_domain_policy_waits_for_complete_names_before_dispatch(store, enabled):
    Engine(store, config()).evaluate(JOB, sample(), NOW)
    job = {**JOB, "alerting": {
        "default_enabled": not enabled,
        "resources": [{"domain": "api.example.com" if enabled else "san.example.com", "enabled": enabled}],
    }}
    cfg = config(job)
    notifier = Mock()
    service = Service(cfg, store, notifier=notifier)
    assert not store.status()["active_alerts"]
    with store.connect() as connection:
        assert not connection.execute(select(STATES)).first()
    assert [row["status"] for row in store.list_messages()] == ["cancelled"]
    service.dispatch(NOW)
    notifier.send.assert_not_called()
    complete = replace(sample(seconds=60), dimensions={
        "kind": "certificates", "domain": "api.example.com", "domains": ["api.example.com", "san.example.com"],
    })
    service.engine.evaluate(job, complete, NOW + timedelta(seconds=60))
    Service(cfg, store, notifier=notifier)
    assert len(store.status()["active_alerts"]) == int(enabled)
    assert [row["status"] for row in store.list_messages()] == ["cancelled"] + (["pending"] if enabled else [])


@pytest.mark.parametrize("metric_name", ["collection_failed", "api_query_success", "api_query_duration_seconds",
                                        "api_last_success_timestamp_seconds", "api_signature_failed"])
def test_legacy_certificate_domain_reconciliation_preserves_job_health(store, metric_name):
    rule = {**RULE, "metric": metric_name}
    metric = replace(sample(), resource_id=JOB["id"], metric=metric_name, dimensions={})
    Engine(store, config(rule=rule)).evaluate(JOB, metric, NOW)
    job = {**JOB, "alerting": {"resources": [{"domain": "san.example.com", "enabled": False}]}}
    Service(config(job, rule), store, notifier=Mock())
    assert len(store.status()["active_alerts"]) == 1
    assert [row["status"] for row in store.list_messages()] == ["pending"]


@pytest.mark.parametrize("complete,kind,selector", [
    (True, "certificates", {"domain": "api.example.com", "enabled": True}),
    (False, "certificates", {"resource_id": "cert-a", "enabled": True}),
    (False, "domains", {"domain": "api.example.com", "enabled": True}),
])
def test_certificate_metadata_upgrade_preserves_states_outside_legacy_domain_scope(store, complete, kind, selector):
    original_job = {**JOB, "kind": kind}
    metric = sample()
    if complete:
        metric = replace(metric, dimensions={**metric.dimensions, "domains": ["api.example.com"]})
    Engine(store, config(original_job)).evaluate(original_job, metric, NOW)
    job = {**original_job, "alerting": {"default_enabled": False, "resources": [selector]}}
    Service(config(job), store, notifier=Mock())
    assert len(store.status()["active_alerts"]) == 1
    assert [row["status"] for row in store.list_messages()] == ["pending"]
