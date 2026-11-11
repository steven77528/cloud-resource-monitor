from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from cloud_monitor.config import validate_config
from cloud_monitor.engine import Engine
from cloud_monitor.models import Metric
from cloud_monitor.service import Service
from cloud_monitor.store import Store


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
RULE = {"id": "expiry", "metric": "expiry_days", "levels": [
    {"severity": "P0", "op": "le", "threshold": 7},
    {"severity": "P1", "op": "le", "threshold": 30},
]}
JOB = {"id": "expiry-job", "account": "test", "kind": "ecs_expiry", "interval_seconds": 60}


def config(rule=RULE):
    return validate_config({"version": 1, "rules": [deepcopy(rule)], "jobs": [JOB],
                            "accounts": [{"id": "test", "provider": "aliyun", "enabled": True}]})


def sample(value, seconds=0, dimensions=None):
    return Metric("aliyun", "test", "ecs-1", "Production", "expiry_days", value, "days",
                  NOW + timedelta(seconds=seconds), dimensions or {})


def messages(store):
    return store.list_messages()


@pytest.mark.parametrize("days,severity", [(31, None), (30, "P1"), (7.01, "P1"),
                                            (7, "P0"), (0, "P0"), (-1, "P0")])
def test_expiry_boundaries_only_highest_level(tmp_path, days, severity):
    store = Store(tmp_path / "state.db")
    Engine(store, config()).evaluate(JOB, sample(days), NOW)
    rows = messages(store)
    assert len(rows) == int(severity is not None)
    if severity:
        assert json.loads(rows[0]["payload"])["severity"] == severity
        assert rows[0]["channel"] == ("sms" if severity == "P0" else "dingtalk")


def test_restart_dedup_and_renewal_recovery_use_stable_identity(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    engine = Engine(store, config())
    engine.evaluate(JOB, sample(25, dimensions={"expires_at": "old"}), NOW)
    store.delivered(messages(store)[0]["id"])
    restarted = Engine(Store(path), config())
    restarted.evaluate(JOB, sample(24, 60, {"expires_at": "old"}), NOW + timedelta(seconds=60))
    assert len(messages(store)) == 1
    for seconds in (120, 180):
        restarted.evaluate(JOB, sample(365, seconds, {"expires_at": "renewed"}), NOW + timedelta(seconds=seconds))
    rows = messages(store)
    assert len(rows) == 2
    assert json.loads(rows[-1]["payload"])["phase"] == "recovery"
    assert not store.status()["active_alerts"]


def test_distinct_in_and_out_bandwidth_do_not_share_alert_state():
    incoming = sample(90, dimensions={"region": "cn-hangzhou", "metric_name": "incoming"})
    outgoing = replace(incoming, dimensions={"region": "cn-hangzhou", "metric_name": "outgoing"})
    assert incoming.key() != outgoing.key()


def test_duration_requires_fresh_contiguous_observations(tmp_path):
    rule = {"id": "duration", "metric": "expiry_days", "levels": [
        {"severity": "P1", "op": "ge", "threshold": 80, "for_seconds": 300}]}
    store = Store(tmp_path / "state.db")
    engine = Engine(store, config(rule))
    for seconds in (0, 60, 120, 300, 360, 420, 480, 540):
        engine.evaluate(JOB, sample(90, seconds), NOW + timedelta(seconds=seconds))
    assert not messages(store)
    engine.evaluate(JOB, sample(90, 600), NOW + timedelta(seconds=600))
    assert len(messages(store)) == 1


def test_duplicate_or_stale_data_cannot_advance_duration_or_recover(tmp_path):
    store = Store(tmp_path / "state.db")
    engine = Engine(store, config())
    engine.evaluate(JOB, sample(5), NOW)
    engine.evaluate(JOB, sample(60), NOW)
    assert store.status()["active_alerts"][0]["active"] == "P0"
    with pytest.raises(ValueError, match="stale"):
        engine.evaluate(JOB, sample(60, 1), NOW + timedelta(seconds=3600))
    with pytest.raises(ValueError, match="future"):
        engine.evaluate(JOB, sample(60, 300), NOW)
    assert store.status()["active_alerts"][0]["active"] == "P0"


def test_outbox_backoff_and_terminal_retry_are_persistent(tmp_path):
    store = Store(tmp_path / "state.db")
    Engine(store, config()).evaluate(JOB, sample(5), NOW)
    event_id = messages(store)[0]["id"]
    store.delivery_failed(event_id, NOW, 2, "Timeout")
    assert not store.due_messages(NOW + timedelta(seconds=29))
    assert len(store.due_messages(NOW + timedelta(seconds=30))) == 1
    store.delivery_failed(event_id, NOW + timedelta(seconds=30), 2, "Timeout")
    assert not store.due_messages(NOW + timedelta(days=1))
    assert store.status()["outbox"]["failed"] == 1
    assert store.retry_failed(NOW) == 1
    assert len(store.due_messages(NOW)) == 1


def test_collection_error_routes_p2_without_resolving_resource_alert(tmp_path):
    store = Store(tmp_path / "state.db")
    cfg = config()
    Engine(store, cfg).evaluate(JOB, sample(5), NOW)

    class BrokenCollector:
        def collect(self, *_):
            raise RuntimeError("Never persist secret credentials")

    service = Service(cfg, store, collector_factory=lambda _: BrokenCollector())
    assert service.collect(JOB, NOW + timedelta(seconds=60)) is False
    assert any(r["channel"] == "email" for r in messages(store))
    assert "secret credentials" not in json.dumps(store.status())
    assert {s["active"] for s in store.status()["active_alerts"]} == {"P0", "P2"}


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), True])
def test_invalid_numeric_input_rejected(invalid):
    with pytest.raises(ValueError):
        sample(invalid)


def test_invalid_enabled_metric_cannot_pass_live_config_check():
    cfg = config()
    cfg["accounts"][0].update(access_key_id="test", access_key_secret="test", regions=["cn-hangzhou"])
    cfg["jobs"] = [{**JOB, "kind": "metrics", "options": {"metrics": [{
        "namespace": "x", "metric_name": "storage", "resource_id": "bucket",
        "dimensions": {"bucket": "bucket"}, "output_metric": "storage_used_percent", "limit": None,
    }]}}]
    with pytest.raises(ValueError, match="limit"):
        validate_config(cfg, live=True)


def test_no_enabled_accounts_cannot_start_accidentally():
    with pytest.raises(ValueError, match="No enabled jobs"):
        validate_config({"version": 1}, live=True)


def test_offline_memory_store_supports_lock_and_cross_thread_queries():
    store = Store(":memory:")
    try:
        with store.acquire_lock():
            assert store.check_lock()
            Engine(store, config()).evaluate(JOB, sample(5), NOW)
            with ThreadPoolExecutor(max_workers=1) as executor:
                state = executor.submit(store.status).result(timeout=5)
            assert state["active_alerts"][0]["active"] == "P0"
            assert state["outbox"] == {"pending": 1}
            with pytest.raises(RuntimeError, match="scheduler lock"):
                with store.acquire_lock():
                    pass
        with store.acquire_lock():
            assert len(store.list_messages()) == 1
    finally:
        store.dispose()
