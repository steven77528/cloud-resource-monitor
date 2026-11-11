"""Standalone notification HTTP service; configuration is reloaded per request."""

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
from pathlib import Path
import re
import signal
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

import yaml

from .config import ROUTES, _expand
from .models import utcnow
from .notifiers import EVENT_FIELDS, NotificationError, Notifier, validate_notifications


CHANNELS = frozenset({"sms", "dingtalk", "email"})
CONFIG_ERROR = "notification configuration is unavailable or invalid"
LOG = logging.getLogger(__name__)
CONFIG_FIELDS = frozenset({
    "timeout_seconds", "webhook", "secret", "keyword", "host", "from_address", "to", "port", "security",
    "username", "provider", "phone_numbers", "access_key_id", "access_key_secret", "sign_name", "template_code",
    "api_endpoint", "app_key", "app_secret", "sender", "template_id", "template_params", "max_param_length",
    "encoding", "url", "headers", "content_field", "extra_body", "success_json", "templates",
    "templates.firing", "templates.recovery", "max_content_length", "banner_urls",
})


class GatewayConfigError(ValueError):
    def __init__(self, reason="config_invalid", field="config"):
        super().__init__(CONFIG_ERROR)
        self.reason = reason
        self.field = field


class RequestError(ValueError):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def _channel_config(config, channel, force_enabled=False):
    notifications = config["notifications"]
    section = notifications.get(channel)
    if not isinstance(section, dict):
        raise GatewayConfigError("channel_config_invalid", f"notifications.{channel}")
    if not isinstance(section.get("enabled", True), bool):
        raise GatewayConfigError("channel_enabled_invalid", f"notifications.{channel}.enabled")
    if not force_enabled and not section.get("enabled", True):
        return None
    selected = {**notifications, channel: {**section, "enabled": True}}
    try:
        validate_notifications(selected, [channel])
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        field = f"notifications.{channel}"
        # Retain only a known schema key, never the exception text or a configured value.
        match = re.match(rf"^notifications\.{channel}\.([a-z_]+(?:\.[a-z_]+)?)\s", str(exc))
        if match and match[1] in CONFIG_FIELDS:
            field += "." + match[1]
        raise GatewayConfigError("channel_config_invalid", field) from None
    return selected


def load_gateway_config(path, *, check_channels=False):
    """Load a complete snapshot. Parsing failures never include configuration values."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        config = _expand(yaml.safe_load(text))
        if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] != 1:
            raise GatewayConfigError("config_version_invalid", "version")
        api = config.get("api")
        if not isinstance(api, dict):
            raise GatewayConfigError("api_config_invalid", "api")
        token = api.get("token")
        if (not isinstance(token, str) or len(token.strip()) < 24
                or any(char in token for char in ("\r", "\n"))):
            raise GatewayConfigError("api_token_invalid", "api.token")
        host = api.get("host", "0.0.0.0")
        port = api.get("port", 8081)
        if not isinstance(host, str) or not host.strip():
            raise GatewayConfigError("api_host_invalid", "api.host")
        if type(port) is not int or not 1 <= port <= 65535:
            raise GatewayConfigError("api_port_invalid", "api.port")
        routes = config.get("routes", dict(ROUTES))
        if not isinstance(routes, dict) or set(routes) != set(ROUTES):
            raise GatewayConfigError("routes_invalid", "routes")
        if any(not isinstance(channel, str) or channel not in CHANNELS for channel in routes.values()):
            raise GatewayConfigError("routes_invalid", "routes")
        config["routes"] = routes
        if not isinstance(config.get("notifications"), dict):
            raise GatewayConfigError("channels_invalid", "notifications")
        if check_channels:
            configured = set(config["notifications"]) & CHANNELS
            for channel in sorted(configured | set(routes.values())):
                _channel_config(config, channel)
        return config
    except GatewayConfigError:
        raise
    except FileNotFoundError:
        raise GatewayConfigError("config_file_missing") from None
    except PermissionError:
        raise GatewayConfigError("config_file_permission_denied") from None
    except OSError:
        raise GatewayConfigError("config_file_unreadable") from None
    except UnicodeError:
        raise GatewayConfigError("config_encoding_invalid") from None
    except yaml.YAMLError:
        raise GatewayConfigError("config_yaml_invalid") from None
    except (ValueError, TypeError, KeyError, RecursionError):
        raise GatewayConfigError() from None


def _log_event_id(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        return str(uuid.UUID(value))
    return "-"


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _json_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite value")
    return result


def _invalid_constant(value):
    raise ValueError("non-finite value")


def _read_json(handler, maximum):
    if handler.headers.get_all("Transfer-Encoding"):
        raise RequestError(400, "transfer encoding is not supported")
    types = handler.headers.get_all("Content-Type", [])
    if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
        raise RequestError(415, "content type must be application/json")
    lengths = handler.headers.get_all("Content-Length", [])
    if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
        raise RequestError(400, "a single valid content length is required")
    length = int(lengths[0])
    if not 0 < length <= maximum:
        raise RequestError(413, "invalid payload size")
    try:
        body = handler.rfile.read(length)
        if len(body) != length:
            raise ValueError("incomplete payload")
        return json.loads(body.decode("utf-8"), object_pairs_hook=_json_pairs,
                          parse_float=_json_float, parse_constant=_invalid_constant)
    except (OSError, ValueError, TypeError, RecursionError):
        raise RequestError(400, "invalid JSON payload") from None


def _event_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {"event"}:
        raise RequestError(400, "provide an event object")
    event = payload["event"]
    if not isinstance(event, dict):
        raise RequestError(400, "provide an event object")
    if not isinstance(event.get("severity"), str) or event["severity"] not in ROUTES:
        raise RequestError(400, "event severity must be P0, P1 or P2")
    for name in EVENT_FIELDS:
        value = event.get(name)
        if value is not None and not isinstance(value, (str, bool, int, float)):
            raise RequestError(400, "event template fields must contain scalar values")
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise RequestError(400, "event contains invalid text") from None
    if "dimensions" in event and not isinstance(event["dimensions"], dict):
        raise RequestError(400, "event dimensions must be an object")
    return event


def _content_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {"content"}:
        raise RequestError(400, "provide only a content string")
    content = payload["content"]
    if not isinstance(content, str) or not content.strip() or len(content) > 1000:
        raise RequestError(400, "content must be non-empty and at most 1000 characters")
    try:
        content.encode("utf-8")
    except UnicodeError:
        raise RequestError(400, "content contains invalid text") from None
    return content


def _test_event(content, channel):
    severity = {"sms": "P0", "dingtalk": "P1", "email": "P2"}[channel]
    return {
        "id": str(uuid.uuid4()), "phase": "test", "severity": severity, "provider": "manual", "account": "manual",
        "resource_id": f"{channel}-test", "resource_name": f"{channel} test", "metric": "notification_test",
        "value": "", "unit": "", "summary": content, "observed_at": utcnow().isoformat(),
    }


def make_server(config_path, host=None, port=None):
    initial = load_gateway_config(config_path)
    config_path = Path(config_path)
    test_lock = threading.Lock()
    test_state = {channel: {"next_allowed": 0.0, "sending": False} for channel in CHANNELS}

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, fmt, *args):
            # Default access logs contain raw URLs, including possible query credentials.
            pass

        def respond(self, status, payload, headers=None, *, reason=None, field="-"):
            if self.command == "POST":
                result = payload.get("status")
                result = result if result in {"accepted", "skipped"} else "rejected"
                if reason is None:
                    reason = {200: "accepted", 400: "invalid_request", 401: "authentication_failed",
                              404: "not_found", 413: "invalid_payload_size", 415: "unsupported_content_type",
                              429: "rate_limited"}.get(status, "request_failed")
                if result == "skipped":
                    reason = "channel_disabled"
                level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
                LOG.log(level, "gateway_request request_id=%s endpoint=%s event_id=%s channel=%s "
                        "status=%s result=%s reason=%s field=%s",
                        self.log_request_id, self.log_endpoint, self.log_event_id, self.log_channel,
                        status, result, reason, field)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def do_GET(self):
            if urlsplit(self.path).path == "/healthz":
                self.respond(200, {"status": "ok"})
            else:
                self.respond(404, {"error": "not found"})

        def do_POST(self):
            self.log_request_id = uuid.uuid4().hex
            self.log_endpoint = "other"
            self.log_event_id = "-"
            self.log_channel = "-"
            try:
                path = urlsplit(self.path).path
            except ValueError:
                self.respond(400, {"error": "invalid request path"})
                return
            tests = {f"/notifications/{channel}/test": channel for channel in CHANNELS}
            if path == "/notify":
                self.log_endpoint = "notify"
            elif path in tests:
                self.log_endpoint = tests[path] + "_test"
                self.log_channel = tests[path]
            if path != "/notify" and path not in tests:
                self.respond(404, {"error": "not found"})
                return
            try:
                config = load_gateway_config(config_path)
                authorization = self.headers.get_all("Authorization", [])
                if (len(authorization) != 1 or not authorization[0].startswith("Bearer ")
                        or not hmac.compare_digest(config["api"]["token"].encode(), authorization[0][7:].encode())):
                    self.respond(401, {"error": "unauthorized"})
                    return
                if path == "/notify":
                    self.notify(config, _event_payload(_read_json(self, 64 * 1024)))
                else:
                    self.test(config, tests[path], _content_payload(_read_json(self, 16 * 1024)))
            except RequestError as exc:
                self.respond(exc.status, {"error": exc.message})
            except GatewayConfigError as exc:
                self.respond(503, {"error": CONFIG_ERROR}, reason=exc.reason, field=exc.field)
            except (ValueError, TypeError, KeyError):
                self.respond(503, {"error": CONFIG_ERROR}, reason="channel_config_invalid")
            except NotificationError:
                self.respond(502, {"error": "notification delivery failed"}, reason="delivery_failed")
            except Exception:
                self.respond(502, {"error": "notification delivery failed"}, reason="delivery_failed")

        def notify(self, config, event):
            channel = config["routes"][event["severity"]]
            self.log_event_id = _log_event_id(event.get("id"))
            self.log_channel = channel
            selected = _channel_config(config, channel)
            if selected is None:
                self.respond(200, {"status": "skipped", "reason": "channel_disabled", "channel": channel})
                return
            Notifier(selected).send(channel, event)
            self.respond(200, {"status": "accepted", "channel": channel})

        def test(self, config, channel, content):
            state = test_state[channel]
            retry_after = None
            # Reserve only this channel; unrelated notification requests remain independent.
            with test_lock:
                now = time.monotonic()
                if state["sending"] or now < state["next_allowed"]:
                    retry_after = max(1, math.ceil(state["next_allowed"] - now))
                else:
                    state["sending"] = True
                    state["next_allowed"] = now + 60
            if retry_after is not None:
                self.respond(429, {"error": "notification test rate limit exceeded", "retry_after": retry_after},
                             {"Retry-After": str(retry_after)})
                return
            try:
                selected = _channel_config(config, channel, force_enabled=True)
                notifier = Notifier(selected)
                if channel == "sms":
                    if (selected["sms"].get("provider") == "http"
                            and len(content) > selected["sms"].get("max_content_length", 1000)):
                        raise RequestError(400, "content exceeds the configured SMS content length limit")
                    event_id = notifier.send_sms_test(content)
                else:
                    event = _test_event(content, channel)
                    self.log_event_id = event["id"]
                    notifier.send(channel, event)
                    event_id = event["id"]
                self.log_event_id = _log_event_id(event_id)
                self.respond(200, {"status": "accepted", "channel": channel, "event_id": event_id})
            finally:
                with test_lock:
                    state["sending"] = False

    server = ThreadingHTTPServer((host or initial["api"].get("host", "0.0.0.0"),
                                  initial["api"].get("port", 8081) if port is None else port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Independent notification HTTP gateway")
    parser.add_argument("--config", default="config/notifications.yaml")
    parser.add_argument("command", choices=("serve", "check"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        if args.command == "check":
            load_gateway_config(args.config, check_channels=True)
            print(json.dumps({"valid": True}))
            return 0
        server = make_server(args.config)
        LOG.info("gateway_started port=%s", server.server_address[1])

        def stop(*_):
            raise KeyboardInterrupt

        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            server.serve_forever()
        finally:
            server.server_close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            LOG.info("gateway_stopped")
        return 0
    except KeyboardInterrupt:
        return 0
    except GatewayConfigError as exc:
        LOG.error("gateway_startup_failed reason=%s field=%s", exc.reason, exc.field)
        print(f"Notification gateway configuration failed: reason={exc.reason} field={exc.field}", file=sys.stderr)
        return 2
    except OSError:
        LOG.error("gateway_startup_failed reason=listen_failed field=api")
        print("Notification gateway could not listen; check the configured address and port", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
