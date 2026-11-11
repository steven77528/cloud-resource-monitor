from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
import json
import threading

import pytest
from sqlalchemy import select

from cloud_monitor.api import make_server
from cloud_monitor.models import Metric
from cloud_monitor.service import Service
from cloud_monitor.store import OUTBOX, Store


@pytest.fixture
def service(tmp_path):
    config = {
        "service": {
            "recovery_samples": 1,
            "notify_recovery": True,
            "repeat_seconds": {"P0": 60, "P1": 60, "P2": 60},
            "retry_max_attempts": 1,
        },
        "notifications": {},
        "accounts": [{"id": "production", "provider": "aliyun", "enabled": True}],
        "jobs": [
            {"id": "live-job", "kind": "ecs_expiry", "account": "production", "interval_seconds": 60},
            {"id": "disabled-job", "kind": "ecs_expiry", "account": "production",
             "interval_seconds": 60, "enabled": False},
        ],
        "rules": [{
            "id": "expiry",
            "metric": "expiry_days",
            "levels": [
                {"severity": "P0", "op": "le", "threshold": -1},
                {"severity": "P1", "op": "le", "threshold": 0},
            ],
        }],
        "api": {
            "admin_token": "admin-token-used-only-in-tests",
            "receipts": {
                "enabled": True,
                "accounts": {"production": {"token": "receipt-token-used-only-in-tests"}},
            },
        },
    }
    return Service(config, Store(tmp_path / "monitor.db"))


def expiry(resource_id, value, observed_at):
    return Metric("aliyun", "production", resource_id, resource_id,
                  "expiry_days", value, "days", observed_at)


def evaluate(service, resource_id, value, observed_at):
    service.engine.evaluate(service.jobs[0], expiry(resource_id, value, observed_at), observed_at)


@pytest.mark.parametrize("new_value,phase,severity,channel", [
    (30, "recovery", "P1", "dingtalk"),
    (-1, "firing", "P0", "sms"),
])
def test_dispatch_skips_old_cached_message_after_incident_transition(
    service, new_value, phase, severity, channel
):
    now = datetime.now(timezone.utc)
    evaluate(service, "resource-a", 0, now)
    evaluate(service, "resource-b", 0, now + timedelta(seconds=1))
    initial = service.store.due_messages(now + timedelta(seconds=1))
    obsolete_id = next(row["id"] for row in initial if json.loads(row["payload"])["resource_id"] == "resource-b")
    sent = []

    class TransitionDuringFirstSend:
        def send(self, delivery_channel, event):
            sent.append((delivery_channel, event))
            if event["resource_id"] == "resource-a":
                # Simulate a collection result arriving while an earlier batch item sends.
                evaluate(service, "resource-b", new_value, now + timedelta(seconds=2))

    service.notifier = TransitionDuringFirstSend()
    service.dispatch(now + timedelta(seconds=1))
    assert [event["resource_id"] for _, event in sent] == ["resource-a"]
    service.store.delivered(obsolete_id)
    with service.store.connect() as db:
        assert db.execute(select(OUTBOX.c.status).where(OUTBOX.c.id == obsolete_id)).scalar_one() == "cancelled"

    service.dispatch(now + timedelta(seconds=3))
    assert len(sent) == 2
    actual_channel, latest = sent[1]
    assert actual_channel == channel
    assert latest["resource_id"] == "resource-b"
    assert latest["phase"] == phase
    assert latest["severity"] == severity
    assert latest["value"] == new_value


def test_successful_repeat_supersedes_exhausted_failure(service):
    now = datetime.now(timezone.utc)
    evaluate(service, "resource-a", 0, now)
    original = service.store.due_messages(now)[0]
    service.store.delivery_failed(original["id"], now, 1, "NotificationError")
    assert service.store.status()["outbox"]["failed"] == 1
    evaluate(service, "resource-a", 0, now + timedelta(seconds=61))
    sent = []

    class Success:
        def send(self, channel, event):
            sent.append(event)

    service.notifier = Success()
    service.dispatch(now + timedelta(seconds=61))
    assert len(sent) == 1
    assert sent[0]["id"] != original["id"]
    assert service.store.status()["outbox"].get("failed", 0) == 0
    assert service.store.retry_failed(now + timedelta(seconds=62)) == 0
    service.dispatch(now + timedelta(seconds=63))
    assert len(sent) == 1


@contextmanager
def running_http(service):
    server = make_server(service, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def request(port, method, path, body=None, token=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if body is not None:
        body = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_readiness_ignores_deleted_and_disabled_job_history(service):
    now = datetime.now(timezone.utc)
    service.store.job_status("live-job", "ok", now, count=1)
    service.store.job_status("disabled-job", "error", now, detail="PermissionError")
    service.store.job_status("deleted-job", "error", now, detail="PermissionError")
    service.started = True
    service.last_tick = now
    with running_http(service) as port:
        code, state = request(port, "GET", "/readyz", token=service.config["api"]["admin_token"])
    assert code == 200
    assert {job["id"] for job in state["jobs"]} == {"live-job"}
    assert {job["id"] for job in service.store.status()["jobs"]} == {
        "live-job", "disabled-job", "deleted-job"
    }


@pytest.mark.parametrize("state,expected", [("insufficient_data", 200), ("error", 503)])
def test_readiness_distinguishes_unavailable_statistics_from_failed_requests(service, state, expected):
    now = datetime.now(timezone.utc)
    service.store.job_status("live-job", state, now)
    service.started = True
    service.last_tick = now
    with running_http(service) as port:
        code, result = request(port, "GET", "/readyz", token=service.config["api"]["admin_token"])
    assert code == expected
    assert result["jobs"][0]["status"] == state


def receipt_payload(now):
    return [{
        "phone_number": "13800000000",
        "send_time": (now - timedelta(seconds=5)).isoformat(),
        "report_time": now.isoformat(),
        "success": True,
        "biz_id": "test-business-id",
    }]


def test_http_receipts_require_account_token_and_deduplicate_vendor_retries(service):
    now = datetime.now(timezone.utc)
    payload = receipt_payload(now)
    path = "/webhooks/aliyun/sms/production"
    token = service.config["api"]["receipts"]["accounts"]["production"]["token"]
    with running_http(service) as port:
        assert request(port, "POST", path, payload)[0] == 401
        assert request(port, "POST", path, payload, token="incorrect-token")[0] == 401
        before = service.store.receipt_summary("production", now - timedelta(minutes=1), now + timedelta(minutes=1))
        assert before["total"] == 0
        assert request(port, "POST", path, payload, token=token) == (200, {"code": 0, "msg": "success"})
        assert request(port, "POST", f"{path}?token={token}", payload)[0] == 200
        assert request(port, "POST", "/webhooks/aliyun/sms/unknown", payload, token=token)[0] == 401
    summary = service.store.receipt_summary("production", now - timedelta(minutes=1), now + timedelta(minutes=1))
    assert summary["total"] == 1
    assert summary["success"] == 1


def test_invalid_receipt_batch_is_rejected_without_partial_write(service):
    now = datetime.now(timezone.utc)
    payload = receipt_payload(now)
    invalid = dict(payload[0], phone_number="13900000000", success="invalid")
    token = service.config["api"]["receipts"]["accounts"]["production"]["token"]
    with running_http(service) as port:
        code, _ = request(port, "POST", "/webhooks/aliyun/sms/production", payload + [invalid], token=token)
    assert code == 400
    summary = service.store.receipt_summary("production", now - timedelta(minutes=1), now + timedelta(minutes=1))
    assert summary["total"] == 0


def test_status_endpoint_requires_admin_token_even_with_receipt_token(service):
    receipt_token = service.config["api"]["receipts"]["accounts"]["production"]["token"]
    with running_http(service) as port:
        assert request(port, "GET", "/status")[0] == 401
        assert request(port, "GET", "/status", token=receipt_token)[0] == 401
        assert request(port, "GET", "/status", token=service.config["api"]["admin_token"])[0] == 200
