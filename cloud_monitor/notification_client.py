"""Send monitoring events to the separately configured notification gateway."""

from __future__ import annotations

import json
import math
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .notifiers import NotificationDisabled, NotificationError


MAX_RESPONSE_BYTES = 1024 * 1024


class NotificationRateLimited(NotificationError):
    """The gateway rejected a request until its cooldown has elapsed."""

    def __init__(self, retry_after: int):
        super().__init__("notification gateway rate limit exceeded")
        self.retry_after = retry_after


def validate_gateway(config: dict[str, Any]) -> None:
    if not isinstance(config, dict):
        raise ValueError("notifications.gateway must be an object")
    url = config.get("url")
    valid_url = False
    if isinstance(url, str):
        try:
            parsed = urlsplit(url)
            valid_url = bool(
                parsed.scheme in {"http", "https"} and parsed.hostname
                and parsed.username is None and parsed.password is None
                and "?" not in url and "#" not in url
                and not any(char.isspace() or ord(char) < 32 for char in url)
                and (parsed.port is None or 1 <= parsed.port <= 65535)
            )
        except ValueError:
            pass
    if not valid_url:
        raise ValueError("notifications.gateway.url must be an HTTP(S) URL without user information, query or fragment")
    token = config.get("token")
    if (not isinstance(token, str) or len(token.strip()) < 24
            or "\r" in token or "\n" in token):
        raise ValueError("notifications.gateway.token must contain at least 24 characters without line breaks")
    timeout = config.get("timeout_seconds", 20)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 300):
        raise ValueError("notifications.gateway.timeout_seconds must be greater than 0 and at most 300")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _retry_after(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 86400:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 5:
        seconds = int(value)
        if 1 <= seconds <= 86400:
            return seconds
    return None


class NotificationClient:
    def __init__(self, config: dict[str, Any]):
        validate_gateway(config)
        self.config = dict(config)

    def send(self, channel: str, event: dict[str, Any]) -> None:
        # Keep the outbox transport signature; the gateway owns severity routing.
        result = self._post("/notify", {"event": event})
        if result.get("status") == "accepted":
            return
        if result.get("status") == "skipped" and result.get("reason") == "channel_disabled":
            raise NotificationDisabled("notification channel is disabled")
        raise NotificationError("notification gateway did not accept the event")

    def send_sms_test(self, content: str) -> str:
        result = self._post("/notifications/sms/test", {"content": content})
        event_id = result.get("event_id")
        if (result.get("status") != "accepted" or not isinstance(event_id, str)
                or not event_id.strip() or any(ord(char) < 32 or ord(char) == 127 for char in event_id)):
            raise NotificationError("notification gateway did not accept the SMS test")
        return event_id

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request = Request(
                self.config["url"].rstrip("/") + path,
                data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8",
                         "Authorization": "Bearer " + self.config["token"]},
                method="POST",
            )
            with build_opener(_NoRedirect()).open(
                request, timeout=self.config.get("timeout_seconds", 20)
            ) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if not 200 <= response.status < 300 or len(body) > MAX_RESPONSE_BYTES:
                    raise NotificationError("notification gateway returned an invalid response")
                result = json.loads(body.decode("utf-8"))
                if not isinstance(result, dict):
                    raise NotificationError("notification gateway returned an invalid response")
                return result
        except HTTPError as error:
            try:
                if error.code == 429:
                    retry_after = _retry_after(error.headers.get("Retry-After"))
                    if retry_after is None:
                        try:
                            body = error.read(MAX_RESPONSE_BYTES + 1)
                            result = json.loads(body.decode("utf-8")) if len(body) <= MAX_RESPONSE_BYTES else {}
                            if isinstance(result, dict):
                                retry_after = _retry_after(result.get("retry_after"))
                        except Exception:
                            pass
                    raise NotificationRateLimited(retry_after or 60) from None
                raise NotificationError("notification gateway rejected the request") from None
            finally:
                error.close()
        except NotificationError:
            raise
        except Exception:
            # Neither gateway bodies nor network exceptions may expose credentials.
            raise NotificationError("notification gateway request failed; check configuration and connectivity") from None
