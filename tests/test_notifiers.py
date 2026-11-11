import base64
from email import policy
from email.parser import BytesParser
import hashlib
import hmac
import json
import sys
import types
from urllib.parse import parse_qs, urlsplit

import pytest

from cloud_monitor import notifiers
from cloud_monitor.notification_banners import BANNER_CID, banner_bytes
from cloud_monitor.notification_rendering import BRAND_TITLE
from cloud_monitor.notifiers import NotificationError, Notifier, validate_notifications


STATE_CASES = [
    ("P0", "firing", "P0"),
    ("P1", "firing", "P1"),
    ("P2", "firing", "P2"),
    ("P0", "recovery", "recovery"),
    ("P1", "recovery", "recovery"),
    ("P2", "recovery", "recovery"),
]


@pytest.fixture
def event():
    return {"id": "event-123", "phase": "firing", "severity": "P0", "provider": "aliyun",
            "account": "production", "resource_id": "db-1", "resource_name": "database",
            "metric": "expiry_days", "value": 0, "unit": "days", "summary": "resource expired",
            "observed_at": "2026-09-08T01:00:00Z"}


def test_dingtalk_signing_preserves_access_token(monkeypatch, event):
    observed = {}
    monkeypatch.setattr(notifiers.time, "time", lambda: 1234.5)

    def post(url, payload, timeout):
        observed.update(url=url, payload=payload, timeout=timeout)
        return {"errcode": 0}

    monkeypatch.setattr(notifiers, "_post_json", post)
    Notifier({"dingtalk": {"webhook": "https://oapi.dingtalk.com/robot/send?access_token=token",
                           "secret": "test-secret", "keyword": "cloud-alert"}}).send("dingtalk", event)
    query = parse_qs(urlsplit(observed["url"]).query)
    expected = base64.b64encode(hmac.new(b"test-secret", b"1234500\ntest-secret", hashlib.sha256).digest()).decode()
    assert query == {"access_token": ["token"], "timestamp": ["1234500"], "sign": [expected]}
    assert observed["payload"]["msgtype"] == "markdown"
    assert observed["payload"]["markdown"]["title"] == BRAND_TITLE
    assert "cloud-alert" in observed["payload"]["markdown"]["text"]
    assert "**云平台/账号：**" in observed["payload"]["markdown"]["text"]
    assert "event-123" not in observed["payload"]["markdown"]["text"]
    assert observed["timeout"] == 15


@pytest.mark.parametrize("severity,phase,key", STATE_CASES)
def test_dingtalk_transport_uses_configured_image_for_each_state(monkeypatch, event, severity, phase, key):
    observed = []
    event.update(severity=severity, phase=phase)
    urls = {state: f"https://images.example.test/{state}.png" for state in ("P0", "P1", "P2", "recovery")}

    def post(url, payload, timeout):
        observed.append(payload)
        return {"errcode": 0}

    monkeypatch.setattr(notifiers, "_post_json", post)
    Notifier({"dingtalk": {"webhook": "https://example.test/robot", "keyword": "cloud_alert",
                           "banner_urls": urls}}).send("dingtalk", event)
    assert len(observed) == 1
    markdown = observed[0]["markdown"]
    assert markdown["title"] == BRAND_TITLE
    assert markdown["text"].splitlines()[0].endswith(f"]({urls[key]})")
    assert markdown["text"].splitlines()[0].startswith("![")
    assert "cloud_alert" in markdown["text"]
    assert "resource expired" in markdown["text"]
    assert "已恢复" in markdown["text"] if phase == "recovery" else "告警触发" in markdown["text"]


@pytest.mark.parametrize("response", [{"errcode": 310000, "errmsg": "secret-value"}, {}])
def test_dingtalk_response_failure_is_not_silently_accepted(monkeypatch, event, response):
    monkeypatch.setattr(notifiers, "_post_json", lambda *args: response)
    with pytest.raises(NotificationError, match="dingtalk rejected") as error:
        Notifier({"dingtalk": {"webhook": "https://example.com/robot?secret=secret-value"}}).send("dingtalk", event)
    assert "secret-value" not in str(error.value)


def test_network_exception_does_not_expose_signed_url(monkeypatch, event):
    def fail(*args):
        raise OSError("https://example.com/?secret=private-secret")

    monkeypatch.setattr(notifiers, "_post_json", fail)
    with pytest.raises(NotificationError) as error:
        Notifier({"dingtalk": {"webhook": "https://example.com"}}).send("dingtalk", event)
    assert "private-secret" not in str(error.value)
    assert error.value.__suppress_context__


def test_template_variables_are_literal_and_bounded(event):
    config = {"template_params": {"content": "{resource_id}:{summary}"}, "max_param_length": 14}
    assert notifiers._render_params(config, event) == {"content": "db-1:resource "}


@pytest.mark.parametrize("template", ["{resource_id.__class__}", "{unknown}", "{summary[0]}", "{summary:1000}"])
def test_template_rejects_expressions_and_unknown_fields(template):
    config = _sms_config()
    config["template_params"] = {"content": template}
    with pytest.raises(ValueError, match="invalid event placeholder"):
        validate_notifications({"sms": config}, ["sms"])


def _sms_config():
    return {"provider": "aliyun", "access_key_id": "test-id", "access_key_secret": "test-secret",
            "sign_name": "approved-sign", "template_code": "SMS_123", "phone_numbers": ["13800000000"],
            "template_params": {"resource": "{resource_id}", "value": "{value}"}}


def test_aliyun_sms_uses_real_api_contract(monkeypatch, event):
    from cloud_monitor import aliyun_compat

    observed = {}
    monkeypatch.setattr(aliyun_compat, "ensure_aliyun_tls_compatibility",
                        lambda: observed.update(tls_ready=True))

    class Client:
        def __init__(self, *args, **kwargs):
            assert observed.get("tls_ready")
            observed["options"] = kwargs

        def do_action_with_exception(self, request):
            observed["params"] = request.params
            return b'{"Code":"OK"}'

    class CommonRequest:
        def __init__(self):
            self.params = {}

        def __getattr__(self, name):
            if name.startswith("set_"):
                return lambda value: observed.update({name: value})
            raise AttributeError(name)

        def add_query_param(self, name, value):
            self.params[name] = value

    monkeypatch.setitem(sys.modules, "aliyunsdkcore.client", types.SimpleNamespace(AcsClient=Client))
    monkeypatch.setitem(sys.modules, "aliyunsdkcore.request", types.SimpleNamespace(CommonRequest=CommonRequest))
    Notifier({"sms": _sms_config()}).send("sms", event)
    assert observed["set_action_name"] == "SendSms"
    assert observed["set_domain"] == "dysmsapi.aliyuncs.com"
    assert observed["set_protocol_type"] == "https"
    assert observed["params"]["PhoneNumbers"] == "13800000000"
    assert json.loads(observed["params"]["TemplateParam"]) == {"resource": "db-1", "value": "0"}
    assert observed["options"]["auto_retry"] is False
    assert observed["options"]["timeout"] == 15


def test_aliyun_rejects_non_ok_response_without_exposing_provider_details(monkeypatch, event):
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def do_action_with_exception(self, request):
            return b'{"Code":"isv.BUSINESS_LIMIT_CONTROL","Message":"private-details"}'

    class CommonRequest:
        def __getattr__(self, name):
            return lambda *args: None

    monkeypatch.setitem(sys.modules, "aliyunsdkcore.client", types.SimpleNamespace(AcsClient=Client))
    monkeypatch.setitem(sys.modules, "aliyunsdkcore.request", types.SimpleNamespace(CommonRequest=CommonRequest))
    with pytest.raises(NotificationError, match="sms provider rejected") as error:
        Notifier({"sms": _sms_config()}).send("sms", event)
    assert "private-details" not in str(error.value)


@pytest.mark.parametrize("security", ["ssl", "starttls"])
@pytest.mark.parametrize("severity,phase,key", STATE_CASES)
def test_email_tls_utf8_and_envelope(monkeypatch, event, security, severity, phase, key):
    observed = {"ehlo": 0}

    class SMTP:
        def __init__(self, **kwargs):
            observed["options"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            observed["ehlo"] += 1

        def starttls(self, **kwargs):
            observed["starttls"] = True

        def login(self, username, password):
            observed["auth"] = (username, password)

        def send_message(self, message, **kwargs):
            observed["message"] = message
            observed["envelope"] = kwargs
            return {}

    monkeypatch.setattr(notifiers.smtplib, "SMTP_SSL", SMTP)
    monkeypatch.setattr(notifiers.smtplib, "SMTP", SMTP)
    event.update(severity=severity, phase=phase)
    event["summary"] = "\u4e91\u8d44\u6e90\u5230\u671f\r\nSubject: injected"
    config = {"host": "smtp.example.com", "port": 465 if security == "ssl" else 587,
              "security": security, "username": "sender", "password": "secret",
              "from_address": "sender@example.com", "to": ["ops@example.com"]}
    Notifier({"email": config}).send("email", event)
    message = BytesParser(policy=policy.default).parsebytes(observed["message"].as_bytes())
    text = message.get_body(preferencelist=("plain",))
    html = message.get_body(preferencelist=("html",))
    assert message.get_content_type() == "multipart/alternative"
    alternatives = list(message.iter_parts())
    assert [part.get_content_type() for part in alternatives] == ["text/plain", "multipart/related"]
    related = list(alternatives[1].iter_parts())
    assert [part.get_content_type() for part in related] == ["text/html", "image/png"]
    image = related[1]
    assert image.get_content_disposition() == "inline"
    assert image["Content-ID"] == f"<{BANNER_CID}>"
    assert image.get_payload(decode=True) == banner_bytes(key)
    assert image.get_payload(decode=True).startswith(b"\x89PNG\r\n\x1a\n")
    assert f'src="cid:{BANNER_CID}"' in html.get_content()
    assert html.get_content().count("<img ") == 1
    assert '<img src="http' not in html.get_content()
    assert text.get_content_charset() == html.get_content_charset() == "utf-8"
    assert "\u4e91\u8d44\u6e90\u5230\u671f" in text.get_content()
    assert "font-weight:700" in html.get_content()
    assert "事件编号" not in html.get_content() + text.get_content()
    status = "已恢复" if phase == "recovery" else "告警触发"
    assert str(message["Subject"]).startswith(f"{BRAND_TITLE} | {status}")
    assert severity not in str(message["Subject"]) + html.get_content() + text.get_content()
    assert len(message.get_all("Subject")) == 1
    assert "\n" not in str(message["Subject"])
    assert observed["envelope"]["to_addrs"] == ["ops@example.com"]
    assert observed["ehlo"] == (2 if security == "starttls" else 1)
    assert bool(observed.get("starttls")) == (security == "starttls")


def test_huawei_sms_wsse_and_per_recipient_acceptance(monkeypatch, event):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps({"code": "000000", "result": [{"status": "E000500"}]}).encode()

    def open_url(request, timeout):
        captured["request"] = request
        return Response()

    monkeypatch.setattr(notifiers, "urlopen", open_url)
    monkeypatch.setattr(notifiers.secrets, "token_hex", lambda size: "nonce")
    config = {"provider": "huawei", "api_endpoint": "https://smsapi.example.com/sms/batchSendSms/v1",
              "app_key": "app-key", "app_secret": "app-secret", "sender": "sender",
              "template_id": "template", "phone_numbers": ["+8613800000000"],
              "template_params": ["{resource_id}", "{value}"]}
    with pytest.raises(NotificationError, match="not accepted for all recipients"):
        Notifier({"sms": config}).send("sms", event)
    request = captured["request"]
    assert 'Username="app-key"' in request.headers["X-wsse"]
    assert "app-secret" not in request.headers["X-wsse"]
    params = parse_qs(request.data.decode())
    assert json.loads(params["templateParas"][0]) == ["db-1", "0"]


def test_huawei_sms_signature_uses_raw_digest(monkeypatch, event):
    from datetime import datetime, timezone

    captured = {}

    class FixedDatetime:
        @staticmethod
        def now(tz):
            return datetime(2026, 9, 8, 1, 0, tzinfo=timezone.utc)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return b'{"code":"000000","result":[{"status":"000000"}]}'

    def open_url(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(notifiers, "urlopen", open_url)
    monkeypatch.setattr(notifiers, "datetime", FixedDatetime)
    monkeypatch.setattr(notifiers.secrets, "token_hex", lambda size: "fixed-nonce")
    config = {"provider": "huawei", "api_endpoint": "https://smsapi.example.com/sms/batchSendSms/v1",
              "app_key": "app-key", "app_secret": "app-secret", "sender": "sender",
              "template_id": "template", "phone_numbers": ["+8613800000000"],
              "template_params": ["{resource_id}", "{phase}"]}
    Notifier({"sms": config}).send("sms", event)
    expected = base64.b64encode(hashlib.sha256(b"fixed-nonce2026-09-08T01:00:00Zapp-secret").digest()).decode()
    assert f'PasswordDigest="{expected}"' in captured["request"].headers["X-wsse"]
    assert captured["timeout"] == 15


def test_email_partial_refusal_is_a_delivery_failure(monkeypatch, event):
    class SMTP:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            pass

        def send_message(self, message, **kwargs):
            return {"private-recipient@example.com": (550, b"private-details")}

    monkeypatch.setattr(notifiers.smtplib, "SMTP_SSL", SMTP)
    config = {"host": "smtp.example.com", "from_address": "sender@example.com",
              "to": ["ops@example.com", "private-recipient@example.com"]}
    with pytest.raises(NotificationError, match="one or more recipients") as error:
        Notifier({"email": config}).send("email", event)
    assert "private-recipient" not in str(error.value)
    assert "private-details" not in str(error.value)


def test_only_configured_route_channels_are_validated():
    validate_notifications({"dingtalk": {"webhook": "https://example.com"}, "sms": {}}, ["dingtalk"])
    with pytest.raises(ValueError, match="phone_numbers"):
        validate_notifications({"sms": {}}, ["sms"])


@pytest.mark.parametrize("config", [
    {"webhook": "http://example.com"},
    {"webhook": "https://user:password@example.com"},
    {"webhook": "https://example.com", "timeout_seconds": 0},
])
def test_reject_invalid_dingtalk_config(config):
    with pytest.raises(ValueError):
        validate_notifications({"dingtalk": config}, ["dingtalk"])


@pytest.mark.parametrize("banner_urls", [
    None, [], "https://images.example.test/private-banner-token.png",
    {"unknown-private-banner-token": "https://images.example.test/unused.png"},
    {"P0": None}, {"P1": 123}, {"P2": False}, {"recovery": {}},
    {"P1": "http://images.example.test/private-banner-token.png"},
    {"P1": "data:image/png;base64,private-banner-token"},
    {"P1": "javascript:private-banner-token"},
    {"P1": "https://private-banner-token@images.example.test/banner.png"},
    {"P1": "https://user:private-banner-token@images.example.test/banner.png"},
    {"P1": "https://images.example.test/banner.png#private-banner-token"},
    {"P1": " https://images.example.test/private-banner-token.png"},
    {"P1": "https://images.example.test/private banner-token.png"},
    {"P1": "https://images.example.test/private-banner-token.png\n"},
    {"P1": "https://images.example.test/private-banner-token.png\r\nInjected: value"},
    {"P1": "https://images.example.test/\x00private-banner-token.png"},
    {"P1": "https://images.example.test/\x7fprivate-banner-token.png"},
    {"P1": "https://[private-banner-token/banner.png"},
    {"P1": "https://images.example.test:private-banner-token/banner.png"},
    {"P1": "https://images.example.test:99999/private-banner-token.png"},
    {"P1": "/private-banner-token/banner.png"},
])
def test_banner_url_validation_rejects_invalid_settings_without_leaking_values(banner_urls):
    config = {"dingtalk": {"webhook": "https://example.test/robot", "banner_urls": banner_urls}}
    with pytest.raises(ValueError, match=r"^notifications\.dingtalk\.banner_urls ") as error:
        validate_notifications(config, ["dingtalk"])
    assert "private-banner-token" not in str(error.value)
    assert "images.example.test" not in str(error.value)


@pytest.mark.parametrize("banner_urls", [
    {}, {"P0": "", "P1": "", "P2": "", "recovery": ""},
    {"P1": "https://images.example.test/banner.png"},
    {"recovery": "https://images.example.test/%E6%81%A2%E5%A4%8D.png?version=1&token=synthetic"},
    {"P2": "https://images.example.test/banner%20blue%28new%29.png?name=%5Bimage%5D"},
])
def test_banner_url_validation_accepts_optional_partial_and_uploaded_image_urls(banner_urls):
    validate_notifications({"dingtalk": {"webhook": "https://example.test/robot", "banner_urls": banner_urls}},
                           ["dingtalk"])
