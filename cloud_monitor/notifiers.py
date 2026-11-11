"""Notification transports. Delivery errors deliberately exclude credentials."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import smtplib
import ssl
import time
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from .notification_banners import BANNER_CID, BANNER_FILES, banner_bytes, banner_key
from .notification_rendering import BRAND_TITLE, dingtalk_markdown, email_html, plain_body, present


EVENT_FIELDS = frozenset(
    {"id", "phase", "severity", "provider", "account", "resource_id", "resource_name",
     "metric", "value", "unit", "summary", "observed_at"}
)
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
HTTP_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
HTTP_SMS_TEMPLATES = {
    "firing": "[{severity}] {summary}; account={account}; resource={resource_id}; value={value} {unit}",
    "recovery": "[{severity}] Recovered: {summary}; account={account}; resource={resource_id}",
}


class NotificationError(RuntimeError):
    """A delivery did not succeed; the caller may retry through its outbox."""


class NotificationDisabled(NotificationError):
    """The channel is intentionally disabled and must not be retried."""


def sms_enabled(config: dict[str, Any]) -> bool:
    if not isinstance(config, dict):
        raise ValueError("notifications must be an object")
    section = config.get("sms", {})
    if not isinstance(section, dict):
        raise ValueError("notifications.sms must be an object")
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("notifications.sms.enabled must be a boolean")
    return enabled


def _required(section: dict[str, Any], key: str, path: str) -> None:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}.{key} must be a non-empty string")


def _recipients(section: dict[str, Any], key: str, path: str) -> list[str]:
    value = section.get(key)
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item.strip() or "\r" in item or "\n" in item
        for item in value
    ):
        raise ValueError(f"{path}.{key} must be a non-empty list of strings")
    return value


def _https_url(value: Any, path: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be an HTTPS URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{path} must be an HTTPS URL without user information")


def _positive_number(value: Any, path: str, maximum: float = 300) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum:
        raise ValueError(f"{path} must be greater than 0 and at most {maximum}")


def _banner_urls_config(value: Any, path: str) -> None:
    if not isinstance(value, dict) or set(value) - set(BANNER_FILES):
        raise ValueError(f"{path} must be an object with P0, P1, P2 or recovery keys")
    for url in value.values():
        if url == "":
            continue
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            valid = (parsed is not None and parsed.scheme == "https" and parsed.hostname
                     and parsed.username is None and parsed.password is None and not parsed.fragment
                     and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url))
            if parsed is not None:
                parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"{path} values must be HTTPS image URLs without user information or fragments")


def _validate_placeholder(value: str, path: str) -> None:
    names = set(PLACEHOLDER.findall(value))
    if names - EVENT_FIELDS or "{" in PLACEHOLDER.sub("", value) or "}" in PLACEHOLDER.sub("", value):
        raise ValueError(f"{path} contains an invalid event placeholder")


def _template_config(section: dict[str, Any], provider: str) -> None:
    params = section.get("template_params", {} if provider == "aliyun" else [])
    if provider == "aliyun":
        if not isinstance(params, dict) or any(not isinstance(key, str) for key in params):
            raise ValueError("notifications.sms.template_params must be an object for aliyun")
        values = params.values()
    else:
        if not isinstance(params, list):
            raise ValueError("notifications.sms.template_params must be an ordered list for huawei")
        values = params
    for value in values:
        if not isinstance(value, str):
            raise ValueError("notifications.sms.template_params values must be strings")
        _validate_placeholder(value, "notifications.sms.template_params")
    maximum = section.get("max_param_length", 64)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 1000:
        raise ValueError("notifications.sms.max_param_length must be an integer from 1 to 1000")


def _json_object(value: Any, path: str) -> None:
    def valid(item: Any) -> bool:
        if isinstance(item, dict):
            return all(isinstance(key, str) and valid(child) for key, child in item.items())
        if isinstance(item, list):
            return all(valid(child) for child in item)
        return item is None or type(item) in {str, int, float, bool}

    if not isinstance(value, dict) or not valid(value):
        raise ValueError(f"{path} must be a JSON object with string keys")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{path} must contain valid JSON values") from None


def _http_sms_config(section: dict[str, Any]) -> None:
    path = "notifications.sms"
    encoding = section.get("encoding", "json")
    if not isinstance(encoding, str) or encoding not in {"json", "form"}:
        raise ValueError(f"{path}.encoding must be json or form")
    _required(section, "url", path)
    url = section["url"]
    try:
        parsed = urlsplit(url)
        valid_url = (parsed.scheme in {"http", "https"} and parsed.hostname
                     and parsed.username is None and parsed.password is None
                     and not parsed.fragment and not any(char.isspace() for char in url))
        parsed.port
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError(f"{path}.url must be an HTTP(S) URL without user information or fragment")
    headers = section.get("headers", {})
    if not isinstance(headers, dict):
        raise ValueError(f"{path}.headers must be an object")
    seen_headers = set()
    for key, value in headers.items():
        if (not isinstance(key, str) or not HTTP_HEADER_NAME.fullmatch(key)
                or key.lower() in {"host", "content-length", "transfer-encoding"}
                or key.lower() in seen_headers
                or not isinstance(value, str) or "\r" in value or "\n" in value):
            raise ValueError(f"{path}.headers contains an invalid or reserved HTTP header")
        seen_headers.add(key.lower())
    content_field = section.get("content_field", "content")
    if not isinstance(content_field, str) or not content_field.strip():
        raise ValueError(f"{path}.content_field must be a non-empty string")
    extra_body = section.get("extra_body", {})
    _json_object(extra_body, f"{path}.extra_body")
    if encoding == "form" and any(type(value) not in {str, int, float, bool} for value in extra_body.values()):
        raise ValueError(f"{path}.extra_body values must be strings, numbers, or booleans for form encoding")
    if content_field in extra_body:
        raise ValueError(f"{path}.extra_body must not contain content_field")
    _json_object(section.get("success_json", {"code": 0}), f"{path}.success_json")
    templates = section.get("templates", HTTP_SMS_TEMPLATES)
    if not isinstance(templates, dict):
        raise ValueError(f"{path}.templates must be an object")
    for phase in ("firing", "recovery"):
        _required(templates, phase, f"{path}.templates")
        _validate_placeholder(templates[phase], f"{path}.templates.{phase}")
    maximum = section.get("max_content_length", 1000)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 10000:
        raise ValueError(f"{path}.max_content_length must be an integer from 1 to 10000")


def validate_notifications(config: dict[str, Any], channels: Iterable[str]) -> None:
    """Validate only used channels in the notifications section, before collection."""
    if not isinstance(config, dict):
        raise ValueError("notifications must be an object")
    for channel in set(channels):
        if channel not in {"sms", "dingtalk", "email"}:
            raise ValueError("notification channel must be sms, dingtalk, or email")
        section = config.get(channel)
        path = f"notifications.{channel}"
        if not isinstance(section, dict):
            raise ValueError(f"{path} must be configured")
        if channel == "sms" and not sms_enabled(config):
            continue
        _positive_number(section.get("timeout_seconds", 15), f"{path}.timeout_seconds")
        if channel == "dingtalk":
            _required(section, "webhook", path)
            _https_url(section["webhook"], f"{path}.webhook")
            if not isinstance(section.get("secret", ""), str):
                raise ValueError(f"{path}.secret must be a string")
            if not isinstance(section.get("keyword", ""), str):
                raise ValueError(f"{path}.keyword must be a string")
            _banner_urls_config(section.get("banner_urls", {}), f"{path}.banner_urls")
        elif channel == "email":
            for key in ("host", "from_address"):
                _required(section, key, path)
                if "\r" in section[key] or "\n" in section[key]:
                    raise ValueError(f"{path}.{key} must not contain line breaks")
            _recipients(section, "to", path)
            port = section.get("port", 465)
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError(f"{path}.port must be an integer from 1 to 65535")
            if section.get("security", "ssl") not in {"ssl", "starttls"}:
                raise ValueError(f"{path}.security must be ssl or starttls")
            if bool(section.get("username")) != bool(section.get("password")):
                raise ValueError(f"{path}.username and password must be configured together")
        else:
            provider = section.get("provider", "aliyun")
            if provider not in {"aliyun", "huawei", "http"}:
                raise ValueError(f"{path}.provider must be aliyun, huawei, or http")
            if provider == "http":
                _http_sms_config(section)
                continue
            _recipients(section, "phone_numbers", path)
            if provider == "aliyun":
                for key in ("access_key_id", "access_key_secret", "sign_name", "template_code"):
                    _required(section, key, path)
            else:
                for key in ("api_endpoint", "app_key", "app_secret", "sender", "template_id"):
                    _required(section, key, path)
                _https_url(section["api_endpoint"], f"{path}.api_endpoint")
                for key in ("app_key", "app_secret"):
                    if any(char in section[key] for char in ('"', "\r", "\n")):
                        raise ValueError(f"{path}.{key} contains invalid characters")
            _template_config(section, provider)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _body(event: dict[str, Any]) -> str:
    return plain_body(event)


def _render_params(config: dict[str, Any], event: dict[str, Any]) -> dict[str, str] | list[str]:
    maximum = config.get("max_param_length", 64)
    params = config.get("template_params", {} if config.get("provider", "aliyun") == "aliyun" else [])

    def render(template: str) -> str:
        # Substitute only allowlisted scalar fields; never evaluate attributes or expressions.
        result = PLACEHOLDER.sub(lambda match: _text(event.get(match.group(1))), template)
        return result[:maximum]

    if isinstance(params, dict):
        return {key: render(value) for key, value in params.items()}
    return [render(value) for value in params]


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read(1024 * 1024).decode("utf-8"))
    if not isinstance(result, dict):
        raise NotificationError("dingtalk returned an invalid response")
    return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _same_json(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _same_json(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _same_json(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


class Notifier:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    def send(self, channel: str, event: dict[str, Any]) -> None:
        """Deliver once. Successful return means the provider accepted the request."""
        validate_notifications(self.config, [channel])
        if channel == "sms" and not sms_enabled(self.config):
            raise NotificationDisabled("sms notifications are disabled")
        try:
            if channel == "sms":
                self._sms(event)
            elif channel == "dingtalk":
                self._dingtalk(event)
            else:
                self._email(event)
        except NotificationError:
            raise
        except Exception:
            # SDK/network exceptions may contain signed URLs, credentials, or recipients.
            raise NotificationError(f"{channel} delivery failed; check transport configuration and connectivity") from None

    def send_sms_test(self, content: str) -> str:
        """Send supplied content once using the configured HTTP endpoint, even when paused."""
        if not isinstance(content, str) or not content.strip() or len(content) > 1000:
            raise ValueError("content must be a non-empty string of at most 1000 characters")
        sms_enabled(self.config)
        config = self.config.get("sms", {})
        if config.get("provider", "aliyun") != "http":
            raise ValueError("manual SMS content requires notifications.sms.provider: http")
        enabled_config = {**self.config, "sms": {**config, "enabled": True}}
        validate_notifications(enabled_config, ["sms"])
        if len(content) > config.get("max_content_length", 1000):
            raise ValueError("content exceeds notifications.sms.max_content_length")
        event_id = str(uuid.uuid4())
        Notifier(enabled_config).send("sms", {
            "id": event_id, "phase": "test", "severity": "P0", "provider": "manual",
            "account": "manual", "resource_id": "sms-test", "resource_name": "SMS test",
            "metric": "sms_test", "value": "", "unit": "", "summary": content,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        })
        return event_id

    def _dingtalk(self, event: dict[str, Any]) -> None:
        config = self.config["dingtalk"]
        webhook = config["webhook"]
        secret = config.get("secret", "")
        if secret:
            timestamp = str(int(time.time() * 1000))
            signature = base64.b64encode(hmac.new(
                secret.encode("utf-8"), f"{timestamp}\n{secret}".encode("utf-8"), hashlib.sha256
            ).digest()).decode("ascii")
            parsed = urlsplit(webhook)
            query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                     if key not in {"timestamp", "sign"}]
            query.extend([("timestamp", timestamp), ("sign", signature)])
            webhook = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
        markdown = dingtalk_markdown(event, config.get("keyword", ""), config.get("banner_urls"))
        result = _post_json(webhook, {"msgtype": "markdown", "markdown": markdown},
                            config.get("timeout_seconds", 15))
        if result.get("errcode") != 0:
            raise NotificationError("dingtalk rejected the message; check robot security settings and rate limits")

    def _email(self, event: dict[str, Any]) -> None:
        config = self.config["email"]
        message = EmailMessage()
        message["From"] = config["from_address"]
        message["To"] = ", ".join(config["to"])
        subject = f"{BRAND_TITLE} | {present(event).title}"
        message["Subject"] = subject.replace("\r", " ").replace("\n", " ")[:240]
        message.set_content(_body(event), charset="utf-8")
        message.add_alternative(email_html(event), subtype="html", charset="utf-8")
        message.get_payload()[-1].add_related(
            banner_bytes(banner_key(event)), maintype="image", subtype="png",
            cid=f"<{BANNER_CID}>", disposition="inline",
        )
        context = ssl.create_default_context()
        options = {"host": config["host"], "port": config.get("port", 465),
                   "timeout": config.get("timeout_seconds", 15)}
        if config.get("security", "ssl") == "ssl":
            connection = smtplib.SMTP_SSL(**options, context=context)
        else:
            connection = smtplib.SMTP(**options)
        with connection as client:
            client.ehlo()
            if config.get("security", "ssl") == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if config.get("username"):
                client.login(config["username"], config["password"])
            refused = client.send_message(message, from_addr=config["from_address"], to_addrs=config["to"])
            if refused:
                raise NotificationError("email delivery failed for one or more recipients")

    def _sms(self, event: dict[str, Any]) -> None:
        config = self.config["sms"]
        if config.get("provider") == "http":
            self._http_sms(config, event)
        elif config.get("provider", "aliyun") == "huawei":
            self._huawei_sms(config, event)
        else:
            self._aliyun_sms(config, event)

    @staticmethod
    def _http_sms(config: dict[str, Any], event: dict[str, Any]) -> None:
        if event.get("phase") == "test":
            content = event["summary"]
        else:
            phase = "recovery" if event.get("phase") == "recovery" else "firing"
            template = config.get("templates", HTTP_SMS_TEMPLATES)[phase]
            content = PLACEHOLDER.sub(lambda match: _text(event.get(match.group(1))), template)
        if not isinstance(content, str) or not content.strip() or len(content) > config.get("max_content_length", 1000):
            raise NotificationError("sms content is empty or exceeds the configured character limit")
        payload = {**config.get("extra_body", {}), config.get("content_field", "content"): content}
        headers = {key: value for key, value in config.get("headers", {}).items()
                   if key.lower() != "content-type"}
        if config.get("encoding", "json") == "form":
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
            data = urlencode(payload).encode("utf-8")
        else:
            headers["Content-Type"] = "application/json; charset=utf-8"
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = Request(config["url"], data=data, headers=headers, method="POST")
        # Redirects must not forward configured authorization headers to a different endpoint.
        with build_opener(_NoRedirect()).open(request, timeout=config.get("timeout_seconds", 15)) as response:
            if not 200 <= response.status < 300:
                raise NotificationError("sms HTTP endpoint did not accept the message")
            expected = config.get("success_json", {"code": 0})
            if expected:
                result = json.loads(response.read(1024 * 1024).decode("utf-8"))
                if not isinstance(result, dict) or any(
                    key not in result or not _same_json(result[key], value) for key, value in expected.items()
                ):
                    raise NotificationError("sms HTTP endpoint returned an unsuccessful response")

    @staticmethod
    def _aliyun_sms(config: dict[str, Any], event: dict[str, Any]) -> None:
        from aliyunsdkcore.client import AcsClient
        from aliyunsdkcore.request import CommonRequest
        from .aliyun_compat import ensure_aliyun_tls_compatibility

        ensure_aliyun_tls_compatibility()
        client = AcsClient(config["access_key_id"], config["access_key_secret"],
                           config.get("region_id", "cn-hangzhou"),
                           auto_retry=False, timeout=config.get("timeout_seconds", 15))
        request = CommonRequest()
        request.set_accept_format("json")
        request.set_domain("dysmsapi.aliyuncs.com")
        request.set_method("POST")
        request.set_protocol_type("https")
        request.set_version("2017-05-25")
        request.set_action_name("SendSms")
        request.add_query_param("PhoneNumbers", ",".join(config["phone_numbers"]))
        request.add_query_param("SignName", config["sign_name"])
        request.add_query_param("TemplateCode", config["template_code"])
        request.add_query_param("TemplateParam", json.dumps(_render_params(config, event), ensure_ascii=False))
        result = json.loads(client.do_action_with_exception(request))
        if not isinstance(result, dict) or result.get("Code") != "OK":
            raise NotificationError("sms provider rejected the message; check signature, template, balance and rate limits")

    @staticmethod
    def _huawei_sms(config: dict[str, Any], event: dict[str, Any]) -> None:
        nonce = secrets.token_hex(16)
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(hashlib.sha256(
            (nonce + created + config["app_secret"]).encode("utf-8")
        ).digest()).decode("ascii")
        wsse = (f'UsernameToken Username="{config["app_key"]}", PasswordDigest="{digest}", '
                f'Nonce="{nonce}", Created="{created}"')
        payload = {"from": config["sender"], "to": ",".join(config["phone_numbers"]),
                   "templateId": config["template_id"],
                   "templateParas": json.dumps(_render_params(config, event), ensure_ascii=False)}
        if config.get("signature"):
            payload["signature"] = config["signature"]
        request = Request(config["api_endpoint"], data=urlencode(payload).encode("utf-8"),
                          headers={"Content-Type": "application/x-www-form-urlencoded",
                                   "Authorization": 'WSSE realm="SDP",profile="UsernameToken",type="Appkey"',
                                   "X-WSSE": wsse}, method="POST")
        with urlopen(request, timeout=config.get("timeout_seconds", 15)) as response:
            result = json.loads(response.read(1024 * 1024).decode("utf-8"))
        if not isinstance(result, dict) or result.get("code") != "000000":
            raise NotificationError("huawei sms rejected the message; check application, template, balance and rate limits")
        details = result.get("result")
        if not isinstance(details, list) or not details or any(
            not isinstance(item, dict) or item.get("status") != "000000" for item in details
        ):
            raise NotificationError("huawei sms delivery was not accepted for all recipients")
