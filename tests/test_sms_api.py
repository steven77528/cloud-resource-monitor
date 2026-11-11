from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPResponse
import json
import socket
import threading
from types import SimpleNamespace

import pytest

from cloud_monitor.api import make_server
from cloud_monitor.notifiers import NotificationError


TOKEN = "sms-api-admin-token-only-for-tests"
PATH = "/notifications/sms/test"


class FakeNotifier:
    def __init__(self, error=None):
        self.contents = []
        self.error = error

    def send_sms_test(self, content):
        self.contents.append(content)
        if self.error is not None:
            raise self.error
        return "manual-sms-event-id"


@contextmanager
def running_http(notifier=None, token=TOKEN):
    service = SimpleNamespace(
        config={"api": {"admin_token": token}, "notifications": {"sms": {"enabled": False}}},
        notifier=notifier if notifier is not None else FakeNotifier(),
    )
    server = make_server(service, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], service.notifier
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def request(port, payload=None, *, path=PATH, authorization=f"Bearer {TOKEN}",
            body=None, extra_headers=(), content_type="application/json", length=None):
    if body is None:
        body = json.dumps(payload if payload is not None else {"content": "test"}, ensure_ascii=False).encode()
    lines = [f"POST {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Connection: close"]
    if authorization is not None:
        lines.append(f"Authorization: {authorization}")
    if content_type is not None:
        lines.append(f"Content-Type: {content_type}")
    if length is not False:
        lines.append(f"Content-Length: {len(body) if length is None else length}")
    lines.extend(f"{name}: {value}" for name, value in extra_headers)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        # Send one packet so early rejections do not race a separate body write on Windows.
        connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body)
        response = HTTPResponse(connection)
        response.begin()
        return response.status, json.loads(response.read()), dict(response.getheaders())


@pytest.mark.parametrize("authorization,path,extra_headers", [
    (None, PATH, ()),
    (TOKEN, PATH, ()),
    ("Bearer wrong", PATH, ()),
    (None, f"{PATH}?token={TOKEN}", ()),
    (f"Bearer {TOKEN}", PATH, (("Authorization", f"Bearer {TOKEN}"),)),
])
def test_requires_single_bearer_authorization(authorization, path, extra_headers):
    with running_http() as (port, notifier):
        assert request(port, authorization=authorization, path=path, extra_headers=extra_headers)[0] == 401
        assert notifier.contents == []
        assert request(port)[0] == 200


@pytest.mark.parametrize("token", [None, "", "short", " " * 30, 123])
def test_admin_token_must_be_configured_and_at_least_24_characters(token):
    with running_http(token=token) as (port, notifier):
        assert request(port)[0] == 503
        assert notifier.contents == []


@pytest.mark.parametrize("payload", [
    {}, [], "message", {"content": None}, {"content": True}, {"content": 12},
    {"content": ""}, {"content": " \n\t"}, {"content": "x" * 1001},
    {"content": "test", "phone_numbers": ["13800000000"]},
    {"content": "test", "access_key_secret": "override"},
    {"content": "test", "template_code": "override"},
])
def test_invalid_payload_and_recipient_or_credential_overrides_are_rejected(payload):
    with running_http() as (port, notifier):
        assert request(port, payload)[0] == 400
        assert notifier.contents == []
        assert request(port)[0] == 200


@pytest.mark.parametrize("body", [
    b'{"content":', b'{"content":"\xff"}', b'{"content":"\\ud800"}',
    b'{"content":"first","content":"second"}', b"[" * 1200 + b"]" * 1200,
])
def test_invalid_json_and_unicode_do_not_reach_sender(body):
    with running_http() as (port, notifier):
        assert request(port, body=body)[0] == 400
        assert notifier.contents == []
        assert request(port)[0] == 200


@pytest.mark.parametrize("options,status", [
    ({"content_type": None}, 415),
    ({"content_type": "text/plain"}, 415),
    ({"extra_headers": (("Content-Type", "application/json"),)}, 415),
    ({"extra_headers": (("Transfer-Encoding", "chunked"),)}, 400),
    ({"extra_headers": (("Content-Length", "18"),)}, 400),
    ({"length": False}, 400),
    ({"length": "-1"}, 400),
    ({"length": "+18"}, 400),
    ({"length": "18,18"}, 400),
    ({"length": "1e2"}, 400),
    ({"length": "9" * 50}, 400),
    ({"body": b""}, 413),
    ({"body": b" " * (16 * 1024 + 1)}, 413),
])
def test_request_framing_and_size_limits(options, status):
    with running_http() as (port, notifier):
        assert request(port, **options)[0] == status
        assert notifier.contents == []
        assert request(port)[0] == 200


def test_manual_sms_accepts_exact_chinese_content_while_automatic_sms_is_disabled():
    content = " \u6d4b\u8bd5\u77ed\u4fe1\uff1a\u4e91\u8d44\u6e90\u76d1\u63a7\u544a\u8b66\n\u8bf7\u786e\u8ba4\u6536\u5230\u3002 "
    with running_http() as (port, notifier):
        code, result, _ = request(port, {"content": content}, content_type="application/json; charset=utf-8")
        assert code == 200
        assert result == {"status": "accepted", "channel": "sms", "event_id": "manual-sms-event-id"}
        assert notifier.contents == [content]
        code, _, headers = request(port)
        assert code == 429
        assert 1 <= int(headers["Retry-After"]) <= 60
        assert notifier.contents == [content]


@pytest.mark.parametrize("error,status", [
    (ValueError("secret-key and 13800000000 and private content"), 400),
    (NotificationError("secret-key and 13800000000 and private content"), 502),
    (RuntimeError("secret-key and 13800000000 and private content"), 502),
])
def test_failures_are_sanitized_and_consume_cooldown_without_retry(error, status):
    with running_http(FakeNotifier(error)) as (port, notifier):
        code, result, _ = request(port, {"content": "private content"})
        assert code == status
        assert not any(secret in json.dumps(result) for secret in ("secret-key", "13800000000", "private content"))
        assert request(port)[0] == 429
        assert notifier.contents == ["private content"]


def test_cooldown_uses_monotonic_time_and_is_per_server(monkeypatch):
    moment = [1000.0]
    monkeypatch.setattr("cloud_monitor.api.time.monotonic", lambda: moment[0])
    with running_http() as (first_port, first_notifier), running_http() as (second_port, second_notifier):
        assert request(first_port)[0] == 200
        assert request(second_port)[0] == 200
        moment[0] += 59
        code, _, headers = request(first_port)
        assert code == 429
        assert headers["Retry-After"] == "1"
        moment[0] += 1
        assert request(first_port)[0] == 200
        assert first_notifier.contents == ["test", "test"]
        assert second_notifier.contents == ["test"]


def test_concurrent_requests_cannot_send_twice_even_if_first_delivery_exceeds_cooldown(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    moment = [1000.0]
    monkeypatch.setattr("cloud_monitor.api.time.monotonic", lambda: moment[0])

    class BlockingNotifier(FakeNotifier):
        def send_sms_test(self, content):
            self.contents.append(content)
            entered.set()
            assert release.wait(5)
            return "manual-sms-event-id"

    with running_http(BlockingNotifier()) as (port, notifier), ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request, port)
        try:
            assert entered.wait(5)
            moment[0] += 61
            assert request(port)[0] == 429
            assert notifier.contents == ["test"]
        finally:
            release.set()
        assert first.result(timeout=5)[0] == 200
