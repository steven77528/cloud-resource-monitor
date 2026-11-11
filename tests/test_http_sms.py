import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs
from uuid import UUID

import pytest

from cloud_monitor import notifiers
from cloud_monitor.notifiers import (
    NotificationDisabled,
    NotificationError,
    Notifier,
    sms_enabled,
    validate_notifications,
)


@pytest.fixture
def endpoint():
    state = {"requests": [], "status": 200, "response": b'{"code":0}', "headers": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["requests"].append({"path": self.path, "headers": dict(self.headers), "body": body})
            self.send_response(state["status"])
            for name, value in state["headers"].items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(state["response"])

        def do_GET(self):
            state["requests"].append({"path": self.path, "headers": dict(self.headers), "body": b""})
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/sms/send"
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def event():
    return {"id": "event-1", "phase": "firing", "severity": "P0", "provider": "huawei",
            "account": "production", "resource_id": "elb-1", "resource_name": "Public ELB",
            "metric": "bandwidth", "value": 100, "unit": "%", "summary": "Capacity exhausted",
            "observed_at": "2026-09-09T00:00:00Z"}


def config_for(endpoint, **overrides):
    return {"sms": {"provider": "http", "url": endpoint["url"], **overrides}}


def test_http_sender_renders_alert_and_recovery_with_exact_json(endpoint, event):
    config = config_for(endpoint, content_field="message",
                        headers={"Authorization": "Bearer test-token", "X-Tenant": "ops"},
                        extra_body={"recipients": ["13800000000"], "options": {"priority": 1}},
                        templates={"firing": "[{severity}] {summary} {resource_id} {value}{unit}",
                                   "recovery": "Recovered: {resource_name}"})
    notifier = Notifier(config)
    notifier.send("sms", event)
    notifier.send("sms", {**event, "phase": "recovery"})
    firing, recovery = endpoint["requests"]
    assert firing["path"] == "/sms/send"
    assert firing["headers"]["Authorization"] == "Bearer test-token"
    assert firing["headers"]["X-Tenant"] == "ops"
    assert firing["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(firing["body"]) == {
        "message": "[P0] Capacity exhausted elb-1 100%",
        "recipients": ["13800000000"], "options": {"priority": 1},
    }
    assert json.loads(recovery["body"])["message"] == "Recovered: Public ELB"


def test_sms_disabled_skips_config_and_network_but_manual_send_remains_available(endpoint, event):
    validate_notifications({"sms": {"enabled": False}}, ["sms"])
    with pytest.raises(NotificationDisabled):
        Notifier({"sms": {"enabled": False}}).send("sms", event)
    config = config_for(endpoint, enabled=False)
    notifier = Notifier(config)
    with pytest.raises(NotificationDisabled):
        notifier.send("sms", event)
    assert endpoint["requests"] == []
    content = "\u6062\u590d\u901a\u77e5\uff1a{summary}\n\u8bf7\u6838\u5bf9\u4e91\u8d44\u6e90\u4f59\u989d"
    event_id = notifier.send_sms_test(content)
    assert str(UUID(event_id)) == event_id
    assert len(endpoint["requests"]) == 1
    assert json.loads(endpoint["requests"][0]["body"]) == {"content": content}
    assert content.encode("utf-8") in endpoint["requests"][0]["body"].replace(b"\\n", b"\n")
    assert config["sms"]["enabled"] is False


def test_form_sender_posts_utf8_content_and_static_fields(endpoint):
    config = config_for(endpoint, encoding="form", content_field="text",
                        extra_body={"apikey": "test-api-key", "mobile": "13800000000"})
    content = "\u3010\u544a\u8b66\u3011\u4e91\u8d44\u6e90\u4f59\u989d\u4e0d\u8db3 & + ="
    Notifier(config).send_sms_test(content)
    request = endpoint["requests"][0]
    assert request["headers"]["Content-Type"] == "application/x-www-form-urlencoded; charset=utf-8"
    assert parse_qs(request["body"].decode("utf-8")) == {
        "apikey": ["test-api-key"], "mobile": ["13800000000"], "text": [content],
    }
    endpoint["response"] = b'{"code":"0"}'
    with pytest.raises(NotificationError):
        Notifier(config).send_sms_test(content)


@pytest.mark.parametrize("response", [b'{"code":1}', b'{"code":false}', b'{"code":"0"}',
                                      b'{"code":0.0}', b'{}', b'[]', b'not-json'])
def test_http_success_requires_expected_json_type_and_value(endpoint, event, response):
    endpoint["response"] = response
    with pytest.raises(NotificationError):
        Notifier(config_for(endpoint)).send("sms", event)
    assert len(endpoint["requests"]) == 1


def test_http_success_matches_custom_fields_and_nested_types(endpoint, event):
    config = config_for(endpoint, success_json={"status": "ok", "details": [0, {"accepted": True}]})
    endpoint["response"] = b'{"status":"ok","details":[0,{"accepted":true}],"id":"123"}'
    Notifier(config).send("sms", event)
    endpoint["response"] = b'{"status":"ok","details":[false,{"accepted":true}]}'
    with pytest.raises(NotificationError):
        Notifier(config).send("sms", event)


@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
def test_empty_success_json_accepts_http_2xx_without_json(endpoint, event, status):
    endpoint.update(status=status, response=b"accepted")
    Notifier(config_for(endpoint, success_json={})).send("sms", event)
    assert len(endpoint["requests"]) == 1


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_http_failure_never_exposes_remote_details_or_retries(endpoint, status):
    endpoint.update(status=status, response=b"private-provider-details")
    config = config_for(endpoint, url=endpoint["url"] + "?key=private-url-token",
                        headers={"Authorization": "Bearer private-header-token"})
    with pytest.raises(NotificationError) as caught:
        Notifier(config).send_sms_test("private-message-content")
    message = str(caught.value)
    assert all(secret not in message for secret in ["private-provider-details", "private-url-token",
                                                   "private-header-token", "private-message-content"])
    assert caught.value.__suppress_context__
    assert len(endpoint["requests"]) == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_is_not_followed_or_forwarded(endpoint, event, status):
    endpoint.update(status=status, headers={"Location": endpoint["url"] + "/redirect-target"})
    config = config_for(endpoint, headers={"Authorization": "Bearer private-auth"}, success_json={})
    with pytest.raises(NotificationError):
        Notifier(config).send("sms", event)
    assert len(endpoint["requests"]) == 1
    assert endpoint["requests"][0]["path"] == "/sms/send"


def test_network_errors_are_redacted(monkeypatch, endpoint, event):
    def fail(*args):
        raise OSError("URL token=private-url; Authorization=private-header; body=private-message")

    monkeypatch.setattr(notifiers, "build_opener", fail)
    with pytest.raises(NotificationError) as caught:
        Notifier(config_for(endpoint)).send("sms", event)
    assert "private" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_chinese_content_character_limit_rejects_without_truncation(endpoint, event):
    notifier = Notifier(config_for(endpoint, max_content_length=1000))
    content = "\u544a" * 1000
    notifier.send_sms_test(content)
    assert json.loads(endpoint["requests"][0]["body"])["content"] == content
    with pytest.raises(ValueError, match="1000"):
        notifier.send_sms_test(content + "\u8b66")
    with pytest.raises(NotificationError, match="character limit"):
        notifier.send("sms", {**event, "summary": content})
    assert len(endpoint["requests"]) == 1


@pytest.mark.parametrize("content", [None, 123, "", " \t\n"])
def test_manual_content_requires_nonempty_string(endpoint, content):
    with pytest.raises(ValueError, match="content"):
        Notifier(config_for(endpoint)).send_sms_test(content)
    assert endpoint["requests"] == []


def test_manual_request_respects_configured_shorter_limit(endpoint):
    with pytest.raises(ValueError, match="max_content_length"):
        Notifier(config_for(endpoint, max_content_length=3)).send_sms_test("long")
    assert endpoint["requests"] == []


def test_manual_send_validates_disabled_config_and_requires_http_provider():
    with pytest.raises(ValueError, match="url"):
        Notifier({"sms": {"provider": "http", "enabled": False}}).send_sms_test("test")
    with pytest.raises(ValueError, match="provider: http"):
        Notifier({"sms": {"provider": "aliyun", "enabled": False}}).send_sms_test("test")


@pytest.mark.parametrize("enabled", ["false", 0, 1, None, [], {}])
def test_enabled_must_be_boolean_even_with_incomplete_config(enabled):
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        validate_notifications({"sms": {"enabled": enabled}}, ["sms"])


def test_enabled_defaults_to_true_for_existing_configs():
    assert sms_enabled({}) is True
    assert sms_enabled({"sms": {"provider": "aliyun"}}) is True


@pytest.mark.parametrize("overrides", [
    {"encoding": "xml"},
    {"encoding": []},
    {"encoding": "form", "extra_body": {"nested": {"recipient": "13800000000"}}},
    {"url": "ftp://example.com/send"},
    {"url": "https://user:secret@example.com/send"},
    {"url": "https://example.com/send\r\n"},
    {"url": "https://example.com:bad/send"},
    {"headers": {"Host": "example.org"}},
    {"headers": {"CONTENT-LENGTH": "0"}},
    {"headers": {"Transfer-Encoding": "chunked"}},
    {"headers": {"Authorization": "Bearer test\r\nX-Injected: 1"}},
    {"headers": {"Invalid Header": "value"}},
    {"headers": {"Authorization": 123}},
    {"headers": {"X-Key": "one", "x-key": "two"}},
    {"content_field": ""},
    {"content_field": []},
    {"extra_body": {"content": "overwrite"}},
    {"extra_body": {"nested": {1: "not-string"}}},
    {"extra_body": {"value": float("nan")}},
    {"success_json": []},
    {"templates": {"firing": "{unknown}", "recovery": "ok"}},
    {"templates": {"firing": "{summary.__class__}", "recovery": "ok"}},
    {"templates": {"firing": "ok"}},
    {"max_content_length": 0},
    {"max_content_length": 10001},
    {"max_content_length": True},
])
def test_http_config_rejects_invalid_contract(endpoint, overrides):
    with pytest.raises(ValueError):
        validate_notifications(config_for(endpoint, **overrides), ["sms"])
    assert endpoint["requests"] == []
