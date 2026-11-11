from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading

import yaml

from cloud_monitor.config import validate_config
from cloud_monitor.models import Metric, utcnow
from cloud_monitor import notification_gateway
from cloud_monitor.service import Service
from cloud_monitor.store import Store


TOKEN = "local-integration-test-token-only"


def monitor_config(url):
    return validate_config({
        "version": 1,
        "accounts": [{"id": "test", "provider": "aliyun", "enabled": True}],
        "notifications": {"gateway": {"url": url, "token": TOKEN, "timeout_seconds": 2}},
        "jobs": [{"id": "expiry", "account": "test", "kind": "ecs_expiry", "interval_seconds": 60}],
        "rules": [{"id": "expiry", "metric": "expiry_days", "levels": [
            {"severity": "P0", "op": "le", "threshold": 0},
            {"severity": "P1", "op": "le", "threshold": 30},
        ]}],
    })


def evaluate(service, resource, now, value=0):
    metric = Metric("aliyun", "test", resource, resource, "expiry_days", value, "days", now)
    service.engine.evaluate(service.jobs[0], metric, now)


def test_real_client_gateway_dispatch_skip_and_hot_route_change(tmp_path, monkeypatch):
    config_path = tmp_path / "notifications.yaml"
    config = {"version": 1, "api": {"token": TOKEN},
              "routes": {"P0": "sms", "P1": "dingtalk", "P2": "email"},
              "notifications": {"sms": {"enabled": False},
                                "dingtalk": {"webhook": "https://example.invalid/robot"},
                                "email": {"enabled": True}}}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    accepted = []

    class Transport:
        def __init__(self, config):
            pass

        def send(self, channel, event):
            accepted.append((channel, event))

    monkeypatch.setattr(notification_gateway, "Notifier", Transport)
    server = notification_gateway.make_server(config_path, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store = Store(tmp_path / "monitor.db")
    try:
        service = Service(monitor_config(f"http://127.0.0.1:{server.server_port}"), store)
        now = utcnow()
        evaluate(service, "paused-sms", now)
        service.dispatch(now)
        assert store.status()["outbox"] == {"skipped": 1}
        assert store.status()["active_alerts"][0]["active"] == "P0"
        assert not accepted
        config["routes"]["P0"] = "dingtalk"
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        evaluate(service, "rerouted", now + timedelta(seconds=1))
        service.dispatch(now + timedelta(seconds=1))
        assert store.status()["outbox"] == {"skipped": 1, "sent": 1}
        assert [(channel, event["severity"]) for channel, event in accepted] == [("dingtalk", "P0")]
        assert "dimensions" in accepted[0][1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        store.dispose()


def test_collection_state_progresses_while_notification_is_slow(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class SlowTransport:
        def send(self, channel, event):
            entered.set()
            assert release.wait(5)

    store = Store(tmp_path / "monitor.db")
    try:
        service = Service(monitor_config("http://127.0.0.1:8081"), store, notifier=SlowTransport())
        now = utcnow()
        evaluate(service, "first", now)
        with ThreadPoolExecutor(max_workers=2) as executor:
            delivery = executor.submit(service.dispatch, now)
            assert entered.wait(2)
            try:
                collecting = executor.submit(evaluate, service, "second", now + timedelta(seconds=1), 20)
                collecting.result(timeout=2)
                assert len(store.status()["active_alerts"]) == 2
            finally:
                release.set()
            delivery.result(timeout=2)
    finally:
        release.set()
        store.dispose()


def test_unreachable_gateway_keeps_retry_without_changing_collection_state(tmp_path):
    import socket

    # Reserve an unserved TCP port to get a deterministic connection refusal.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        store = Store(tmp_path / "monitor.db")
        try:
            service = Service(monitor_config(f"http://127.0.0.1:{unavailable.getsockname()[1]}"), store)
            now = utcnow()
            evaluate(service, "first", now)
            service.dispatch(now)
            row = store.list_messages()[0]
            assert row["status"] == "pending" and row["attempts"] == 1
            assert row["last_error"] == "NotificationError"
            assert store.status()["active_alerts"][0]["active"] == "P0"
            evaluate(service, "second", now + timedelta(seconds=1), 20)
            assert len(store.status()["active_alerts"]) == 2
        finally:
            store.dispose()
