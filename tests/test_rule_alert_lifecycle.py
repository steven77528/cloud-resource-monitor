from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest

from cloud_monitor.config import validate_config
from cloud_monitor.engine import Engine
from cloud_monitor.models import Metric
from cloud_monitor.service import COLLECTION_RULE, Service
from cloud_monitor.store import Store


NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
JOB = {"id": "balance", "account": "qiniu-test", "kind": "balance", "interval_seconds": 60,
       "rules": ["cash-low", "available-low"]}
RULES = [{"id": rule_id, "metric": metric, "levels": [
    {"severity": "P1", "op": "le", "threshold": 500},
]} for rule_id, metric in (("cash-low", "qiniu_cash_balance"), ("available-low", "qiniu_available_balance"))]


def configuration():
    return validate_config({
        "version": 1,
        "accounts": [{"id": "qiniu-test", "provider": "qiniu", "enabled": True}],
        "jobs": [deepcopy(JOB)], "rules": deepcopy(RULES),
        "service": {"recovery_samples": 1},
    })


def sample(name="qiniu_cash_balance", value=417.08, seconds=0):
    return Metric("qiniu", "qiniu-test", "finance-account", "finance-account", name, value, "CNY",
                  NOW + timedelta(seconds=seconds), {"currency": "CNY"})


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "state.db")
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.mark.parametrize("change", ["unbind", "delete", "delete_without_selection"])
@pytest.mark.parametrize("delivery", ["pending", "failed", "sent"])
def test_restart_removes_obsolete_balance_alert_without_affecting_available_or_health(store, change, delivery):
    cfg = configuration()
    engine = Engine(store, cfg)
    metrics = [sample(), sample("qiniu_available_balance", 407.74)]
    for metric in metrics:
        engine.evaluate(JOB, metric, NOW)
    store.save_samples(JOB["id"], metrics)
    cash_message = next(row for row in store.list_messages()
                        if json.loads(row["payload"])["rule_id"] == "cash-low")
    if delivery == "failed":
        store.delivery_failed(cash_message["id"], NOW, 1, "offline_failure")
    elif delivery == "sent":
        store.delivered(cash_message["id"])
    health = replace(sample(), metric="collection_failed", value=1, unit="bool", resource_id=JOB["id"])
    engine.evaluate(JOB, health, NOW, rules=[COLLECTION_RULE])
    store.mark_collection_failure(JOB["id"], NOW)

    cfg["jobs"][0]["rules"] = ["available-low"]
    if change != "unbind":
        cfg["rules"] = [rule for rule in cfg["rules"] if rule["id"] != "cash-low"]
    if change == "delete_without_selection":
        cfg["jobs"][0].pop("rules")
    notifier = Mock()
    service = Service(cfg, store, notifier=notifier)
    Service(cfg, store, notifier=Mock())

    assert {state["rule_id"] for state in store.status()["active_alerts"]} == {
        "available-low", "collection-error",
    }
    messages = store.list_messages()
    assert len(messages) == 3
    assert next(row["status"] for row in messages if row["id"] == cash_message["id"]) == (
        "sent" if delivery == "sent" else "cancelled"
    )
    assert all(json.loads(row["payload"])["phase"] == "firing" for row in messages)
    assert {row["payload"]["metric"] for row in store.latest_samples([JOB["id"]])} == {
        "qiniu_cash_balance", "qiniu_available_balance",
    }
    assert store.last_collection_failure(JOB["id"]) == NOW.timestamp()
    assert store.retry_failed(NOW) == 0
    service.dispatch(NOW)
    assert {call.args[1]["rule_id"] for call in notifier.send.call_args_list} == {
        "available-low", "collection-error",
    }


def test_unbinding_rule_cancels_pending_recovery(store):
    cfg = configuration()
    engine = Engine(store, cfg)
    engine.evaluate(JOB, sample(), NOW)
    store.delivered(store.list_messages()[0]["id"])
    later = NOW + timedelta(seconds=60)
    engine.evaluate(JOB, sample(value=600, seconds=60), later)
    assert json.loads(store.list_messages()[-1]["payload"])["phase"] == "recovery"
    cfg["jobs"][0]["rules"] = ["available-low"]
    notifier = Mock()
    service = Service(cfg, store, notifier=notifier)
    assert [row["status"] for row in store.list_messages()] == ["sent", "cancelled"]
    service.dispatch(later)
    notifier.send.assert_not_called()


def test_empty_selection_clears_candidates_before_rule_is_reenabled(store):
    cfg = configuration()
    cfg["rules"][0]["levels"][0]["for_samples"] = 2
    Engine(store, cfg).evaluate(JOB, sample(), NOW)
    disabled = deepcopy(cfg)
    disabled["jobs"][0]["rules"] = []
    Service(disabled, store, notifier=Mock())
    restarted = Service(cfg, store, notifier=Mock())
    restarted.engine.evaluate(JOB, sample(seconds=60), NOW + timedelta(seconds=60))
    assert not store.list_messages()
    restarted.engine.evaluate(JOB, sample(seconds=120), NOW + timedelta(seconds=120))
    assert len(store.list_messages()) == 1


def test_unbinding_is_scoped_to_job_and_unrestricted_jobs_keep_configured_rules(store):
    cfg = configuration()
    other_job = {**JOB, "id": "other-balance"}
    other_job.pop("rules")
    cfg["jobs"].append(other_job)
    engine = Engine(store, cfg)
    engine.evaluate(JOB, sample(), NOW)
    engine.evaluate(other_job, sample(), NOW)
    cfg["jobs"][0]["rules"] = ["available-low"]
    Service(cfg, store, notifier=Mock())
    assert [state["job_id"] for state in store.status()["active_alerts"]] == [other_job["id"]]
    assert sorted(row["status"] for row in store.list_messages()) == ["cancelled", "pending"]
