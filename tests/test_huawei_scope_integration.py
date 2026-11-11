from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from prometheus_client.parser import text_string_to_metric_families

from cloud_monitor.config import validate_config
from cloud_monitor.diagnostics import safe_collection_error
from cloud_monitor.engine import Engine
from cloud_monitor.models import Metric
from cloud_monitor.prometheus import render_metrics
from cloud_monitor.providers.huawei import HuaweiCollector, HuaweiCollectionError
from cloud_monitor.providers.network_traffic import NetworkTrafficCollector, validate_network_traffic_options
from cloud_monitor.resource_selection import network_resource_selected
from cloud_monitor.service import Service
from cloud_monitor.store import Store


NOW = datetime(2026, 10, 27, 10, 6, tzinfo=timezone.utc)
SCOPE = "intranet_estimated"
JOB = {"id": "network", "account": "main", "kind": "network_traffic", "interval_seconds": 300,
       "max_age_seconds": 900, "options": {"product": "ecs", "scope": SCOPE}}
RULE = {"id": "network-in", "metric": "network_in_bps",
        "match": {"provider": "huawei", "product": "ecs", "scope": SCOPE},
        "levels": [{"severity": "P1", "op": "ge", "threshold": 100}]}


def configuration(job=None, rules=None):
    return validate_config({"version": 1,
        "accounts": [{"id": "main", "provider": "huawei", "enabled": True, "regions": ["cn-east-3"]}],
        "jobs": [deepcopy(job or JOB)], "rules": deepcopy(rules if rules is not None else [RULE])})


def metric(scope=SCOPE, value=123, resource="instance", observed=NOW):
    return Metric("huawei", "main", resource, resource, "network_in_bps", value, "bit/s", observed,
                  {"region": "cn-east-3", "product": "ecs", "scope": scope, "source": "total_minus_eip"})


def families(service, now=NOW):
    return {family.name: family.samples
            for family in text_string_to_metric_families(render_metrics(service, now).decode())}


@pytest.fixture
def store():
    instance = Store(":memory:")
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.mark.parametrize("product", ["ecs", "rds"])
def test_estimate_scope_uses_300_second_default_and_is_huawei_only(product):
    validate_network_traffic_options({"product": product, "scope": SCOPE}, "huawei")
    with pytest.raises(ValueError, match="only supports Huawei"):
        validate_network_traffic_options({"product": product, "scope": SCOPE}, "aliyun")
    with pytest.raises(ValueError, match="300-second"):
        validate_network_traffic_options({"product": product, "scope": SCOPE, "period": 60}, "huawei")


@pytest.mark.parametrize("scope", ["all", "intranet", "public", None, 1, True])
def test_invalid_explicit_network_scope_is_rejected(scope):
    with pytest.raises(ValueError, match="Explicit network scope"):
        validate_network_traffic_options({"product": "ecs", "scope": scope}, "huawei")


def test_selected_instances_route_to_the_persistent_estimator_without_total_metric_requests(monkeypatch):
    instances = [{"resource_id": "selected", "resource_name": "selected-name", "region": "cn-east-3"}]
    owner = HuaweiCollector({"id": "main"})
    owner._metrics = Mock(side_effect=AssertionError("total-only collector must not run"))
    calls = []

    class Estimator:
        def __init__(self, collector):
            assert collector is owner

        def collect(self, options, selected, now):
            calls.append((deepcopy(options), deepcopy(selected), now))
            return [metric(resource=selected[0]["resource_id"])]

    monkeypatch.setattr("cloud_monitor.providers.huawei_network_scope.HuaweiIntranetCollector", Estimator)
    collector = NetworkTrafficCollector(owner, "huawei")
    collector._discover = Mock(side_effect=AssertionError("explicit selection must not rediscover"))
    options = {"product": "ecs", "scope": SCOPE, "instances": instances}
    assert collector.collect(options, NOW)[0].resource_id == "selected"
    cached = collector._huawei_intranet_collector
    collector.collect(options, NOW + timedelta(seconds=300))
    assert collector._huawei_intranet_collector is cached
    assert calls[0] == (options, instances, NOW)
    assert len(calls) == 2


def test_empty_ecs_estimate_selection_never_constructs_an_estimator(monkeypatch):
    constructor = Mock(side_effect=AssertionError("empty selection must not query"))
    monkeypatch.setattr("cloud_monitor.providers.huawei_network_scope.HuaweiIntranetCollector", constructor)
    collector = NetworkTrafficCollector(HuaweiCollector({"id": "main"}), "huawei")
    assert collector.collect({"product": "ecs", "scope": SCOPE, "instances": []}, NOW) == []
    constructor.assert_not_called()


@pytest.mark.parametrize("status", ["error", "no_data", "insufficient_data", "waiting", "stale"])
def test_estimated_values_are_hidden_whenever_collection_is_not_ok(store, status):
    service = Service(configuration(), store, notifier=Mock())
    store.save_samples("network", [metric()])
    store.job_status("network", status, NOW, 1)
    exported = families(service)
    assert not exported["cloud_monitor_resource_value"]
    assert exported["cloud_monitor_resource_fresh"][0].value == 0
    assert exported["cloud_monitor_resource_observed_timestamp_seconds"][0].value == NOW.timestamp()


def test_effectively_stale_estimate_job_hides_a_still_age_valid_sample(store):
    service = Service(configuration(), store, notifier=Mock())
    store.save_samples("network", [metric()])
    store.job_status("network", "ok", NOW, 1)
    exported = families(service, NOW + timedelta(seconds=811))
    assert any(sample.labels["status"] == "stale" and sample.value == 1
               for sample in exported["cloud_monitor_job_status"])
    assert not exported["cloud_monitor_resource_value"]
    assert exported["cloud_monitor_resource_fresh"][0].value == 0


def test_partial_estimate_failure_is_persisted_but_hidden_without_fabricated_zero(store):
    class Collector:
        network_partial_failures = 1

        def collect(self, kind, options, now):
            return [metric()]

    cfg = configuration(rules=[])
    notifier = Mock()
    service = Service(cfg, store, collector_factory=lambda _: Collector(), notifier=notifier)
    assert not service.collect(cfg["jobs"][0], NOW)
    assert store.status()["jobs"][0]["status"] == "error"
    assert store.latest_samples(["network"])[0]["payload"]["value"] == 123
    exported = families(service)
    assert not exported["cloud_monitor_resource_value"]
    assert all(sample.value == 0 for sample in exported["cloud_monitor_resource_fresh"])
    notifier.send.assert_not_called()


def test_total_scope_migration_hides_old_series_and_cancels_old_alerts_without_recovery(store):
    old_job = deepcopy(JOB)
    old_job["options"].pop("scope")
    old_rule = deepcopy(RULE)
    old_rule["match"]["scope"] = "all"
    old_cfg = configuration(old_job, [old_rule])
    old_reading = metric("all")
    Engine(store, old_cfg).evaluate(old_cfg["jobs"][0], old_reading, NOW)
    store.save_samples("network", [old_reading])
    assert len(store.status()["active_alerts"]) == 1
    assert store.list_messages()[0]["status"] == "pending"
    notifier = Mock()
    service = Service(configuration(), store, notifier=notifier)
    assert store.status()["active_alerts"] == []
    assert [message["status"] for message in store.list_messages()] == ["cancelled"]
    assert not families(service)["cloud_monitor_resource_value"]
    assert len(store.latest_samples(["network"])) == 1
    service.dispatch(NOW)
    notifier.send.assert_not_called()


def test_estimate_selection_preserves_health_metrics_and_filters_old_total_payloads():
    assert not network_resource_selected(JOB, metric("all"))
    assert network_resource_selected(JOB, metric())
    health = Metric("huawei", "main", "network", "network", "collection_failed", 1, "bool", NOW, {})
    assert network_resource_selected(JOB, health)


@pytest.mark.parametrize("message", [
    "Huawei network estimate has no complete unambiguous EIP mapping",
    "Huawei network estimate source has an invalid rate, unit or timestamp",
    "Huawei network estimate sources are stale or in the future",
    "Huawei network estimate has a negative or non-finite difference",
    "Huawei network estimate is waiting for a closed window after an EIP binding change",
])
def test_estimate_diagnostics_preserve_only_known_static_reasons(message):
    assert safe_collection_error(HuaweiCollectionError(message)) == "HuaweiCollectionError: " + message
    assert safe_collection_error(HuaweiCollectionError(message + " secret-resource-id")) == "HuaweiCollectionError"


def test_estimate_query_diagnostics_allow_expected_codes_and_reject_payloads():
    message = "Huawei vpc network estimate query failed (status=403, code=VPC.2201)"
    assert safe_collection_error(HuaweiCollectionError(message)) == "HuaweiCollectionError: " + message
    for forbidden in (
        "Huawei vpc network estimate query failed (status=403, code=private_access_key)",
        message + " private-response-body",
        "Huawei unknown network estimate query failed (status=403, code=VPC.2201)",
        "Huawei vpc network estimate query failed (status=999, code=VPC.2201)",
    ):
        assert safe_collection_error(HuaweiCollectionError(forbidden)) == "HuaweiCollectionError"
