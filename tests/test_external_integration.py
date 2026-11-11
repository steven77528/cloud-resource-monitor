from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from prometheus_client.parser import text_string_to_metric_families

from cloud_monitor.config import load_config, read_config_data, validate_config
from cloud_monitor.engine import Engine
from cloud_monitor.models import Metric
from cloud_monitor.prometheus import render_metrics
from cloud_monitor.providers import make_collector
from cloud_monitor.providers.common import ProviderError
from cloud_monitor.providers.huifu import HuifuCollector
from cloud_monitor.service import Service
from cloud_monitor.store import Store


NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
RULE = {"id": "low", "metric": "huifu_available_balance", "levels": [
    {"severity": "P1", "op": "lt", "threshold": 100, "for_samples": 2}]}
JOB = {"id": "balance", "account": "merchant", "kind": "balance", "interval_seconds": 60,
       "max_age_seconds": 120, "rules": ["low", "signature"], "collection_error_samples": 3}


def config():
    return validate_config({"version": 1, "accounts": [{"id": "merchant", "provider": "huifu", "enabled": True}],
                            "jobs": [deepcopy(JOB)], "rules": [deepcopy(RULE),
                            {"id": "signature", "metric": "api_signature_failed", "levels": [
                                {"severity": "P1", "op": "eq", "threshold": 1}]}]})


def sample(value=10, seconds=0):
    return Metric("huifu", "merchant", "merchant:01:a", "Account", "huifu_available_balance", value,
                  "CNY", NOW + timedelta(seconds=seconds))


def test_consecutive_samples_restart_duplicate_and_gap(tmp_path):
    cfg = config()
    store = Store(tmp_path / "state.db")
    engine = Engine(store, cfg)
    engine.evaluate(JOB, sample(), NOW)
    engine.evaluate(JOB, sample(), NOW)
    assert not store.status()["active_alerts"]
    Engine(store, cfg).evaluate(JOB, sample(seconds=60), NOW + timedelta(seconds=60))
    assert len(store.status()["active_alerts"]) == 1
    for second in (120, 180):
        engine.evaluate(JOB, sample(200, second), NOW + timedelta(seconds=second))
    assert not store.status()["active_alerts"]
    engine.evaluate(JOB, sample(seconds=240), NOW + timedelta(seconds=240))
    engine.evaluate(JOB, sample(seconds=500), NOW + timedelta(seconds=500))
    assert not store.status()["active_alerts"]


def test_failed_poll_breaks_business_consecutive_samples_after_restart(tmp_path):
    store = Store(tmp_path / "state.db")

    class Collector:
        def collect(self, kind, options, now):
            if now == NOW + timedelta(seconds=60):
                raise ProviderError("transport_error")
            return [replace(sample(), observed_at=now)]

    def service():
        return Service(config(), store, collector_factory=lambda _: Collector())

    assert service().collect(JOB, NOW)
    assert not service().collect(JOB, NOW + timedelta(seconds=60))
    assert service().collect(JOB, NOW + timedelta(seconds=120))
    assert not store.status()["active_alerts"]
    assert service().collect(JOB, NOW + timedelta(seconds=180))
    assert {state["rule_id"] for state in store.status()["active_alerts"]} == {"low"}


def test_signature_failure_does_not_recover_on_network_failure(tmp_path):
    store = Store(tmp_path / "state.db")

    class Collector:
        def collect(self, kind, options, now):
            if now < NOW + timedelta(seconds=180):
                raise ProviderError("signature_invalid" if now == NOW else "transport_error")
            return [replace(sample(1000), observed_at=now)]

    service = Service(config(), store, collector_factory=lambda _: Collector())
    for second in (0, 60, 120):
        assert not service.collect(JOB, NOW + timedelta(seconds=second))
    assert {s["rule_id"] for s in store.status()["active_alerts"]} == {"signature", "collection-error"}
    for second in (180, 240):
        assert service.collect(JOB, NOW + timedelta(seconds=second))
    assert not store.status()["active_alerts"]
    assert all(json.loads(row["payload"])["metric"] != "huifu_available_balance" for row in store.list_messages())


def test_query_health_persistence_and_stale_last_success(tmp_path):
    store = Store(tmp_path / "state.db")

    class Collector:
        def collect(self, kind, options, now):
            if now != NOW:
                raise ProviderError("transport_error")
            return [sample(1000)]

    service = Service(config(), store, collector_factory=lambda _: Collector())
    assert service.collect(JOB, NOW)
    assert not service.collect(JOB, NOW + timedelta(seconds=180))
    result = render_metrics(service, NOW + timedelta(seconds=181)).decode()
    values = [s for family in text_string_to_metric_families(result) for s in family.samples
              if s.name == "cloud_monitor_resource_value"]
    keyed = {s.labels["metric"]: s.value for s in values}
    assert "huifu_available_balance" not in keyed
    assert keyed["api_query_success"] == 0
    assert keyed["api_last_success_timestamp_seconds"] == NOW.timestamp()
    assert store.status()["jobs"][0]["status"] == "error"


def test_signature_recovery_requires_uninterrupted_successes(tmp_path):
    store = Store(tmp_path / "state.db")

    class Collector:
        def collect(self, kind, options, now):
            if now == NOW:
                raise ProviderError("signature_invalid")
            if now == NOW + timedelta(seconds=120):
                raise ProviderError("transport_error")
            return [replace(sample(1000), observed_at=now)]

    service = Service(config(), store, collector_factory=lambda _: Collector())
    for second in (0, 60, 120, 180):
        service.collect(JOB, NOW + timedelta(seconds=second))
    assert {s["rule_id"] for s in store.status()["active_alerts"]} == {"signature"}
    assert service.collect(JOB, NOW + timedelta(seconds=240))
    assert not store.status()["active_alerts"]


def test_empty_settlement_manifest_cannot_recover_signature_alert(tmp_path):
    path = tmp_path / "orders.json"
    order = {"resource_id": "order-1", "org_req_date": "20260911", "org_req_seq_id": "req-1",
             "created_at": NOW.isoformat()}
    path.write_text(json.dumps({"updated_at": NOW.isoformat(), "orders": [order]}))
    job = {**JOB, "kind": "settlements", "options": {"orders_file": str(path)}}
    cfg = config()
    cfg["accounts"][0]["huifu_id"] = "merchant-id"
    cfg["jobs"] = [job]
    store = Store(tmp_path / "state.db")

    class Collector(HuifuCollector):
        def _post(self, *_):
            self.query_performed = True
            raise ProviderError("signature_invalid")

    service = Service(cfg, store, collector_factory=Collector)
    assert not service.collect(job, NOW)
    path.write_text(json.dumps({"updated_at": NOW.isoformat(), "orders": []}))
    for second in (60, 120):
        assert service.collect(job, NOW + timedelta(seconds=second))
    assert {s["rule_id"] for s in store.status()["active_alerts"]} == {"signature"}
    values = {row["payload"]["metric"]: row["payload"]["value"] for row in store.latest_samples([job["id"]])}
    assert values["huifu_tracked_settlements"] == 0
    assert values["api_signature_failed"] == 1
    assert values["api_query_success"] == 0
    assert "api_last_success_timestamp_seconds" not in values


@pytest.mark.parametrize("kind,instrumented", [
    ("company_verification", True), ("company_verification_snapshot", False), ("package_ledger", False),
])
def test_asign_native_query_health_is_separate_from_local_sources(tmp_path, kind, instrumented):
    cfg = config()
    cfg["accounts"][0]["provider"] = "asign"
    job = {**JOB, "kind": kind}
    cfg["jobs"] = [job]

    class Collector:
        def collect(self, *_):
            return [Metric("asign", "merchant", "company", "Company", "company_verified", 1, "bool", NOW)]

    store = Store(tmp_path / "state.db")
    service = Service(cfg, store, collector_factory=lambda _: Collector())
    assert service.collect(job, NOW)
    values = {row["payload"]["metric"] for row in store.latest_samples([job["id"]])}
    assert ("api_query_success" in values) is instrumented
    assert ("api_last_success_timestamp_seconds" in values) is instrumented


def test_external_no_data_is_an_explicit_collection_error(tmp_path):
    class Empty:
        def collect(self, *_):
            return []

    store = Store(tmp_path / "state.db")
    service = Service(config(), store, collector_factory=lambda _: Empty())
    assert not service.collect(JOB, NOW)
    assert store.status()["jobs"][0]["detail"] == "ProviderError: no_data"
    assert not any(row["payload"]["metric"] == "huifu_available_balance"
                   for row in store.latest_samples([JOB["id"]]))


@pytest.mark.parametrize("count", [0, -1, True, "2", 1.5])
def test_invalid_consecutive_sample_config(count):
    cfg = config()
    cfg["rules"][0]["levels"][0]["for_samples"] = count
    with pytest.raises(ValueError, match="for_samples"):
        validate_config(cfg)


def test_monitoring_fragments_are_additive_and_cannot_override_notifications(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\nmonitoring_files: [extra.yaml]\nnotifications: {sms: {enabled: false}}\n")
    fragment = tmp_path / "extra.yaml"
    fragment.write_text("accounts: [{id: qiniu-test, provider: qiniu, enabled: false}]\n")
    cfg = load_config(path)
    assert cfg["accounts"][0]["provider"] == "qiniu"
    assert cfg["notifications"]["sms"]["enabled"] is False
    fragment.write_text("notifications: {sms: {enabled: true}}\n")
    with pytest.raises(ValueError, match="only accounts, jobs and rules"):
        load_config(path)


def test_shipped_integrations_load_without_changing_enablement():
    root = Path(__file__).resolve().parents[1]
    original = read_config_data(root / "config/config.yaml")
    cfg = load_config(root / "config/config.yaml")
    providers = {"qiniu", "huifu", "asign", "wechat"}
    accounts = [a for a in cfg["accounts"] if a["provider"] in providers]
    assert {a["provider"] for a in accounts} == providers
    ids = {a["id"] for a in accounts}
    assert {a["id"]: a["enabled"] for a in accounts} == {
        a["id"]: a["enabled"] for a in original["accounts"] if a["id"] in ids
    }
    assert {j["id"]: j["enabled"] for j in cfg["jobs"] if j["account"] in ids} == {
        j["id"]: j["enabled"] for j in original["jobs"] if j["account"] in ids
    }


@pytest.mark.parametrize("provider", ["huifu", "qiniu", "asign", "wechat"])
def test_provider_dispatch_requires_relay(provider):
    with pytest.raises(ProviderError, match="relay_not_configured"):
        make_collector({"id": "unused", "provider": provider})
