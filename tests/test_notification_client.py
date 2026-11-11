from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from cloud_monitor import notification_client
from cloud_monitor.notification_client import NotificationClient, NotificationRateLimited, validate_gateway
from cloud_monitor.notifiers import NotificationDisabled, NotificationError


TOKEN = "test-only-gateway-token-123456789"
EVENT = {"id": "event-123", "severity": "P0", "summary": "resource expired"}


@contextmanager
def gateway_server(body=None, status=200, headers=None):
    received = []
    response = body if isinstance(body, bytes) else json.dumps(
        body if body is not None else {"status": "accepted"}
    ).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append({"path": self.path, "headers": dict(self.headers),
                             "body": self.rfile.read(int(self.headers.get("Content-Length", "0")))})
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def do_GET(self):
            self.do_POST()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_port}", "token": TOKEN,
               "timeout_seconds": 2}, received
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.fixture(autouse=True)
def local_requests_bypass_proxies(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")


def test_gateway_config_does_not_require_vendor_or_channel_credentials():
    validate_gateway({"url": "http://notification-gateway:8081", "token": TOKEN})
    validate_gateway({"url": "https://notification.example.invalid/api/", "token": TOKEN,
                      "timeout_seconds": 300})


@pytest.mark.parametrize("invalid", [None, "", "ftp://host", "http://", "http://user:password@host",
                                    "http://host?token=secret", "http://host?", "http://host#fragment",
                                    "http://host#", "http://host:bad", "http://host:0", "http://host:65536",
                                    "http://host\r\nInjected:secret", "http://host/space here"])
def test_invalid_gateway_urls_rejected_without_echoing_values(invalid):
    with pytest.raises(ValueError, match="notifications.gateway.url") as error:
        validate_gateway({"url": invalid, "token": TOKEN})
    assert "secret" not in str(error.value)
    assert "password" not in str(error.value)


@pytest.mark.parametrize("invalid", [None, "", "too-short", " " * 24, "x" * 24 + "\r", "x" * 24 + "\n"])
def test_gateway_token_validation(invalid):
    with pytest.raises(ValueError, match="notifications.gateway.token"):
        validate_gateway({"url": "http://gateway", "token": invalid})


@pytest.mark.parametrize("invalid", [None, True, 0, -1, 301, float("inf"), float("nan"), "20"])
def test_gateway_timeout_validation(invalid):
    with pytest.raises(ValueError, match="notifications.gateway.timeout_seconds"):
        validate_gateway({"url": "http://gateway", "token": TOKEN, "timeout_seconds": invalid})


def test_event_uses_authenticated_json_once_and_gateway_owns_routing():
    with gateway_server() as (config, received):
        config["url"] += "/prefix/"
        assert NotificationClient(config).send("email", EVENT) is None
    assert len(received) == 1
    request = received[0]
    assert request["path"] == "/prefix/notify"
    assert request["headers"]["Authorization"] == "Bearer " + TOKEN
    assert request["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(request["body"]) == {"event": EVENT}


def test_sms_test_forwards_content_and_returns_event_id():
    with gateway_server({"status": "accepted", "event_id": "sms-test-123"}, status=202) as (config, received):
        assert NotificationClient(config).send_sms_test("test message") == "sms-test-123"
    assert len(received) == 1
    assert received[0]["path"] == "/notifications/sms/test"
    assert json.loads(received[0]["body"]) == {"content": "test message"}


def test_exact_disabled_response_is_not_a_retryable_failure():
    with gateway_server({"status": "skipped", "reason": "channel_disabled"}) as (config, received):
        with pytest.raises(NotificationDisabled):
            NotificationClient(config).send("sms", EVENT)
    assert len(received) == 1


@pytest.mark.parametrize("body", [[], {}, {"status": "skipped"}, {"status": "skipped", "reason": "other"},
                                  {"status": "error", "reason": "channel_disabled"},
                                  {"status": "failed", "detail": "private-response-body"},
                                  b"private-response-body", b"\xff"])
def test_unaccepted_or_invalid_response_is_redacted(body):
    with gateway_server(body) as (config, received):
        with pytest.raises(NotificationError) as error:
            NotificationClient(config).send("sms", EVENT)
    assert not isinstance(error.value, NotificationDisabled)
    assert "private-response-body" not in str(error.value)
    assert TOKEN not in str(error.value)
    assert len(received) == 1


@pytest.mark.parametrize("event_id", [None, "", "   ", 1, [], "id\r\nprivate", "id\x7f"])
def test_sms_test_requires_valid_event_id(event_id):
    with gateway_server({"status": "accepted", "event_id": event_id}) as (config, _):
        with pytest.raises(NotificationError, match="SMS test"):
            NotificationClient(config).send_sms_test("test")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_is_not_followed_or_given_credentials(status):
    with gateway_server() as (destination, leaked):
        with gateway_server({}, status=status, headers={"Location": destination["url"] + "/leak"}) as (config, received):
            with pytest.raises(NotificationError):
                NotificationClient(config).send("sms", EVENT)
    assert len(received) == 1
    assert leaked == []


@pytest.mark.parametrize("status", [400, 401, 403, 500, 503])
def test_http_errors_are_redacted_and_not_retried(status):
    with gateway_server({"status": "accepted", "secret": TOKEN}, status=status) as (config, received):
        with pytest.raises(NotificationError) as error:
            NotificationClient(config).send("sms", EVENT)
    assert TOKEN not in str(error.value)
    assert error.value.__suppress_context__
    assert len(received) == 1


@pytest.mark.parametrize("headers,body,expected", [
    ({"Retry-After": "15"}, {"retry_after": 90}, 15),
    ({}, {"retry_after": 42}, 42),
    ({"Retry-After": "invalid-secret"}, {"retry_after": False}, 60),
    ({}, {"retry_after": -1}, 60),
    ({}, b"private-error", 60),
])
def test_sms_test_propagates_only_safe_rate_limit_metadata(headers, body, expected):
    with gateway_server(body, status=429, headers=headers) as (config, received):
        with pytest.raises(NotificationRateLimited) as error:
            NotificationClient(config).send_sms_test("test")
    assert error.value.retry_after == expected
    assert "private-error" not in str(error.value)
    assert "invalid-secret" not in str(error.value)
    assert len(received) == 1


def test_oversized_response_is_rejected():
    body = b'{"status":"accepted","padding":"' + b"x" * notification_client.MAX_RESPONSE_BYTES + b'"}'
    with gateway_server(body) as (config, _):
        with pytest.raises(NotificationError, match="invalid response"):
            NotificationClient(config).send("sms", EVENT)


def test_network_errors_do_not_expose_token_or_retry(monkeypatch):
    calls = []

    class BrokenOpener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            raise OSError("private-network-detail " + TOKEN)

    monkeypatch.setattr(notification_client, "build_opener", lambda *_: BrokenOpener())
    client = NotificationClient({"url": "http://gateway", "token": TOKEN, "timeout_seconds": 12})
    with pytest.raises(NotificationError) as error:
        client.send("sms", EVENT)
    assert len(calls) == 1
    assert calls[0][1] == 12
    assert "private-network-detail" not in str(error.value)
    assert TOKEN not in str(error.value)
    assert error.value.__suppress_context__
