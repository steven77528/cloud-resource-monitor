"""Small, credential-safe primitives for read-only external API collectors."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from ipaddress import ip_address
import json
import math
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ProviderError(ValueError):
    def __init__(self, reason, field=""):
        # Callers pass local reason codes and schema paths, never provider messages or URLs.
        self.reason = reason if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,64}", reason) else "collection_failed"
        self.field = field if isinstance(field, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\[\]]{0,95}", field) else ""
        super().__init__(self.reason + (f" field={self.field}" if self.field else ""))


def require_text(value, field):
    if not isinstance(value, str) or not value.strip() or value.startswith("CHANGE_ME"):
        raise ProviderError("missing_configuration", field)
    return value


def decimal_number(value, field):
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float, Decimal)):
        raise ProviderError("invalid_number", field)
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise ProviderError("invalid_number", field) from None
    if not result.is_finite():
        raise ProviderError("invalid_number", field)
    return result


def number(value, field):
    result = float(decimal_number(value, field))
    if not math.isfinite(result):
        raise ProviderError("invalid_number", field)
    return result


def timestamp(value, field, unit="seconds", *, default_timezone=None):
    try:
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, str) and not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            divisor = {"seconds": 1, "milliseconds": 1000}[unit]
            result = datetime.fromtimestamp(number(value, field) / divisor, timezone.utc)
        if result.tzinfo is None:
            if default_timezone is None:
                raise ValueError
            result = result.replace(tzinfo=default_timezone)
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        raise ProviderError("invalid_timestamp", field) from None


def read_secret_file(path, field):
    require_text(path, field)
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise ProviderError("credential_file_unavailable", field) from None
    return require_text(value, field)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def is_http_relay_endpoint(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or not parsed.hostname or "%" in parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port is None or not 1 <= parsed.port <= 65535
                or parsed.path != "/v1/monitor/collect" or "?" in url or "#" in url
                or any(char.isspace() or ord(char) < 32 or char == "\\" for char in url)):
            return False
        ip_address(parsed.hostname)
    except ValueError:
        return False
    return True


def request_json(method, url, *, headers=None, body=None, timeout=20, allow_http_relay=False):
    parsed = urlsplit(url)
    allowed_scheme = parsed.scheme == "https" or (allow_http_relay and is_http_relay_endpoint(url))
    if not allowed_scheme or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ProviderError("invalid_endpoint")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
        raise ProviderError("invalid_configuration", "timeout_seconds")
    try:
        request = Request(url, data=body, headers=headers or {}, method=method)
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ProviderError("response_too_large")
        result = json.loads(raw)
    except HTTPError:
        raise ProviderError("http_error") from None
    except (URLError, OSError, TimeoutError):
        raise ProviderError("transport_error") from None
    except (ValueError, UnicodeError):
        raise ProviderError("invalid_response") from None
    if not isinstance(result, dict):
        raise ProviderError("invalid_response")
    return result
