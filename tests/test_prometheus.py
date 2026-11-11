from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
import json
import threading
from types import SimpleNamespace

import pytest
from prometheus_client.parser import text_string_to_metric_families

from cloud_monitor.api import make_server
from cloud_monitor.config import validate_config
from cloud_monitor.engine import Engine
from cloud_monitor.models import Metric
from cloud_monitor.prometheus import render_metrics
from cloud_monitor.store import Store


NOW = datetime(2026, 9, 10, 8, tzinfo=timezone.utc)
TOKEN = "test-metrics-readonly-token-123456"


@pytest.fixture
def service():
    config = validate_config({
        "version": 1,
        "accounts": [{"id": "main", "provider": "aliyun", "enabled": True},
                     {"id": "disabled", "provider": "huawei", "enabled": False}],
        "jobs": [{"id": "rds", "account": "main", "kind": "rds_performance", "interval_seconds": 60},
                 {"id": "daily", "account": "main", "kind": "balance", "interval_seconds": 86400}],
        "rules": [{"id": "cpu", "metric": "db_cpu_percent", "levels": [
            {"severity": "P1", "op": "ge", "threshold": 80}]}],
        "api": {"admin_token": "admin-token-never-exposed-123456"},
    })
    store = Store(":memory:")
    result = SimpleNamespace(config=config, store=store, jobs=config["jobs"],
                             accounts={a["id"]: a for a in config["accounts"]},
                             started=True, last_tick=NOW)
    yield result
    store.dispose()


def sample(value=42, seconds=0, dimensions=None, name="database", metric="db_cpu_percent", unit="%"):
    return Metric("aliyun", "main", "resource-1", name, metric, value, unit,
                  NOW + timedelta(seconds=seconds), dimensions or {})


def parsed(service, now=NOW):
    body = render_metrics(service, now).decode()
    families = list(text_string_to_metric_families(body))
    return {family.name: family.samples for family in families}, body


def test_source_units_label_escaping_identity_and_no_sensitive_dimensions(service):
    first = sample(81.5, name='DB "primary"\npath\\one \u6570\u636e\u5e93', dimensions={
        "region": "cn-test", "identity": {"node": "a"}, "secret": "secret-value-never-exported",
        "raw_value": 123, "sql": "select * from private_data",
    })
    second = sample(9, dimensions={"region": "cn-test", "identity": {"node": "b"}})
    service.store.save_samples("rds", [first, second])
    service.store.job_status("rds", "ok", NOW, 2)
    families, body = parsed(service)
    values = families["cloud_monitor_resource_value"]
    assert len(values) == 2
    assert {row.value for row in values} == {81.5, 9}
    assert {row.labels["metric_key"] for row in values} == {first.key(), second.key()}
    assert {row.labels["unit"] for row in values} == {"%"}
    assert values[0].timestamp is None
    assert first.resource_name in {row.labels["resource_name"] for row in values}
    for forbidden in ("secret-value-never-exported", "private_data", "admin-token-never-exposed", "raw_value"):
        assert forbidden not in body
    assert all(row.value == NOW.timestamp() for row in families["cloud_monitor_resource_observed_timestamp_seconds"])
    identities = [(row.name, tuple(sorted(row.labels.items()))) for family in families.values() for row in family]
    assert len(identities) == len(set(identities))


def test_latest_only_and_disabled_or_deleted_jobs_are_not_exported(service):
    service.store.save_samples("rds", [sample(20, -60), sample(80)])
    for job in ("disabled", "deleted"):
        service.store.save_samples(job, [sample(99)])
        service.store.job_status(job, "error", NOW)
    families, _ = parsed(service)
    assert [row.value for row in families["cloud_monitor_resource_value"]] == [80]
    assert {row.labels["job_id"] for row in families["cloud_monitor_job_status"]} == {"rds", "daily"}
    assert len(families["cloud_monitor_alerts"]) == 3


def test_stale_future_and_no_data_omit_value_but_preserve_freshness_metadata(service):
    service.store.save_samples("rds", [sample(seconds=-301)])
    families, _ = parsed(service)
    assert not families["cloud_monitor_resource_value"]
    assert families["cloud_monitor_resource_fresh"][0].value == 0
    service.store.save_samples("rds", [sample(seconds=61)])
    assert not parsed(service)[0]["cloud_monitor_resource_value"]
    service.store.job_status("rds", "no_data", NOW + timedelta(seconds=61))
    assert not parsed(service, NOW + timedelta(seconds=61))[0]["cloud_monitor_resource_value"]


def test_daily_values_remain_available_between_collections_and_configured_age_wins(service):
    service.store.save_samples("daily", [sample(1000, seconds=-86400, metric="balance", unit="CNY")])
    families, _ = parsed(service)
    assert families["cloud_monitor_resource_value"][0].value == 1000
    service.jobs[1]["max_age_seconds"] = 3600
    assert not parsed(service)[0]["cloud_monitor_resource_value"]


def test_job_states_are_one_hot_and_collection_failure_does_not_fabricate_zero(service):
    service.store.save_samples("rds", [sample(89)])
    service.store.job_status("rds", "error", NOW, detail="private failure details")
    families, body = parsed(service)
    active = [row for row in families["cloud_monitor_job_status"] if row.value]
    assert {(row.labels["job_id"], row.labels["status"]) for row in active} == {
        ("rds", "error"), ("daily", "waiting"),
    }
    assert families["cloud_monitor_resource_value"][0].value == 89
    assert "private failure details" not in body
    families, _ = parsed(service, NOW + timedelta(seconds=301))
    assert not families["cloud_monitor_resource_value"]
    assert any(row.labels["status"] == "stale" and row.value == 1 for row in families["cloud_monitor_job_status"])
    assert families["cloud_monitor_scheduler_alive"][0].value == 0


@pytest.mark.parametrize("state", ["no_data", "insufficient_data"])
def test_unavailable_statistics_hide_previous_values_without_zero_samples(service, state):
    service.store.save_samples("rds", [sample(200, metric="sms_growth_percent")])
    service.store.job_status("rds", state, NOW, detail="private statistics detail")
    families, body = parsed(service)
    assert not families["cloud_monitor_resource_value"]
    assert families["cloud_monitor_resource_fresh"][0].value == 0
    assert len(families["cloud_monitor_resource_observed_timestamp_seconds"]) == 1
    active = [row for row in families["cloud_monitor_job_status"] if row.labels["job_id"] == "rds" and row.value]
    assert len(active) == 1 and active[0].labels["status"] == state
    assert "private statistics detail" not in body


def test_alerts_come_from_existing_engine_and_filter_disabled_jobs(service):
    engine = Engine(service.store, service.config)
    engine.evaluate(service.jobs[0], sample(95), NOW)
    engine.evaluate({**service.jobs[0], "id": "disabled"}, sample(99), NOW)
    families, _ = parsed(service)
    alerts = families["cloud_monitor_active_alert"]
    assert len(alerts) == 1 and alerts[0].labels["severity"] == "P1"
    assert alerts[0].labels["rule_id"] == "cpu"
    assert {row.labels["severity"]: row.value for row in families["cloud_monitor_alerts"]} == {
        "P0": 0, "P1": 1, "P2": 0,
    }
    assert len(families["cloud_monitor_outbox_messages"]) == 5
    # A stale measurement does not resolve an existing alert.
    assert len(parsed(service, NOW + timedelta(days=3))[0]["cloud_monitor_active_alert"]) == 1


def test_job_account_reassignment_does_not_relabel_old_samples_or_alerts(service):
    Engine(service.store, service.config).evaluate(service.jobs[0], sample(95), NOW)
    service.store.save_samples("rds", [sample(95)])
    service.accounts["other"] = {"id": "other", "provider": "huawei", "enabled": True}
    service.jobs[0]["account"] = "other"
    families, _ = parsed(service)
    assert not families["cloud_monitor_active_alert"]
    assert not families["cloud_monitor_resource_value"]
    assert all(row.value == 0 for row in families["cloud_monitor_alerts"])


def estimated_network(service, monkeypatch, quality):
    job = service.jobs[0]
    job.update(kind="network_traffic", options={"product": "ecs", "scope": "intranet_estimated",
                                               "instances": [
                                                   {"resource_id": "resource-1", "region": "cn-test"},
                                                   {"resource_id": "resource-2", "region": "cn-test"},
                                               ]})
    original_status = service.store.status
    monkeypatch.setattr(service.store, "status", lambda: {**original_status(), "estimate_quality": quality})
    metrics = [replace(sample(value, metric="network_in_bps", unit="bit/s", dimensions={
        "region": "cn-test", "product": "ecs", "scope": "intranet_estimated",
    }), resource_id=resource_id) for resource_id, value in [("resource-1", 42), ("resource-2", 84)]]
    service.store.save_samples("rds", metrics)


def quality_entry(resource_id, region="cn-test", valid=0):
    return {json.dumps([region, resource_id], separators=(",", ":")): {
        "resource_id": resource_id, "region": region, "reason": "negative_difference",
        "invalid_observed_at": NOW.timestamp(), "valid_observed_at": valid,
    }}


@pytest.mark.parametrize("status", ["degraded", "ok"])
def test_invalid_estimate_hides_only_affected_resource_and_preserves_metadata(service, monkeypatch, status):
    estimated_network(service, monkeypatch, {"rds": quality_entry("resource-1")})
    service.store.job_status("rds", status, NOW, 1)
    families, _ = parsed(service)
    assert [(row.labels["resource_id"], row.value) for row in families["cloud_monitor_resource_value"]] == [
        ("resource-2", 84),
    ]
    assert {row.labels["resource_id"]: row.value for row in families["cloud_monitor_resource_fresh"]} == {
        "resource-1": 0, "resource-2": 1,
    }
    assert len(families["cloud_monitor_resource_observed_timestamp_seconds"]) == 2
    active = [row for row in families["cloud_monitor_job_status"] if row.labels["job_id"] == "rds" and row.value]
    assert len(active) == 1 and active[0].labels["status"] == status
    quality = families["cloud_monitor_network_estimate_available"]
    assert len(quality) == 1 and quality[0].value == 0
    assert quality[0].labels == {"provider": "aliyun", "account": "main", "job_id": "rds",
                                 "resource_id": "resource-1", "region": "cn-test", "reason": "negative_difference"}


@pytest.mark.parametrize("valid,available", [(NOW.timestamp(), 0), (NOW.timestamp() + 1, 1)])
def test_estimate_quality_requires_strictly_newer_valid_observation(service, monkeypatch, valid, available):
    quality = {"rds": {**quality_entry("resource-1", valid=valid),
                       **quality_entry("resource-1", region="other-region"),
                       **quality_entry("not-selected")},
               "daily": quality_entry("resource-1"), "deleted": quality_entry("resource-1")}
    estimated_network(service, monkeypatch, quality)
    if available:
        service.store.save_samples("rds", [sample(42, seconds=1, metric="network_in_bps", unit="bit/s", dimensions={
            "region": "cn-test", "product": "ecs", "scope": "intranet_estimated",
        })])
    service.store.job_status("rds", "ok", NOW, 2)
    families, _ = parsed(service)
    assert len(families["cloud_monitor_resource_value"]) == 1 + available
    states = families["cloud_monitor_network_estimate_available"]
    assert len(states) == 1 and states[0].value == available


def test_recovered_estimate_does_not_revive_older_metric_identities(service, monkeypatch):
    estimated_network(service, monkeypatch, {"rds": quality_entry("resource-1", valid=NOW.timestamp() + 1)})
    service.store.save_samples("rds", [sample(126, seconds=1, metric="network_in_bps", unit="bit/s", dimensions={
        "region": "cn-test", "product": "ecs", "scope": "intranet_estimated", "metric_name": "new-source",
    })])
    service.store.job_status("rds", "ok", NOW, 2)
    families, _ = parsed(service)
    assert sorted(row.value for row in families["cloud_monitor_resource_value"]) == [84, 126]
    old = [row for row in families["cloud_monitor_resource_fresh"]
           if row.labels["resource_id"] == "resource-1" and row.labels["metric_name"] == ""]
    assert len(old) == 1 and old[0].value == 0
    assert families["cloud_monitor_network_estimate_available"][0].value == 1


def test_estimate_without_negative_history_is_available_without_a_failure_reason(service, monkeypatch):
    entry = next(iter(quality_entry("resource-1", valid=NOW.timestamp()).values()))
    entry.update(invalid_observed_at=0, reason="")
    quality = {"rds": {json.dumps(["cn-test", "resource-1"], separators=(",", ":")): entry}}
    estimated_network(service, monkeypatch, quality)
    service.store.job_status("rds", "ok", NOW, 2)
    families, body = parsed(service)
    states = families["cloud_monitor_network_estimate_available"]
    assert len(states) == 1 and states[0].value == 1 and states[0].labels["reason"] == ""
    assert len(families["cloud_monitor_resource_value"]) == 2
    assert "Whether a network estimate has valid paired samples after its latest negative difference." in body


def test_out_of_scope_estimate_hides_quality_and_values_without_losing_recovery_watermark(service, monkeypatch):
    quality = {"rds": quality_entry("resource-1")}
    resource = next(iter(quality["rds"].values()))
    resource["in_scope"] = False
    estimated_network(service, monkeypatch, quality)
    service.store.job_status("rds", "ok", NOW, 1)
    families, _ = parsed(service)
    assert not families["cloud_monitor_network_estimate_available"]
    assert [row.labels["resource_id"] for row in families["cloud_monitor_resource_value"]] == ["resource-2"]
    assert {row.labels["resource_id"]: row.value for row in families["cloud_monitor_resource_fresh"]} == {
        "resource-1": 0, "resource-2": 1,
    }

    resource["in_scope"] = True
    families, _ = parsed(service)
    assert families["cloud_monitor_network_estimate_available"][0].value == 0
    assert [row.labels["resource_id"] for row in families["cloud_monitor_resource_value"]] == ["resource-2"]
    resource["valid_observed_at"] = NOW.timestamp() + 1
    families, _ = parsed(service)
    assert [row.labels["resource_id"] for row in families["cloud_monitor_resource_value"]] == ["resource-2"]
    service.store.save_samples("rds", [sample(42, seconds=1, metric="network_in_bps", unit="bit/s", dimensions={
        "region": "cn-test", "product": "ecs", "scope": "intranet_estimated",
    })])
    families, _ = parsed(service)
    assert families["cloud_monitor_network_estimate_available"][0].value == 1
    assert len(families["cloud_monitor_resource_value"]) == 2


def test_estimate_quality_does_not_hide_the_same_resource_id_in_another_region(service, monkeypatch):
    estimated_network(service, monkeypatch, {"rds": quality_entry("resource-1")})
    service.jobs[0]["options"]["instances"].append({"resource_id": "resource-1", "region": "cn-other"})
    service.store.save_samples("rds", [sample(126, metric="network_in_bps", unit="bit/s", dimensions={
        "region": "cn-other", "product": "ecs", "scope": "intranet_estimated",
    })])
    service.store.job_status("rds", "degraded", NOW, 2)
    families, _ = parsed(service)
    assert {(row.labels["resource_id"], row.labels["region"]): row.value
            for row in families["cloud_monitor_resource_value"]} == {
        ("resource-2", "cn-test"): 84, ("resource-1", "cn-other"): 126,
    }


@pytest.mark.parametrize("status,offset", [("error", 0), ("ok", 301), ("waiting", 0),
                                         ("no_data", 0), ("insufficient_data", 0)])
def test_unhealthy_estimate_job_still_hides_all_resources(service, monkeypatch, status, offset):
    estimated_network(service, monkeypatch, {"rds": quality_entry("resource-1")})
    service.store.job_status("rds", status, NOW, 1)
    families, _ = parsed(service, NOW + timedelta(seconds=offset))
    assert not families["cloud_monitor_resource_value"]
    assert all(row.value == 0 for row in families["cloud_monitor_resource_fresh"])


@contextmanager
def running_http(service):
    server = make_server(service, "127.0.0.1", 0)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()


def get(port, path="/metrics", authorization=None, duplicate=False):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.putrequest("GET", path)
        if authorization is not None:
            connection.putheader("Authorization", authorization)
            if duplicate:
                connection.putheader("Authorization", authorization)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type"), response.read()
    finally:
        connection.close()


def test_http_scrapes_persisted_data_without_collection_or_notification(service, monkeypatch):
    monkeypatch.setattr("cloud_monitor.prometheus.utcnow", lambda: NOW)
    service.store.save_samples("rds", [sample()])
    with running_http(service) as port:
        status, content_type, body = get(port)
        assert status == 200 and content_type.startswith("text/plain; version=0.0.4")
        assert "cloud_monitor_resource_value" in body.decode()
        assert list(text_string_to_metric_families(body.decode()))
        assert get(port, "/status")[0] == 401
    assert not service.store.list_messages()


@pytest.mark.parametrize("status,failed,expected", [("degraded", 0, 200), ("error", 0, 503),
                                                  ("degraded", 1, 503)])
def test_readiness_accepts_unavailable_estimates_but_rejects_collection_or_delivery_failures(
        service, status, failed, expected):
    service.status = lambda: {"scheduler_alive": True, "jobs": [{"id": "rds", "status": status}],
                              "outbox": {"failed": failed}}
    with running_http(service) as port:
        assert get(port, "/readyz")[0] == 401
        authorization = "Bearer " + service.config["api"]["admin_token"]
        assert get(port, "/readyz", authorization)[0] == expected


@pytest.mark.parametrize("authorization,path,duplicate", [
    (None, "/metrics", False), (TOKEN, "/metrics", False),
    ("Bearer wrong", "/metrics", False), (None, f"/metrics?token={TOKEN}", False),
    (f"Bearer {TOKEN}", "/metrics", True),
])
def test_metrics_token_is_strict_bearer_and_separate_from_admin(service, authorization, path, duplicate):
    service.config["api"]["metrics"]["token"] = TOKEN
    with running_http(service) as port:
        assert get(port, path, authorization, duplicate)[0] == 401
        assert get(port, authorization="Bearer " + service.config["api"]["admin_token"])[0] == 401
        assert get(port, authorization=f"Bearer {TOKEN}")[0] == 200


def test_metrics_disabled_and_database_error_fail_closed(service, monkeypatch):
    service.config["api"]["metrics"]["enabled"] = False
    with running_http(service) as port:
        assert get(port)[0] == 404
        service.config["api"]["metrics"]["enabled"] = True

        def failure(*args):
            raise RuntimeError("mysql://private-secret")

        monkeypatch.setattr(service.store, "latest_samples", failure)
        status, _, body = get(port)
        assert status == 503 and b"private-secret" not in body


@pytest.mark.parametrize("metrics", [None, [], {"enabled": "false"}, {"token": ""},
                                         {"token": "short"}, {"token": " " * 30}, {"token": 123}])
def test_invalid_metrics_configuration_is_rejected(metrics):
    with pytest.raises(ValueError, match="api.metrics"):
        validate_config({"version": 1, "api": {"metrics": metrics}})
