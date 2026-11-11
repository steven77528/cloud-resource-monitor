from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from cloud_monitor.config import validate_config
from cloud_monitor.engine import Engine
from cloud_monitor.models import Metric
from cloud_monitor.notifiers import NotificationDisabled, NotificationError, Notifier
from cloud_monitor.service import Service
from cloud_monitor.store import Store


NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
JOB = {"id": "expiry-job", "account": "test", "kind": "ecs_expiry", "interval_seconds": 60}


def configured(sms_enabled=False):
    return validate_config({
        "version": 1,
        "service": {
            "database": {"host": "mysql", "name": "monitor", "user": "test", "password": "test"},
            "repeat_seconds": {"P0": 120, "P1": 120, "P2": 120},
        },
        "accounts": [{"id": "test", "provider": "aliyun", "enabled": True,
                      "access_key_id": "test-key", "access_key_secret": "test-secret",
                      "regions": ["cn-hangzhou"]}],
        "jobs": [deepcopy(JOB)],
        "rules": [{"id": "expiry", "metric": "expiry_days", "levels": [
            {"severity": "P0", "op": "le", "threshold": 7},
            {"severity": "P1", "op": "le", "threshold": 30},
            {"severity": "P2", "op": "le", "threshold": 90},
        ]}],
        "notifications": {
            "sms": {"enabled": sms_enabled},
            "dingtalk": {"webhook": "https://example.invalid/robot"},
            "email": {"host": "smtp.example.invalid", "from_address": "monitor@example.invalid",
                      "to": ["operations@example.invalid"]},
        },
    })


def sample(days, resource="ecs-1", when=NOW):
    return Metric("aliyun", "test", resource, resource, "expiry_days", days, "days", when)


@pytest.fixture
def store(tmp_path):
    database = Store(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.dispose()


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def send(self, channel, event):
        self.calls.append((channel, event))


def test_live_config_accepts_disabled_sms_without_sender_credentials():
    cfg = validate_config(configured(), live=True)
    assert cfg["notifications"]["sms"]["enabled"] is False


@pytest.mark.parametrize("channel,field", [("dingtalk", "webhook"), ("email", "host")])
def test_disabled_sms_does_not_skip_other_channel_validation(channel, field):
    cfg = configured()
    cfg["notifications"][channel][field] = ""
    with pytest.raises(ValueError, match=f"notifications.{channel}.{field}"):
        validate_config(cfg, live=True)


@pytest.mark.parametrize("invalid", ["false", "true", 0, 1, None, [], {}])
@pytest.mark.parametrize("live", [False, True])
def test_sms_enabled_requires_a_boolean(invalid, live):
    cfg = configured()
    cfg["notifications"]["sms"]["enabled"] = invalid
    with pytest.raises(ValueError, match="enabled"):
        validate_config(cfg, live=live)


def test_sms_enabled_defaults_to_true_for_existing_configs(monkeypatch):
    cfg = configured()
    cfg["notifications"]["sms"] = {
        "provider": "aliyun", "access_key_id": "test-key", "access_key_secret": "test-secret",
        "sign_name": "Test", "template_code": "SMS_TEST", "phone_numbers": ["13800000000"],
    }
    validated = validate_config(cfg, live=True)
    notifier = Notifier(validated["notifications"])
    calls = []
    monkeypatch.setattr(notifier, "_sms", lambda event: calls.append(event))
    event = {"id": "compatibility", "severity": "P0"}
    notifier.send("sms", event)
    assert calls == [event]

    cfg["notifications"]["sms"].pop("access_key_id")
    with pytest.raises(ValueError, match="notifications.sms.access_key_id"):
        validate_config(cfg, live=True)


def test_disabled_notifier_never_invokes_sms_transport(monkeypatch):
    notifier = Notifier({"sms": {"enabled": False}})
    calls = []
    monkeypatch.setattr(notifier, "_sms", lambda event: calls.append(event))
    with pytest.raises(NotificationDisabled) as error:
        notifier.send("sms", {"id": "disabled", "severity": "P0"})
    assert isinstance(error.value, NotificationError)
    assert calls == []


def test_disabled_sms_keeps_highest_alert_and_records_skip(store):
    Engine(store, configured()).evaluate(JOB, sample(5), NOW)
    rows = store.list_messages()
    assert len(rows) == 1
    assert rows[0]["channel"] == "sms"
    assert rows[0]["status"] == "skipped"
    assert rows[0]["last_error"] == "channel_disabled"
    assert rows[0]["attempts"] == 0
    assert json.loads(rows[0]["payload"])["severity"] == "P0"
    assert [state["active"] for state in store.status()["active_alerts"]] == ["P0"]
    assert store.due_messages(NOW + timedelta(days=1)) == []


def test_service_start_skips_existing_sms_but_dispatches_other_channels(store):
    engine = Engine(store, configured(sms_enabled=True))
    engine.evaluate(JOB, sample(5, "sms-pending"), NOW)
    engine.evaluate(JOB, sample(5, "sms-failed"), NOW)
    engine.evaluate(JOB, sample(20, "dingtalk"), NOW)
    engine.evaluate(JOB, sample(70, "email"), NOW)
    rows = {json.loads(row["payload"])["resource_id"]: row for row in store.list_messages()}
    store.delivery_failed(rows["sms-failed"]["id"], NOW, 1, "NotificationError")
    notifier = RecordingNotifier()

    service = Service(configured(), store, notifier=notifier)
    sms_rows = [row for row in store.list_messages() if row["channel"] == "sms"]
    assert len(sms_rows) == 2
    assert {row["status"] for row in sms_rows} == {"skipped"}
    assert {row["last_error"] for row in sms_rows} == {"channel_disabled"}
    service.dispatch(NOW)
    assert sorted(channel for channel, _ in notifier.calls) == ["dingtalk", "email"]
    assert store.status()["outbox"] == {"sent": 2, "skipped": 2}


def test_dispatch_skips_sms_added_after_start_even_during_backoff(store):
    notifier = RecordingNotifier()
    service = Service(configured(), store, notifier=notifier)
    Engine(store, configured(sms_enabled=True)).evaluate(JOB, sample(5), NOW)
    event_id = store.list_messages()[0]["id"]
    store.delivery_failed(event_id, NOW, 3, "NotificationError")
    assert store.due_messages(NOW) == []

    service.dispatch(NOW)
    row = store.list_messages()[0]
    assert row["status"] == "skipped"
    assert row["last_error"] == "channel_disabled"
    assert row["attempts"] == 1
    assert notifier.calls == []


def test_skipped_messages_are_not_retried_and_expire_at_retention_boundary(store):
    engine = Engine(store, configured())
    engine.evaluate(JOB, sample(5, "old"), NOW)
    recent = NOW + timedelta(days=2)
    engine.evaluate(JOB, sample(5, "recent", recent), recent)
    assert store.retry_failed(recent) == 0
    assert {row["status"] for row in store.list_messages()} == {"skipped"}

    store.prune(NOW + timedelta(days=31), 30)
    rows = store.list_messages()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["resource_id"] == "recent"


def test_reenabling_sms_sends_new_repeat_without_replaying_skipped_history(store):
    Engine(store, configured()).evaluate(JOB, sample(5), NOW)
    skipped_id = store.list_messages()[0]["id"]
    notifier = RecordingNotifier()
    service = Service(configured(sms_enabled=True), store, notifier=notifier)
    service.dispatch(NOW + timedelta(seconds=60))
    assert notifier.calls == []

    before_repeat = NOW + timedelta(seconds=60)
    service.engine.evaluate(JOB, sample(5, when=before_repeat), before_repeat)
    assert len(store.list_messages()) == 1
    repeat = NOW + timedelta(seconds=120)
    service.engine.evaluate(JOB, sample(5, when=repeat), repeat)
    service.dispatch(repeat)
    assert len(notifier.calls) == 1
    channel, event = notifier.calls[0]
    assert channel == "sms"
    assert event["id"] != skipped_id
    assert event["severity"] == "P0"
    rows = {row["id"]: row for row in store.list_messages()}
    assert rows[skipped_id]["status"] == "skipped"
    assert rows[event["id"]]["status"] == "sent"
