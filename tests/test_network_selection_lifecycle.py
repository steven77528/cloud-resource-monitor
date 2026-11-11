from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import func, select

from cloud_monitor.alerting import resource_alerts_enabled
from cloud_monitor.config import validate_config
from cloud_monitor.engine import Engine
from cloud_monitor.models import Metric
from cloud_monitor.prometheus import render_metrics
from cloud_monitor.resource_selection import network_resource_selected
from cloud_monitor.service import COLLECTION_RULE, Service
from cloud_monitor.store import SAMPLES, STATES, Store


NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)
JOB = {"id": "network", "account": "main", "kind": "network_traffic", "interval_seconds": 60,
       "options": {"product": "ecs"}}
RULE = {"id": "traffic", "metric": "network_in_bps", "levels": [
    {"severity": "P1", "op": "ge", "threshold": 100},
]}


def selected_job(instances=None):
    return {**JOB, "options": {"product": "ecs", "instances": instances if instances is not None else [
        {"resource_id": "kept", "region": "cn-hangzhou"},
    ]}}


def configuration(job=None, rule=None):
    return validate_config({
        "version": 1,
        "accounts": [{"id": "main", "provider": "aliyun", "enabled": True, "regions": ["cn-hangzhou"]},
                     {"id": "other", "provider": "aliyun", "enabled": True, "regions": ["cn-hangzhou"]}],
        "jobs": [deepcopy(job or JOB)], "rules": [deepcopy(rule or RULE)],
        "service": {"recovery_samples": 1},
    })


def reading(resource="removed", value=200, seconds=0, region="cn-hangzhou", account="main"):
    return Metric("aliyun", account, resource, resource, "network_in_bps", value, "bit/s",
                  NOW + timedelta(seconds=seconds), {"region": region, "product": "ecs", "scope": "intranet"})


def families(service):
    return {family.name: family.samples
            for family in text_string_to_metric_families(render_metrics(service, NOW).decode())}


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "network.db")
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.mark.parametrize("metric,selected", [
    (reading("kept"), True),
    (reading("removed"), False),
    (reading("kept", region="cn-shanghai"), False),
    (reading("kept", account="other"), False),
    (reading("KEPT"), False),
    (replace(reading("kept"), dimensions={}), False),
])
def test_explicit_ecs_selection_matches_account_region_and_id_for_objects_and_payloads(metric, selected):
    assert network_resource_selected(selected_job(), metric) is selected
    assert network_resource_selected(selected_job(), metric.to_dict()) is selected
    assert resource_alerts_enabled(selected_job(), metric) is selected


@pytest.mark.parametrize("job", [
    JOB,
    {**selected_job(), "kind": "metrics"},
    {**JOB, "options": {"product": "rds", "instances": [{
        "resource_id": "kept", "region": "cn-hangzhou", "engine": "MySQL"}]}},
])
def test_selection_preserves_discovery_and_other_job_types(job):
    assert network_resource_selected(job, reading())
    assert resource_alerts_enabled(job, reading())


@pytest.mark.parametrize("metric_name", ["collection_failed", "api_query_success", "api_query_duration_seconds",
                                        "api_last_success_timestamp_seconds", "api_signature_failed"])
def test_empty_ecs_selection_preserves_job_health(metric_name):
    metric = replace(reading(), resource_id=JOB["id"], metric=metric_name, dimensions={})
    job = {**selected_job([]), "alerting": {"default_enabled": False}}
    assert network_resource_selected(job, metric)
    assert resource_alerts_enabled(job, metric)
    assert not network_resource_selected(job, reading())


def test_resource_alert_policy_can_further_restrict_selected_ecs():
    job = {**selected_job(), "alerting": {"resources": [
        {"resource_id": "kept", "enabled": False}, {"resource_id": "removed", "enabled": True},
    ]}}
    assert not resource_alerts_enabled(job, reading("kept"))
    assert not resource_alerts_enabled(job, reading("removed"))


def test_export_filters_all_current_series_and_alerts_before_restart_without_deleting_history(store):
    cfg = configuration()
    service = Service(cfg, store, notifier=Mock())
    points = [reading("kept"), reading(), reading("kept", region="cn-shanghai")]
    for point in points:
        service.engine.evaluate(cfg["jobs"][0], point, NOW)
    store.save_samples(JOB["id"], points)
    store.job_status(JOB["id"], "ok", NOW, len(points))
    cfg["jobs"][0]["options"] = selected_job()["options"]
    exported = families(service)
    for name in ("resource_value", "resource_fresh", "resource_observed_timestamp_seconds", "active_alert"):
        rows = exported["cloud_monitor_" + name]
        assert len(rows) == 1
        assert rows[0].labels["resource_id"] == "kept"
        if name != "active_alert":
            assert rows[0].labels["region"] == "cn-hangzhou"
    assert sum(row.value for row in exported["cloud_monitor_alerts"]) == 1
    assert len(store.latest_samples([JOB["id"]])) == 3
    with store.connect() as connection:
        assert connection.execute(select(func.count()).select_from(SAMPLES)).scalar_one() == 3


@pytest.mark.parametrize("delivery", ["pending", "failed", "sent"])
def test_restart_cancels_removed_ecs_notifications_and_preserves_sent_history(store, delivery):
    Engine(store, configuration()).evaluate(JOB, reading(), NOW)
    original = store.list_messages()[0]
    if delivery == "failed":
        store.delivery_failed(original["id"], NOW, 1, "offline_failure")
    elif delivery == "sent":
        store.delivered(original["id"])
    notifier = Mock()
    service = Service(configuration(selected_job()), store, notifier=notifier)
    assert not store.status()["active_alerts"]
    assert [(row["id"], row["status"]) for row in store.list_messages()] == [
        (original["id"], "sent" if delivery == "sent" else "cancelled"),
    ]
    assert store.retry_failed(NOW) == 0
    service.dispatch(NOW)
    notifier.send.assert_not_called()


def test_restart_preserves_selected_ecs_other_jobs_accounts_and_collection_health(store):
    cfg = configuration()
    other_job = {**deepcopy(JOB), "id": "other-network", "account": "other"}
    rds_job = {**deepcopy(JOB), "id": "rds-network", "options": {"product": "rds"}}
    cfg["jobs"].extend([other_job, rds_job])
    engine = Engine(store, cfg)
    for job, point in [(JOB, reading("kept")), (JOB, reading()),
                       (JOB, reading("kept", region="cn-shanghai")),
                       (JOB, reading("kept", account="other")),
                       (other_job, reading(account="other")), (rds_job, reading())]:
        engine.evaluate(job, point, NOW)
    health = replace(reading(), resource_id=JOB["id"], metric="collection_failed", value=1, dimensions={})
    engine.evaluate(JOB, health, NOW, rules=[COLLECTION_RULE])
    store.mark_collection_failure(JOB["id"], NOW)
    cfg["jobs"][0] = selected_job()
    Service(cfg, store, notifier=Mock())
    active = store.status()["active_alerts"]
    assert {(state["job_id"], state["metric"]["resource_id"]) for state in active} == {
        (JOB["id"], "kept"), (JOB["id"], JOB["id"]), (other_job["id"], "removed"),
        (rds_job["id"], "removed"),
    }
    assert store.last_collection_failure(JOB["id"]) == NOW.timestamp()
    assert sorted(row["status"] for row in store.list_messages()) == ["cancelled"] * 3 + ["pending"] * 4


def test_removed_ecs_pending_recovery_is_cancelled(store):
    engine = Engine(store, configuration())
    engine.evaluate(JOB, reading(), NOW)
    store.delivered(store.list_messages()[0]["id"])
    engine.evaluate(JOB, reading(value=0, seconds=60), NOW + timedelta(seconds=60))
    assert json.loads(store.list_messages()[-1]["payload"])["phase"] == "recovery"
    Service(configuration(selected_job()), store, notifier=Mock())
    assert [row["status"] for row in store.list_messages()] == ["sent", "cancelled"]


def test_removed_ecs_candidates_restart_from_new_samples_when_reselected(store):
    rule = deepcopy(RULE)
    rule["levels"][0]["for_samples"] = 2
    original = configuration(rule=rule)
    Engine(store, original).evaluate(JOB, reading(), NOW)
    assert not store.list_messages()
    Service(configuration(selected_job([]), rule), store, notifier=Mock())
    with store.connect() as connection:
        assert not connection.execute(select(STATES)).first()
    restored_job = selected_job([{"resource_id": "removed", "region": "cn-hangzhou"}])
    service = Service(configuration(restored_job, rule), store, notifier=Mock())
    service.engine.evaluate(restored_job, reading(seconds=60), NOW + timedelta(seconds=60))
    assert not store.list_messages()
    service.engine.evaluate(restored_job, reading(seconds=120), NOW + timedelta(seconds=120))
    assert len(store.list_messages()) == 1


@pytest.mark.parametrize("batch", [False, True])
def test_engine_suppresses_removed_ecs_during_evaluation(store, batch):
    engine = Engine(store, configuration())
    engine.evaluate(JOB, reading(), NOW)
    if batch:
        engine.evaluate_batch(selected_job(), [reading(seconds=60)], NOW + timedelta(seconds=60))
    else:
        engine.evaluate(selected_job(), reading(seconds=60), NOW + timedelta(seconds=60))
    assert not store.status()["active_alerts"]
    assert [row["status"] for row in store.list_messages()] == ["cancelled"]
