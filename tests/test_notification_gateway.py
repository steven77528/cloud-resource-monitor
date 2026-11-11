from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from http.client import HTTPResponse
import json
import socket
import threading

import pytest
import yaml

from cloud_monitor import notification_gateway as gateway
from cloud_monitor.notifiers import NotificationError


TOKEN = "gateway-admin-token-only-for-tests"
SECRET = "do-not-expose-this-secret"


@pytest.fixture
def config():
    return {
        "version": 1,
        "api": {"token": TOKEN, "host": "127.0.0.1", "port": 8081},
        "routes": {"P0": "sms", "P1": "dingtalk", "P2": "email"},
        "notifications": {
            "sms": {"enabled": False, "provider": "http", "url": ""},
            "dingtalk": {"enabled": True, "webhook": "https://example.test/robot", "secret": SECRET},
            "email": {"enabled": True, "host": "example.test", "from_address": "from@example.test",
                      "to": ["to@example.test"]},
        },
    }


def write_config(path, config):
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")


class FakeNotifier:
    calls = []
    error = None
    entered = None
    release = None

    def __init__(self, config):
        self.config = deepcopy(config)

    def send(self, channel, event):
        self.calls.append((channel, deepcopy(event), self.config))
        if self.error is not None:
            raise self.error

    def send_sms_test(self, content):
        self.calls.append(("sms-test", content, self.config))
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(5)
        if self.error is not None:
            raise self.error
        return "test-event-id"


@pytest.fixture
def fake_notifier(monkeypatch):
    FakeNotifier.calls = []
    FakeNotifier.error = None
    FakeNotifier.entered = None
    FakeNotifier.release = None
    monkeypatch.setattr(gateway, "Notifier", FakeNotifier)
    return FakeNotifier


@contextmanager
def running_http(path):
    server = gateway.make_server(path, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def request(port, path="/notify", payload=None, *, authorization=f"Bearer {TOKEN}",
            body=None, headers=(), content_type="application/json", length=None, method="POST"):
    if body is None:
        payload = payload if payload is not None else {"event": {"severity": "P1", "summary": "test"}}
        body = json.dumps(payload, ensure_ascii=False).encode()
    lines = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Connection: close"]
    if authorization is not None:
        lines.append(f"Authorization: {authorization}")
    if content_type is not None:
        lines.append(f"Content-Type: {content_type}")
    if length is not False:
        lines.append(f"Content-Length: {len(body) if length is None else length}")
    lines.extend(f"{name}: {value}" for name, value in headers)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body)
        response = HTTPResponse(connection)
        response.begin()
        return response.status, json.loads(response.read()), dict(response.getheaders())


@pytest.fixture
def config_path(tmp_path, config):
    path = tmp_path / "notifications.yaml"
    write_config(path, config)
    return path


def test_check_validates_enabled_channels_without_network(config_path, capsys):
    assert gateway.main(["--config", str(config_path), "check"]) == 0
    assert json.loads(capsys.readouterr().out) == {"valid": True}


def test_check_reports_invalid_configuration_without_secret_values(config_path, config, capsys):
    config["notifications"]["email"]["port"] = SECRET
    write_config(config_path, config)
    assert gateway.main(["--config", str(config_path), "check"]) == 2
    assert SECRET not in capsys.readouterr().err


@pytest.mark.parametrize("token", [None, "short", " " * 30, 123])
def test_short_or_missing_tokens_prevent_startup(config_path, config, token):
    config["api"]["token"] = token
    write_config(config_path, config)
    with pytest.raises(gateway.GatewayConfigError):
        gateway.make_server(config_path, host="127.0.0.1", port=0)


def test_full_event_is_forwarded_and_routes_and_recipients_reload(config_path, config, fake_notifier):
    event = {"severity": "P1", "summary": "\u8d44\u6e90\u5373\u5c06\u5230\u671f", "id": "event-id",
             "dimensions": {"identity": {"region": "test"}}, "rule_id": "expiry", "created_at": "test-time",
             "future_field": {"nested": "retained"}}
    with running_http(config_path) as port:
        assert request(port, payload={"event": event})[:2] == (200, {"status": "accepted", "channel": "dingtalk"})
        assert fake_notifier.calls[-1][1] == event
        config["routes"]["P1"] = "email"
        config["notifications"]["email"]["to"] = ["updated@example.test"]
        write_config(config_path, config)
        assert request(port, payload={"event": event})[:2] == (200, {"status": "accepted", "channel": "email"})
        assert fake_notifier.calls[-1][2]["email"]["to"] == ["updated@example.test"]
        config["api"]["token"] = "rotated-gateway-token-for-tests"
        write_config(config_path, config)
        assert request(port)[0] == 401
        assert request(port, authorization=f"Bearer {config['api']['token']}")[0] == 200


def test_selected_channel_errors_do_not_block_other_channels(config_path, config, fake_notifier):
    config["notifications"]["email"]["port"] = SECRET
    write_config(config_path, config)
    with running_http(config_path) as port:
        assert request(port)[0] == 200
        code, result, _ = request(port, payload={"event": {"severity": "P2"}})
        assert code == 503
        assert SECRET not in json.dumps(result)
        assert len(fake_notifier.calls) == 1


@pytest.mark.parametrize("channel,severity", [("sms", "P0"), ("dingtalk", "P1"), ("email", "P2")])
def test_disabled_channel_skips_without_sender_validation(config_path, config, fake_notifier, channel, severity):
    config["notifications"][channel] = {"enabled": False}
    write_config(config_path, config)
    with running_http(config_path) as port:
        assert request(port, payload={"event": {"severity": severity}})[:2] == (
            200, {"status": "skipped", "reason": "channel_disabled", "channel": channel})
        assert fake_notifier.calls == []


@pytest.mark.parametrize("authorization,path,headers", [
    (None, "/notify", ()),
    (TOKEN, "/notify", ()),
    ("Bearer wrong", "/notify", ()),
    (None, f"/notify?token={TOKEN}", ()),
    (f"Bearer {TOKEN}", "/notify", (("Authorization", f"Bearer {TOKEN}"),)),
])
def test_requires_single_bearer_token(config_path, fake_notifier, authorization, path, headers):
    with running_http(config_path) as port:
        assert request(port, path, authorization=authorization, headers=headers)[0] == 401
        assert fake_notifier.calls == []


@pytest.mark.parametrize("payload", [
    [], {}, {"event": []}, {"event": {"severity": "P9"}}, {"event": {"severity": ["P1"]}},
    {"event": {"severity": "P1", "summary": {"value": "bad"}}},
    {"event": {"severity": "P1", "dimensions": []}},
    {"event": {"severity": "P1"}, "channel": "sms"},
    {"event": {"severity": "P1"}, "phone_numbers": ["13800000000"]},
])
def test_invalid_events_and_channel_overrides_are_rejected(config_path, fake_notifier, payload):
    with running_http(config_path) as port:
        assert request(port, payload=payload)[0] == 400
        assert fake_notifier.calls == []


@pytest.mark.parametrize("options,status", [
    ({"content_type": "text/plain"}, 415),
    ({"content_type": None}, 415),
    ({"headers": (("Content-Type", "application/json"),)}, 415),
    ({"headers": (("Content-Length", "18"),)}, 400),
    ({"headers": (("Transfer-Encoding", "chunked"),)}, 400),
    ({"length": False}, 400),
    ({"length": "-1"}, 400),
    ({"length": "+18"}, 400),
    ({"length": "999999999999"}, 400),
    ({"body": b""}, 413),
    ({"body": b"x" * (64 * 1024 + 1)}, 413),
    ({"body": b'{"event":'}, 400),
    ({"body": b'{"event":{"severity":"P1","summary":"\xff"}}'}, 400),
    ({"body": b'{"event":{"severity":"P1","summary":"\\ud800"}}'}, 400),
    ({"body": b'{"event":{"severity":"P1","value":NaN}}'}, 400),
    ({"body": b'{"event":{"severity":"P1","value":1e9999}}'}, 400),
    ({"body": b'{"event":{},"event":{}}'}, 400),
])
def test_request_boundaries(config_path, fake_notifier, options, status):
    with running_http(config_path) as port:
        assert request(port, **options)[0] == status
        assert fake_notifier.calls == []


def test_file_errors_are_fail_closed_and_health_remains_available(config_path, fake_notifier):
    with running_http(config_path) as port:
        config_path.write_text(f"api: [{SECRET}", encoding="utf-8")
        code, result, _ = request(port)
        assert code == 503
        assert SECRET not in json.dumps(result)
        config_path.unlink()
        assert request(port)[0] == 503
        assert request(port, "/healthz", method="GET", authorization=None)[:2] == (200, {"status": "ok"})
        assert fake_notifier.calls == []


def test_each_request_uses_one_complete_configuration_snapshot(config_path, config, fake_notifier, monkeypatch):
    snapshot = deepcopy(config)
    snapshot["routes"]["P1"] = "email"
    original = gateway.load_gateway_config

    def replace_after_read(path, **kwargs):
        result = original(path, **kwargs)
        write_config(path, snapshot)
        return result

    with running_http(config_path) as port:
        monkeypatch.setattr(gateway, "load_gateway_config", replace_after_read)
        assert request(port)[1]["channel"] == "dingtalk"
        assert request(port)[1]["channel"] == "email"


def test_environment_references_expand_on_each_reload(config_path, config, fake_notifier, monkeypatch):
    config["notifications"]["email"]["to"] = ["${GATEWAY_TEST_EMAIL}"]
    write_config(config_path, config)
    monkeypatch.setenv("GATEWAY_TEST_EMAIL", "first@example.test")
    with running_http(config_path) as port:
        assert request(port, payload={"event": {"severity": "P2"}})[0] == 200
        assert fake_notifier.calls[-1][2]["email"]["to"] == ["first@example.test"]
        monkeypatch.setenv("GATEWAY_TEST_EMAIL", "second@example.test")
        assert request(port, payload={"event": {"severity": "P2"}})[0] == 200
        assert fake_notifier.calls[-1][2]["email"]["to"] == ["second@example.test"]


@pytest.mark.parametrize("error,status", [
    (NotificationError(SECRET), 502), (RuntimeError(SECRET), 502), (ValueError(SECRET), 503),
])
def test_sender_errors_are_sanitized_and_not_retried(config_path, fake_notifier, error, status):
    fake_notifier.error = error
    with running_http(config_path) as port:
        code, result, _ = request(port)
        assert code == status
        assert SECRET not in json.dumps(result)
        assert len(fake_notifier.calls) == 1


def enable_http_sms(config_path, config):
    config["notifications"]["sms"].update({"url": "https://example.test/sms", "success_json": {"code": 0}})
    write_config(config_path, config)


def test_manual_sms_ignores_disabled_switch_preserves_content_and_does_not_limit_automatic_alerts(
    config_path, config, fake_notifier,
):
    enable_http_sms(config_path, config)
    content = " \u77ed\u4fe1\u6d4b\u8bd5\u5185\u5bb9\n\u8bf7\u786e\u8ba4 "
    with running_http(config_path) as port:
        code, result, _ = request(port, "/notifications/sms/test", {"content": content})
        assert code == 200
        assert result == {"status": "accepted", "channel": "sms", "event_id": "test-event-id"}
        assert fake_notifier.calls[0][1] == content
        assert request(port, "/notifications/sms/test", {"content": content})[0] == 429
        assert request(port)[0] == 200
        assert request(port)[0] == 200


@pytest.mark.parametrize("channel", ["dingtalk", "email"])
def test_other_manual_channels_ignore_disabled_switch(config_path, config, fake_notifier, channel):
    config["notifications"][channel]["enabled"] = False
    write_config(config_path, config)
    with running_http(config_path) as port:
        code, result, _ = request(port, f"/notifications/{channel}/test", {"content": "manual test"})
        assert code == 200
        assert result["channel"] == channel
        assert fake_notifier.calls[0][1]["summary"] == "manual test"
        assert fake_notifier.calls[0][1]["phase"] == "test"


@pytest.mark.parametrize("payload", [{}, {"content": ""}, {"content": None}, {"content": "x" * 1001},
                                     {"content": "test", "phone_numbers": ["13800000000"]}])
def test_invalid_manual_requests_do_not_consume_cooldown(config_path, config, fake_notifier, payload):
    enable_http_sms(config_path, config)
    with running_http(config_path) as port:
        assert request(port, "/notifications/sms/test", payload)[0] == 400
        assert request(port, "/notifications/sms/test", {"content": "valid"})[0] == 200


def test_manual_failures_are_sanitized_and_consume_cooldown(config_path, config, fake_notifier):
    enable_http_sms(config_path, config)
    fake_notifier.error = NotificationError(SECRET)
    with running_http(config_path) as port:
        code, result, _ = request(port, "/notifications/sms/test", {"content": "valid"})
        assert code == 502
        assert SECRET not in json.dumps(result)
        code, result, headers = request(port, "/notifications/sms/test", {"content": "valid"})
        assert code == 429
        assert 1 <= int(headers["Retry-After"]) <= 60
        assert result["retry_after"] == int(headers["Retry-After"])
        assert len(fake_notifier.calls) == 1


def test_manual_sms_respects_lower_configured_content_limit(config_path, config, fake_notifier):
    config["notifications"]["sms"]["max_content_length"] = 10
    enable_http_sms(config_path, config)
    with running_http(config_path) as port:
        code, result, _ = request(port, "/notifications/sms/test", {"content": "x" * 11})
        assert code == 400
        assert result["error"] == "content exceeds the configured SMS content length limit"
        assert fake_notifier.calls == []


def test_manual_cooldown_is_per_instance_and_uses_monotonic_time(config_path, config, fake_notifier, monkeypatch):
    enable_http_sms(config_path, config)
    moment = [1000.0]
    monkeypatch.setattr(gateway.time, "monotonic", lambda: moment[0])
    with running_http(config_path) as first, running_http(config_path) as second:
        assert request(first, "/notifications/sms/test", {"content": "test"})[0] == 200
        assert request(second, "/notifications/sms/test", {"content": "test"})[0] == 200
        moment[0] += 59
        assert request(first, "/notifications/sms/test", {"content": "test"})[0] == 429
        moment[0] += 1
        assert request(first, "/notifications/sms/test", {"content": "test"})[0] == 200


def test_slow_sms_test_prevents_duplicate_but_does_not_block_other_channels(
    config_path, config, fake_notifier, monkeypatch,
):
    enable_http_sms(config_path, config)
    fake_notifier.entered = threading.Event()
    fake_notifier.release = threading.Event()
    moment = [1000.0]
    monkeypatch.setattr(gateway.time, "monotonic", lambda: moment[0])
    with running_http(config_path) as port, ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request, port, "/notifications/sms/test", {"content": "test"})
        try:
            assert fake_notifier.entered.wait(5)
            moment[0] += 61
            assert request(port, "/notifications/sms/test", {"content": "duplicate"})[0] == 429
            assert request(port, "/notifications/email/test", {"content": "email test"})[0] == 200
            assert request(port)[0] == 200
        finally:
            fake_notifier.release.set()
        assert first.result(timeout=5)[0] == 200
        assert len([row for row in fake_notifier.calls if row[0] == "sms-test"]) == 1
