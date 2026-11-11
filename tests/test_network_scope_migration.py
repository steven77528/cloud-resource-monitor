"""Offline migration from Huawei total throughput to estimated intranet alerts."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import func, select

from cloud_monitor.config import validate_config
from cloud_monitor.engine import Engine
from cloud_monitor.models import Metric
from cloud_monitor.prometheus import render_metrics
from cloud_monitor.resource_selection import HEALTH_METRICS, network_resource_selected
from cloud_monitor.service import COLLECTION_RULE, Service
from cloud_monitor.store import SAMPLES, STATES, Store


NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


def configuration(product, *, estimated=False, for_samples=1):
    cfg = {
        "version": 1,
        "accounts": [{"id": name, "provider": "huawei", "enabled": True, "regions": ["cn-east-3"]}
                     for name in ("main", "other")],
        "jobs": [{"id": "network", "account": "main", "kind": "network_traffic",
                  "interval_seconds": 300, "options": {"product": product, "period": 300}}],
        "rules": [{"id": "traffic", "metric": "network_in_bps", "match": {"scope": "all"}, "levels": [
            {"severity": "P1", "op": "ge", "threshold": 100, "for_samples": for_samples},
        ]}],
        "service": {"recovery_samples": 1},
    }
    if estimated:
        cfg["jobs"][0]["options"]["scope"] = "intranet_estimated"
        cfg["rules"][0]["match"]["scope"] = "intranet_estimated"
    return validate_config(cfg)


def reading(product, *, scope="all", resource="instance", account="main", value=200, seconds=0):
    return Metric("huawei", account, resource, resource, "network_in_bps", value, "bit/s",
                  NOW + timedelta(seconds=seconds), {"region": "cn-east-3", "product": product, "scope": scope})


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "scope.db")
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.mark.parametrize("product", ["ecs", "rds"])
def test_estimated_jobs_reject_old_scope_and_preserve_job_health(product):
    job = configuration(product, estimated=True)["jobs"][0]
    for scope in ("all", "intranet", "", None):
        metric = reading(product, scope=scope)
        assert not network_resource_selected(job, metric)
        assert not network_resource_selected(job, metric.to_dict())
    assert network_resource_selected(job, reading(product, scope="intranet_estimated"))
    assert not network_resource_selected(job, reading(product, scope="intranet_estimated", account="other"))
    for name in HEALTH_METRICS:
        metric = replace(reading(product), metric=name, dimensions={})
        assert network_resource_selected(job, metric)
    assert network_resource_selected(configuration(product)["jobs"][0], reading(product))


@pytest.mark.parametrize("product", ["ecs", "rds"])
@pytest.mark.parametrize("delivery", ["pending", "failed", "sent"])
def test_startup_cancels_old_total_outbox_without_recovery_or_history_deletion(store, product, delivery):
    old = configuration(product)
    old_point = reading(product)
    Engine(store, old).evaluate(old["jobs"][0], old_point, NOW)
    store.save_samples("network", [old_point])
    original = store.list_messages()[0]
    if delivery == "failed":
        store.delivery_failed(original["id"], NOW, 1, "offline_failure")
    elif delivery == "sent":
        store.delivered(original["id"])

    notifier = Mock()
    service = Service(configuration(product, estimated=True), store, notifier=notifier)
    assert not store.status()["active_alerts"]
    assert [(row["id"], row["status"]) for row in store.list_messages()] == [
        (original["id"], "sent" if delivery == "sent" else "cancelled"),
    ]
    assert store.retry_failed(NOW) == 0
    service.dispatch(NOW)
    notifier.send.assert_not_called()
    assert len(store.latest_samples(["network"])) == 1
    with store.connect() as connection:
        assert connection.execute(select(func.count()).select_from(SAMPLES)).scalar_one() == 1
        assert not connection.execute(select(STATES)).first()


@pytest.mark.parametrize("product", ["ecs", "rds"])
def test_migration_preserves_health_and_unmodified_jobs_and_accounts(store, product):
    cfg = configuration(product)
    old_job = cfg["jobs"][0]
    other_job = {**deepcopy(old_job), "id": "other-network", "account": "other"}
    cfg["jobs"].append(other_job)
    engine = Engine(store, cfg)
    engine.evaluate(old_job, reading(product), NOW)
    engine.evaluate(other_job, reading(product, account="other"), NOW)
    health = replace(reading(product), resource_id="network", metric="collection_failed", value=1, dimensions={})
    engine.evaluate(old_job, health, NOW, rules=[COLLECTION_RULE])
    store.mark_collection_failure("network", NOW)
    cfg["jobs"][0]["options"]["scope"] = "intranet_estimated"
    cfg["rules"][0]["match"]["scope"] = "intranet_estimated"

    Service(cfg, store, notifier=Mock())
    assert {(row["job_id"], row["metric"]["metric"]) for row in store.status()["active_alerts"]} == {
        ("other-network", "network_in_bps"), ("network", "collection_failed"),
    }
    assert store.last_collection_failure("network") == NOW.timestamp()
    assert sorted(row["status"] for row in store.list_messages()) == ["cancelled", "pending", "pending"]


@pytest.mark.parametrize("product", ["ecs", "rds"])
def test_estimated_alert_starts_from_new_samples_and_export_excludes_old_totals(store, product):
    old = configuration(product, for_samples=2)
    old_point = reading(product)
    Engine(store, old).evaluate(old["jobs"][0], old_point, NOW)
    store.save_samples("network", [old_point])
    assert not store.list_messages()

    cfg = configuration(product, estimated=True, for_samples=2)
    service = Service(cfg, store, notifier=Mock())
    with store.connect() as connection:
        assert not connection.execute(select(STATES)).first()
    service.engine.evaluate(cfg["jobs"][0], reading(product, seconds=300), NOW + timedelta(seconds=300))
    assert not store.list_messages()
    assert not store.status()["active_alerts"]
    for seconds in (300, 600):
        point = reading(product, scope="intranet_estimated", seconds=seconds)
        now = NOW + timedelta(seconds=seconds)
        service.engine.evaluate(cfg["jobs"][0], point, now)
        store.save_samples("network", [point])
        store.job_status("network", "ok", now, 1)
        if seconds == 300:
            assert not store.list_messages()
    assert len(store.list_messages()) == 1
    active = store.status()["active_alerts"]
    assert len(active) == 1
    assert active[0]["metric"]["dimensions"]["scope"] == "intranet_estimated"
    exported = {family.name: family.samples for family in text_string_to_metric_families(
        render_metrics(service, NOW + timedelta(seconds=600)).decode())}
    for name in ("resource_value", "resource_fresh", "resource_observed_timestamp_seconds"):
        rows = exported["cloud_monitor_" + name]
        assert len(rows) == 1
        assert rows[0].labels["scope"] == "intranet_estimated"
    assert len(exported["cloud_monitor_active_alert"]) == 1
    assert len(store.latest_samples(["network"])) == 2
    with store.connect() as connection:
        assert connection.execute(select(func.count()).select_from(SAMPLES)).scalar_one() == 3


@pytest.mark.parametrize("product", ["ecs", "rds"])
def test_pending_old_total_recovery_is_cancelled_on_scope_change(store, product):
    cfg = configuration(product)
    engine = Engine(store, cfg)
    engine.evaluate(cfg["jobs"][0], reading(product), NOW)
    store.delivered(store.list_messages()[0]["id"])
    engine.evaluate(cfg["jobs"][0], reading(product, value=0, seconds=300), NOW + timedelta(seconds=300))
    Service(configuration(product, estimated=True), store, notifier=Mock())
    assert [row["status"] for row in store.list_messages()] == ["sent", "cancelled"]
    assert not store.status()["active_alerts"]
