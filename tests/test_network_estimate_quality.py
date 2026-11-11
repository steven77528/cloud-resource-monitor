from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest
from prometheus_client.parser import text_string_to_metric_families

from cloud_monitor.config import validate_config
from cloud_monitor.models import Metric
from cloud_monitor.prometheus import render_metrics
from cloud_monitor.providers.huawei import HuaweiCollectionError
from cloud_monitor.service import Service
from cloud_monitor.store import Store


NOW = datetime(2026, 10, 30, 6, 10, tzinfo=timezone.utc)


def config(*, for_samples=1):
    return validate_config({
        "version": 1,
        "accounts": [{"id": "main", "provider": "huawei", "enabled": True}],
        "jobs": [{"id": "network", "account": "main", "kind": "network_traffic",
                  "interval_seconds": 300, "max_age_seconds": 900,
                  "options": {"product": "ecs", "scope": "intranet_estimated"}}],
        "rules": [{"id": "high", "metric": "network_out_bps",
                   "levels": [{"severity": "P1", "op": "ge", "threshold": 100,
                               "for_samples": for_samples}]}],
    })


def at(second):
    return NOW + timedelta(seconds=second)


def pair(second, *, resource="ecs", value=10, region="cn-east-3"):
    return [Metric("huawei", "main", resource, resource, name, value, "bit/s", at(second),
                   {"region": region, "product": "ecs", "scope": "intranet_estimated",
                    "source": "total_minus_eip"})
            for name in ("network_in_bps", "network_out_bps")]


def rejected(second, resource="ecs", region="cn-east-3"):
    return {"resource_id": resource, "region": region, "observed_at": at(second),
            "reason": "negative_difference"}


class Collector:
    def __init__(self):
        self.metrics = []
        self.unavailable = []
        self.failure = None
        self.network_partial_failures = 0
        self.network_estimate_resources = None

    def collect(self, kind, options, now):
        self.network_estimate_unavailable = deepcopy(self.unavailable)
        if self.failure:
            raise self.failure
        return list(self.metrics)


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / "quality.db")
    cfg, collector, notifier = config(), Collector(), Mock()
    service = Service(cfg, store, collector_factory=lambda _: collector, notifier=notifier)
    yield store, cfg, collector, service, notifier
    store.dispose()


def quality(store):
    return next(iter(store.status()["estimate_quality"]["network"].values()))


def events(store, rule):
    return [json.loads(row["payload"]) for row in store.list_messages()
            if json.loads(row["payload"])["rule_id"] == rule]


def values(service, second):
    return [sample for family in text_string_to_metric_families(render_metrics(service, at(second)).decode())
            for sample in family.samples if sample.name == "cloud_monitor_resource_value"]


def test_only_negative_estimates_are_degraded_without_collection_error(runtime):
    store, cfg, collector, service, notifier = runtime
    collector.unavailable = [rejected(0)]
    assert service.collect(cfg["jobs"][0], NOW)
    job = store.status()["jobs"][0]
    assert job["status"] == "degraded"
    assert job["sample_count"] == 0
    assert "unavailable_resources=1" in job["detail"]
    assert quality(store)["invalid_observed_at"] == NOW.timestamp()
    assert store.list_messages() == []
    assert store.latest_samples(["network"]) == []
    notifier.send.assert_not_called()


def test_newer_paired_sample_required_to_unblock_same_resource_after_restart(runtime):
    store, cfg, collector, service, _ = runtime
    job = cfg["jobs"][0]
    collector.metrics = pair(-300)
    assert service.collect(job, NOW)
    collector.metrics, collector.unavailable = [], [rejected(0)]
    assert service.collect(job, at(300))
    store.dispose()
    restarted_store = Store(store.path)
    try:
        service = Service(cfg, restarted_store, collector_factory=lambda _: collector, notifier=Mock())
        assert values(service, 300) == []
        collector.unavailable = []
        for points in (pair(-300), pair(0), pair(300)[:1]):
            collector.metrics = points
            assert service.collect(job, at(300))
            assert restarted_store.status()["jobs"][0]["status"] == "degraded"
            assert values(service, 300) == []
        collector.metrics = pair(300)
        assert service.collect(job, at(300))
        assert restarted_store.status()["jobs"][0]["status"] == "ok"
        exported = values(service, 300)
        assert len(exported) == 2
        assert all(sample.value == 10 for sample in exported)
        assert quality(restarted_store)["invalid_observed_at"] == NOW.timestamp()
        assert quality(restarted_store)["valid_observed_at"] == at(300).timestamp()
    finally:
        restarted_store.dispose()


def test_invalid_watermark_is_monotonic_and_does_not_hide_other_resource(runtime):
    store, cfg, collector, service, _ = runtime
    job = cfg["jobs"][0]
    collector.unavailable = [rejected(0)]
    collector.metrics = pair(0, resource="healthy") + pair(0, region="cn-north-4")
    assert service.collect(job, NOW)
    assert len(values(service, 0)) == 4
    collector.unavailable = [rejected(-300)]
    assert service.collect(job, at(300))
    state = next(state for state in store.status()["estimate_quality"]["network"].values()
                 if state["resource_id"] == "ecs" and state["region"] == "cn-east-3")
    assert state["invalid_observed_at"] == NOW.timestamp()
    assert len(values(service, 300)) == 4


@pytest.mark.parametrize("partial", [False, True])
def test_real_error_takes_precedence_and_keeps_negative_quality(runtime, partial):
    store, cfg, collector, service, _ = runtime
    collector.unavailable = [rejected(0)]
    if partial:
        collector.network_partial_failures = 1
        collector.metrics = pair(0, resource="healthy")
    else:
        collector.failure = HuaweiCollectionError("Huawei network estimate source has no datapoints")
    assert not service.collect(cfg["jobs"][0], NOW)
    assert store.status()["jobs"][0]["status"] == "error"
    assert quality(store)["invalid_observed_at"] == NOW.timestamp()
    assert len(events(store, "collection-error")) == 1


def test_api_failure_and_empty_success_cannot_clear_invalid_watermark(runtime):
    store, cfg, collector, service, _ = runtime
    job = cfg["jobs"][0]
    collector.metrics = pair(-300)
    assert service.collect(job, NOW)
    collector.metrics, collector.unavailable = [], [rejected(0)]
    assert service.collect(job, NOW)
    collector.unavailable, collector.failure = [], HuaweiCollectionError("API failed")
    assert not service.collect(job, at(300))
    collector.failure = None
    assert service.collect(job, at(300))
    assert store.status()["jobs"][0]["status"] == "degraded"
    assert quality(store)["invalid_observed_at"] == NOW.timestamp()
    assert values(service, 300) == []


def test_degraded_query_rounds_recover_collection_alert_only(runtime):
    store, cfg, collector, service, _ = runtime
    job = cfg["jobs"][0]
    collector.failure = HuaweiCollectionError("API failed")
    assert not service.collect(job, NOW)
    collector.failure, collector.unavailable = None, [rejected(0)]
    assert service.collect(job, at(300))
    assert [event["phase"] for event in events(store, "collection-error")] == ["firing"]
    assert service.collect(job, at(600))
    assert [event["phase"] for event in events(store, "collection-error")] == ["firing", "recovery"]
    assert store.status()["jobs"][0]["status"] == "degraded"


def test_invalid_window_breaks_business_threshold_continuity(runtime):
    store, cfg, collector, _, _ = runtime
    cfg = config(for_samples=2)
    service = Service(cfg, store, collector_factory=lambda _: collector, notifier=Mock())
    job = cfg["jobs"][0]
    collector.metrics = pair(0, value=200)
    assert service.collect(job, NOW)
    collector.metrics, collector.unavailable = [], [rejected(300)]
    assert service.collect(job, at(300))
    collector.metrics, collector.unavailable = pair(600, value=200), []
    assert service.collect(job, at(600))
    assert events(store, "high") == []
    collector.metrics = pair(900, value=200)
    assert service.collect(job, at(900))
    assert [event["phase"] for event in events(store, "high")] == ["firing"]


def test_invalid_window_preserves_active_alert_and_breaks_recovery_continuity(runtime):
    store, cfg, collector, service, _ = runtime
    job = cfg["jobs"][0]
    collector.metrics = pair(0, value=200)
    assert service.collect(job, NOW)
    collector.metrics = pair(300, value=10)
    assert service.collect(job, at(300))
    collector.metrics, collector.unavailable = [], [rejected(600)]
    assert service.collect(job, at(600))
    assert {state["rule_id"] for state in store.status()["active_alerts"]} == {"high"}
    collector.metrics, collector.unavailable = pair(900, value=10), []
    service = Service(cfg, store, collector_factory=lambda _: collector, notifier=Mock())
    assert service.collect(job, at(900))
    assert [event["phase"] for event in events(store, "high")] == ["firing"]
    collector.metrics = pair(1200, value=10)
    assert service.collect(job, at(1200))
    assert [event["phase"] for event in events(store, "high")] == ["firing", "recovery"]


def test_pending_recovery_is_cancelled_when_a_new_negative_point_arrives(runtime):
    store, cfg, collector, service, notifier = runtime
    job = cfg["jobs"][0]
    for second, value in ((0, 200), (300, 10), (600, 10)):
        collector.metrics = pair(second, value=value)
        assert service.collect(job, at(second))
    assert any(row["status"] == "pending" and json.loads(row["payload"])["phase"] == "recovery"
               for row in store.list_messages())
    collector.metrics, collector.unavailable = [], [rejected(900)]
    assert service.collect(job, at(900))
    service.dispatch(at(900))
    notifier.send.assert_not_called()
    assert all(row["status"] == "cancelled" for row in store.list_messages())


def test_disappeared_resource_is_out_of_scope_but_watermark_survives_rediscovery(runtime):
    store, cfg, collector, service, _ = runtime
    job = cfg["jobs"][0]
    collector.network_estimate_resources = [{"resource_id": "ecs", "region": "cn-east-3"}]
    collector.metrics = pair(-300)
    assert service.collect(job, NOW)
    collector.metrics, collector.unavailable = [], [rejected(0)]
    assert service.collect(job, NOW)
    collector.network_estimate_resources, collector.unavailable = [], []
    assert service.collect(job, at(300))
    assert store.status()["jobs"][0]["status"] == "no_data"
    assert quality(store)["in_scope"] is False
    assert values(service, 300) == []
    collector.network_estimate_resources = [{"resource_id": "ecs", "region": "cn-east-3"}]
    collector.metrics = pair(0)
    assert service.collect(job, at(300))
    assert store.status()["jobs"][0]["status"] == "degraded"
    assert quality(store)["invalid_observed_at"] == NOW.timestamp()
    assert values(service, 300) == []
    collector.metrics = pair(300)
    assert service.collect(job, at(300))
    assert store.status()["jobs"][0]["status"] == "ok"
