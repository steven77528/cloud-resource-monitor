import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

from prometheus_client import CONTENT_TYPE_LATEST

from .models import utcnow
from .notification_client import NotificationRateLimited
from .notifiers import NotificationError
from .receipts import parse_receipts


def make_server(service, host=None, port=None):
    config = service.config.get("api", {})
    sms_lock = threading.Lock()
    sms_next_allowed = 0.0
    sms_sending = False

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, fmt, *args):
            pass

        def respond(self, code, data, headers=None):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def authorized(self, expected, allow_query=False):
            if not expected:
                return False
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if allow_query and not supplied:
                supplied = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
            return hmac.compare_digest(str(expected).encode(), supplied.encode())

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/metrics":
                metrics_config = config.get("metrics", {})
                if not metrics_config.get("enabled", True):
                    self.respond(404, {"error": "not found"})
                    return
                if "token" in metrics_config:
                    expected = metrics_config["token"]
                    authorization = self.headers.get_all("Authorization", [])
                    if (not isinstance(expected, str) or len(expected.strip()) < 24
                            or len(authorization) != 1 or not authorization[0].startswith("Bearer ")
                            or not hmac.compare_digest(expected.encode(), authorization[0][7:].encode())):
                        self.respond(401, {"error": "unauthorized"})
                        return
                from .prometheus import render_metrics

                try:
                    body = render_metrics(service)
                except Exception:
                    # A failed database read must fail the scrape, not return an empty healthy result.
                    self.respond(503, {"error": "metrics temporarily unavailable"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", CONTENT_TYPE_LATEST)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/healthz":
                alive = service.status()["scheduler_alive"]
                self.respond(200 if alive else 503, {"status": "ok" if alive else "unavailable"})
            elif path in ("/status", "/readyz"):
                if not self.authorized(config.get("admin_token")):
                    self.respond(401, {"error": "unauthorized"})
                    return
                state = service.status()
                ready = state["scheduler_alive"] and all(
                    j["status"] in ("ok", "no_data", "insufficient_data", "degraded") for j in state["jobs"])
                ready = ready and not state["outbox"].get("failed", 0)
                self.respond(200 if path == "/status" or ready else 503, state)
            else:
                self.respond(404, {"error": "not found"})

        def do_POST(self):
            if urlsplit(self.path).path == "/notifications/sms/test":
                self.sms_test()
                return
            parts = urlsplit(self.path).path.strip("/").split("/")
            receipts = config.get("receipts", {})
            if len(parts) != 4 or parts[:3] != ["webhooks", "aliyun", "sms"] or not receipts.get("enabled"):
                self.respond(404, {"error": "not found"})
                return
            account = parts[3]
            receipt_config = receipts.get("accounts", {}).get(account, {})
            if not self.authorized(receipt_config.get("token"), allow_query=True):
                self.respond(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    self.respond(413, {"error": "invalid payload size"})
                    return
                rows = parse_receipts(json.loads(self.rfile.read(length)), account, utcnow())
                service.store.add_receipts(rows)
            except (ValueError, TypeError, KeyError):
                self.respond(400, {"code": 400, "msg": "invalid delivery receipts"})
                return
            self.respond(200, {"code": 0, "msg": "success"})

        def sms_test(self):
            nonlocal sms_next_allowed, sms_sending
            expected = config.get("admin_token")
            if not isinstance(expected, str) or len(expected.strip()) < 24:
                self.respond(503, {"error": "configure an admin token of at least 24 characters"})
                return
            authorization = self.headers.get_all("Authorization", [])
            if (len(authorization) != 1 or not authorization[0].startswith("Bearer ")
                    or not hmac.compare_digest(expected.encode(), authorization[0][7:].encode())):
                self.respond(401, {"error": "unauthorized"})
                return
            if self.headers.get_all("Transfer-Encoding"):
                self.respond(400, {"error": "transfer encoding is not supported"})
                return
            content_types = self.headers.get_all("Content-Type", [])
            if (len(content_types) != 1
                    or content_types[0].split(";", 1)[0].strip().lower() != "application/json"):
                self.respond(415, {"error": "content type must be application/json"})
                return
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                self.respond(400, {"error": "a single valid content length is required"})
                return
            length = int(lengths[0])
            if not 0 < length <= 16 * 1024:
                self.respond(413, {"error": "invalid payload size"})
                return

            def unique_fields(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result

            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("incomplete payload")
                payload = json.loads(body.decode("utf-8"), object_pairs_hook=unique_fields)
                if not isinstance(payload, dict) or set(payload) != {"content"}:
                    raise ValueError("invalid fields")
                content = payload["content"]
                if not isinstance(content, str) or not content.strip() or len(content) > 1000:
                    raise ValueError("invalid content")
                content.encode("utf-8")
            except (OSError, ValueError, TypeError, RecursionError):
                self.respond(400, {"error": "provide only a non-empty content string of at most 1000 characters"})
                return

            # Reserve before delivery so concurrent requests and uncertain failures cannot double-send.
            with sms_lock:
                now = time.monotonic()
                if sms_sending or now < sms_next_allowed:
                    retry_after = str(max(1, math.ceil(sms_next_allowed - now)))
                    self.respond(429, {"error": "sms test rate limit exceeded"},
                                 {"Retry-After": retry_after})
                    return
                sms_next_allowed = now + 60
                sms_sending = True
            try:
                event_id = service.notifier.send_sms_test(content)
            except NotificationRateLimited as exc:
                self.respond(429, {"error": "sms test rate limit exceeded"},
                             {"Retry-After": str(exc.retry_after)})
            except ValueError:
                self.respond(400, {"error": "sms test configuration or content is invalid"})
            except NotificationError:
                self.respond(502, {"error": "sms test delivery failed; check configuration and connectivity"})
            except Exception:
                self.respond(502, {"error": "sms test delivery failed; check configuration and connectivity"})
            else:
                self.respond(200, {"status": "accepted", "channel": "sms", "event_id": event_id})
            finally:
                with sms_lock:
                    sms_sending = False

    server = ThreadingHTTPServer((host or config.get("host", "0.0.0.0"),
                                  config.get("port", 8080) if port is None else port), Handler)
    server.daemon_threads = True
    return server


def start_server(service):
    server = make_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="monitor-http")
    thread.start()
    return server
