from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
import yaml

from cloud_monitor.config import load_config, validate_config
from cloud_monitor.diagnostics import safe_collection_error
from cloud_monitor.engine import Engine
from cloud_monitor.models import Metric
from cloud_monitor.providers.rds_performance import RdsPerformanceError
from cloud_monitor.service import Service
from cloud_monitor.store import Store


NOW = datetime(2026, 9, 10, 4, tzinfo=timezone.utc)
JOB = {"id": "rds", "account": "ali", "kind": "rds_performance", "interval_seconds": 60,
       "max_age_seconds": 120, "rules": ["cpu"]}
RULE = {"id": "cpu", "metric": "db_cpu_percent", "levels": [
    {"severity": "P1", "op": "ge", "threshold": 80, "for_seconds": 300}]}


def config():
    return validate_config({"version": 1, "accounts": [{"id": "ali", "provider": "aliyun", "enabled": True}],
                            "jobs": [deepcopy(JOB)], "rules": [deepcopy(RULE)],
                            "notifications": {"sms": {"enabled": False}}})


def point(seconds, value=90, resource="rm-test"):
    return Metric("aliyun", "ali", resource, "Example database", "db_cpu_percent", value, "%",
                  NOW + timedelta(seconds=seconds), {"product": "rds", "engine": "MySQL",
                                                    "sample_max_gap_seconds": 90})


def test_batch_consumes_history_and_only_emits_final_alert(tmp_path):
    store = Store(tmp_path / "state.db")
    engine = Engine(store, config())
    points = [point(second) for second in range(-600, 1, 60)]
    engine.evaluate_batch(JOB, list(reversed(points)), NOW)
    messages = store.list_messages()
    assert len(messages) == 1
    payload = json.loads(messages[0]["payload"])
    assert payload["observed_at"] == NOW.isoformat()
    assert payload["severity"] == "P1"
    engine.evaluate_batch(JOB, points, NOW)
    assert len(store.list_messages()) == 1


def test_old_transient_breach_does_not_generate_late_alert_or_recovery(tmp_path):
    store = Store(tmp_path / "state.db")
    engine = Engine(store, config())
    points = [point(second, 90 if second < -120 else 30) for second in range(-600, 1, 60)]
    engine.evaluate_batch(JOB, points, NOW)
    assert store.list_messages() == []
    assert not store.status()["active_alerts"]


def test_interior_normal_point_restarts_duration_despite_latest_high(tmp_path):
    store = Store(tmp_path / "state.db")
    points = [point(second, 10 if second == -120 else 90) for second in range(-300, 1, 60)]
    Engine(store, config()).evaluate_batch(JOB, points, NOW)
    assert store.list_messages() == []


def test_missing_samples_cannot_prove_five_minutes(tmp_path):
    store = Store(tmp_path / "state.db")
    Engine(store, config()).evaluate_batch(JOB, [point(-300), point(0)], NOW)
    assert store.list_messages() == []


def test_missing_one_minute_sample_breaks_continuity(tmp_path):
    store = Store(tmp_path / "state.db")
    Engine(store, config()).evaluate_batch(JOB, [point(second) for second in (-300, -240, -120, -60, 0)], NOW)
    assert store.list_messages() == []


def test_batch_recovers_existing_alert_once_using_actual_new_samples(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    points = [point(second) for second in range(-300, 1, 60)]
    Engine(store, config()).evaluate_batch(JOB, points, NOW)
    store.delivered(store.list_messages()[0]["id"])
    restarted = Engine(Store(path), config())
    restarted.evaluate_batch(JOB, points + [point(60, 20)], NOW + timedelta(seconds=60))
    assert len(store.list_messages()) == 1
    restarted.evaluate_batch(JOB, points + [point(60, 20), point(120, 20)], NOW + timedelta(seconds=120))
    assert len(store.list_messages()) == 2
    assert json.loads(store.list_messages()[-1]["payload"])["phase"] == "recovery"


def test_all_series_freshness_is_checked_before_alert_state_changes(tmp_path):
    store = Store(tmp_path / "state.db")
    points = [point(second) for second in range(-300, 1, 60)] + [point(-400, resource="rm-other")]
    with pytest.raises(ValueError, match="stale"):
        Engine(store, config()).evaluate_batch(JOB, points, NOW)
    assert store.list_messages() == []


@pytest.mark.parametrize("kind", ["rds_performance", "rds_cloudmonitor"])
def test_service_reuses_native_collector_and_persists_deduplicated_history(tmp_path, kind):
    calls = []
    job = {**JOB, "kind": kind}
    cfg = config()
    cfg["jobs"] = [job]

    class Collector:
        def collect(self, kind, options, now):
            calls.append(kind)
            return [point(second) for second in range(-300, 1, 60)]

    factories = []

    def factory(account):
        factories.append(account["id"])
        return Collector()

    store = Store(tmp_path / "state.db")
    service = Service(cfg, store, collector_factory=factory)
    assert service.collect(job, NOW)
    assert service.collect(job, NOW + timedelta(seconds=60))
    assert factories == ["ali"]
    assert calls == [kind, kind]
    assert len(store.list_messages()) == 1


def test_cloudmonitor_cache_history_checks_ten_minute_duration_without_stale_notifications(tmp_path):
    cfg = config()
    rule = {"id": "cache", "metric": "db_cache_hit_percent", "levels": [
        {"severity": "P1", "op": "lt", "threshold": 95, "for_seconds": 600}]}
    job = {**JOB, "kind": "rds_cloudmonitor", "rules": ["cache"]}
    cfg.update(jobs=[job], rules=[rule])
    readings = []

    class Collector:
        def collect(self, kind, options, now):
            return list(readings)

    def cache_point(second, value):
        return Metric("aliyun", "ali", "pg-test", "PostgreSQL", "db_cache_hit_percent", value, "%",
                      NOW + timedelta(seconds=second), {"sample_max_gap_seconds": 90})

    store = Store(tmp_path / "state.db")
    service = Service(cfg, store, collector_factory=lambda _: Collector())
    readings.extend(cache_point(second, 90 if second < -60 else 99) for second in range(-900, 1, 60))
    assert service.collect(job, NOW)
    assert store.list_messages() == []
    readings[:] = [cache_point(second, 90) for second in range(60, 661, 60)]
    assert service.collect(job, NOW + timedelta(seconds=660))
    assert len(store.list_messages()) == 1
    payload = json.loads(store.list_messages()[0]["payload"])
    assert payload["severity"] == "P1"
    assert payload["metric"] == "db_cache_hit_percent"


@pytest.mark.parametrize("rule_id,metric_name,value,unit,duration", [
    ("db-deadlock-rate", "db_deadlocks_per_second", 0.016667, "count/s", 180),
    ("db-long-queries", "db_long_running_queries", 1, "count", 300),
    ("db-row-lock-waits", "db_row_lock_waits_per_second", 1, "count/s", 300),
])
def test_database_rules_require_full_continuous_window(
    tmp_path, rule_id, metric_name, value, unit, duration,
):
    template = Path(__file__).resolve().parents[1] / "config" / "rds-performance.yaml"
    rules = yaml.safe_load(template.read_text(encoding="utf-8"))["rules"]
    cfg = config()
    cfg["rules"] = [rule for rule in rules if rule["id"] == rule_id]
    job = {**JOB, "rules": [rule_id]}
    cfg["jobs"] = [job]
    store = Store(tmp_path / "state.db")
    engine = Engine(store, cfg)

    def sample(second, observed_value=value):
        return Metric("aliyun", "ali", "pg-test", "PostgreSQL", metric_name, observed_value, unit,
                      NOW + timedelta(seconds=second),
                      {"sample_max_gap_seconds": 90, "long_query_seconds": 5})

    history = [sample(second) for second in range(-duration + 60, 1, 60)]
    engine.evaluate_batch(job, history, NOW)
    assert store.list_messages() == []
    engine.evaluate_batch(job, history + [sample(60)], NOW + timedelta(seconds=60))
    assert len(store.list_messages()) == 1
    payload = json.loads(store.list_messages()[0]["payload"])
    assert payload["severity"] == "P1"
    assert payload["value"] == value
    assert payload["dimensions"]["long_query_seconds"] == 5


def test_cloudmonitor_errors_remain_actionable_and_do_not_echo_cloud_response():
    from cloud_monitor.providers.rds_cloudmonitor import RdsCloudMonitorError

    message = safe_collection_error(RdsCloudMonitorError("no_data"))
    assert "RdsCloudMonitorError" in message and "no data" in message
    assert safe_collection_error(RuntimeError("access_key_secret=hidden")) == "RuntimeError"


def test_split_configuration_keeps_existing_expiry_rule(tmp_path):
    main = tmp_path / "config.yaml"
    main.write_text("version: 1\nmonitoring_files: [rds.yaml]\nrules:\n"
                    "- id: expiry\n  metric: expiry_days\n  levels:\n"
                    "  - {severity: P1, op: le, threshold: 30}\n", encoding="utf-8")
    (tmp_path / "rds.yaml").write_text("rules:\n- id: cpu\n  metric: db_cpu_percent\n"
                                    "  levels:\n  - {severity: P1, op: ge, threshold: 80}\n", encoding="utf-8")
    loaded = load_config(main)
    assert [rule["id"] for rule in loaded["rules"]] == ["expiry", "cpu"]
    assert loaded["rules"][0]["levels"][0]["threshold"] == 30


def test_diagnostic_rds_field_error_is_useful_without_raw_response():
    error = RdsPerformanceError("missing_field", metric="db_disk_used_percent", fields=("total_bytes",))
    assert "total_bytes" in safe_collection_error(error)
    assert "missing" not in safe_collection_error(ValueError("access_key_secret=hidden"))
    assert safe_collection_error(ValueError("access_key_secret=hidden")) == "ValueError"
