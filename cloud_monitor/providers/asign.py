"""Read-only Asign OpenAPI and explicitly sourced local inputs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from requests_toolbelt.multipart.encoder import MultipartEncoder

from cloud_monitor.models import Metric
from cloud_monitor.providers.common import (
    ProviderError, decimal_number, number, read_secret_file, request_json,
    require_text, timestamp,
)
from cloud_monitor.providers.relay import trusted_relay_client


SUPPORTED_KINDS = frozenset({
    "company_verification", "company_verification_snapshot", "package_ledger",
})
MODES = {"company_verification": "openapi", "company_verification_snapshot": "openapi_snapshot",
         "package_ledger": "ledger"}
OPEN_ENDPOINTS = {"https://oapi.asign.cn", "https://prev.asign.cn"}
LEDGER_TIMEZONE = timezone(timedelta(hours=8))


def _alias(value, field):
    text = require_text(value, field)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", text):
        raise ProviderError("invalid_configuration", field)
    return text


def _header(value, field):
    text = require_text(value, field)
    if any(ord(char) < 33 or ord(char) > 126 for char in text):
        raise ProviderError("invalid_configuration", field)
    return text


def _positive(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderError("invalid_configuration", field)
    result = number(value, field)
    if result <= 0:
        raise ProviderError("invalid_configuration", field)
    return result


def _private_key(path):
    field = "account.private_key_file"
    content = read_secret_file(path, field)
    try:
        if content.startswith("-----BEGIN"):
            key = serialization.load_pem_private_key(content.encode("ascii"), password=None)
        else:
            raw = base64.b64decode("".join(content.split()), validate=True)
            key = serialization.load_der_private_key(raw, password=None)
    except (ValueError, TypeError, UnicodeError, UnsupportedAlgorithm):
        raise ProviderError("invalid_key", field) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ProviderError("invalid_key", field)
    return key


def validate_account(account):
    if not isinstance(account, dict):
        raise ProviderError("invalid_configuration", "account")
    _alias(account.get("id"), "account.id")
    relay = trusted_relay_client(account)
    mode = account.get("api_mode")
    if not isinstance(mode, str) or mode not in {"openapi", "openapi_snapshot", "ledger"}:
        raise ProviderError("invalid_configuration", "account.api_mode")
    timeout = _positive(account.get("timeout_seconds", 20), "account.timeout_seconds")
    if timeout > 120:
        raise ProviderError("invalid_configuration", "account.timeout_seconds")
    if mode == "ledger":
        require_text(account.get("ledger_file"), "account.ledger_file")
        return
    _header(account.get("app_id"), "account.app_id")
    if account.get("endpoint", "https://oapi.asign.cn") not in OPEN_ENDPOINTS:
        raise ProviderError("invalid_endpoint", "account.endpoint")
    if mode == "openapi":
        if relay is None:
            _private_key(account.get("private_key_file"))
    else:
        require_text(account.get("sdk_result_file"), "account.sdk_result_file")


def validate_options(kind, options):
    if kind not in SUPPORTED_KINDS or not isinstance(options, dict):
        raise ProviderError("invalid_configuration", "options")
    allowed = {
        "company_verification": {"resource_id", "selector"},
        "company_verification_snapshot": {"resource_id", "selector", "source_max_age_seconds"},
        "package_ledger": {"package_ids", "source_max_age_seconds"},
    }[kind]
    if set(options) - allowed:
        raise ProviderError("invalid_configuration", "options")
    if kind in {"company_verification", "company_verification_snapshot"}:
        _alias(options.get("resource_id"), "options.resource_id")
        selector = options.get("selector")
        if (not isinstance(selector, dict) or len(selector) != 1
                or not set(selector) <= {"account", "creditCode", "companyName"}):
            raise ProviderError("invalid_configuration", "options.selector")
        require_text(next(iter(selector.values())), "options.selector")
    if kind in {"company_verification_snapshot", "package_ledger"}:
        _positive(options.get("source_max_age_seconds", 172800), "options.source_max_age_seconds")
    if kind == "package_ledger":
        ids = options.get("package_ids")
        if not isinstance(ids, list) or not ids:
            raise ProviderError("invalid_configuration", "options.package_ids")
        for value in ids:
            _alias(value, "options.package_ids")
        if len(set(ids)) != len(ids):
            raise ProviderError("invalid_configuration", "options.package_ids")


def validate_job(account, kind, options):
    validate_options(kind, options)
    if account.get("api_mode") != MODES[kind]:
        raise ProviderError("api_mode_mismatch", "account.api_mode")


def _read_json(path, field):
    try:
        with Path(path).open("rb") as source:
            raw = source.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ProviderError("source_too_large", field)
        result = json.loads(raw)
    except (OSError, UnicodeError, ValueError):
        raise ProviderError("source_file_invalid", field) from None
    if not isinstance(result, dict) or type(result.get("version")) is not int or result["version"] != 1:
        raise ProviderError("invalid_response", field)
    return result


def _fresh_time(value, now, max_age, field, *, default_timezone=None):
    result = timestamp(value, field, default_timezone=default_timezone)
    age = (now - result).total_seconds()
    if age < -60:
        raise ProviderError("source_time_in_future", field)
    if age > max_age:
        raise ProviderError("source_data_stale", field)
    return result


def _rows(response):
    if not isinstance(response, dict) or type(response.get("code")) is not int:
        raise ProviderError("invalid_response", "response.code")
    if response["code"] != 100000:
        raise ProviderError("api_business_error")
    rows = response.get("data")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ProviderError("invalid_response", "response.data")
    return rows


class AsignCollector:
    SUPPORTED_KINDS = SUPPORTED_KINDS

    def __init__(self, account, *, transport=None):
        self.account = account
        self.account_id = account["id"]
        self._transport = transport or request_json

    def collect(self, kind: str, options: dict, now: datetime) -> list[Metric]:
        validate_job(self.account, kind, options)
        validate_account(self.account)
        now = timestamp(now, "now")
        return getattr(self, "_" + kind)(options, now)

    def _metric(self, resource, name, value, unit, now, dimensions):
        return Metric("asign", self.account_id, resource, resource, name,
                      number(value, "metric.value"), unit, now, dimensions)

    def _company_verification(self, options, now):
        relay = trusted_relay_client(self.account)
        if relay is not None:
            response = relay.request("POST", self.account.get("endpoint", "https://oapi.asign.cn")
                                     + "/user/getCompUser", body=options["selector"])
            return self._company_metric(_rows(response), options, now, "official_api")
        payload = json.dumps(options["selector"], sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False)
        # Matches the supplied official HttpUtils.doPOST, including its +60s timestamp.
        stamp = str(round(now.timestamp() * 1000) + 60000)
        app_id = self.account["app_id"]
        digest = hashlib.md5(payload.encode("utf-8"), usedforsecurity=False).hexdigest()
        signed = (payload + digest + app_id + stamp).encode("utf-8")
        signature = _private_key(self.account["private_key_file"]).sign(
            signed, padding.PKCS1v15(), hashes.SHA1(),
        )
        multipart = MultipartEncoder(fields={
            "appId": app_id, "bizData": payload, "timestamp": stamp,
            "Content-Type": "application/octet-stream", "type": "application/octet-stream",
        }, boundary="-----------------------------" + uuid4().hex)
        response = self._transport(
            "POST", self.account.get("endpoint", "https://oapi.asign.cn") + "/user/getCompUser",
            headers={"Content-Type": multipart.content_type, "Accept": "*/*",
                     "sign": base64.b64encode(signature).decode("ascii")},
            body=multipart.to_string(), timeout=self.account.get("timeout_seconds", 20),
        )
        return self._company_metric(_rows(response), options, now, "official_api")

    def _company_verification_snapshot(self, options, now):
        snapshot = _read_json(self.account["sdk_result_file"], "account.sdk_result_file")
        endpoint = self.account.get("endpoint", "https://oapi.asign.cn") + "/user/getCompUser"
        if snapshot.get("app_id") != self.account["app_id"] or snapshot.get("endpoint") != endpoint:
            raise ProviderError("source_identity_mismatch", "snapshot")
        observed = _fresh_time(snapshot.get("collected_at"), now,
                               options.get("source_max_age_seconds", 172800), "snapshot.collected_at")
        return self._company_metric(_rows(snapshot.get("response")), options, observed, "official_sdk_snapshot")

    def _company_metric(self, rows, options, observed, source):
        key, selected = next(iter(options["selector"].items()))
        matched = [row for row in rows if row.get(key) == selected]
        if not matched:
            raise ProviderError("record_missing", "response.data")
        if len(matched) != 1:
            raise ProviderError("ambiguous_record", "response.data")
        row = matched[0]
        if type(row.get("userType")) is not int or row["userType"] != 1:
            raise ProviderError("company_identity_mismatch", "response.data.userType")
        if type(row.get("status")) is not int or row["status"] not in {0, 1}:
            raise ProviderError("invalid_response", "response.data.status")
        return [self._metric(options["resource_id"], "company_verified", row["status"], "bool", observed,
                             {"source": source, "product": "company_verification",
                              "source_updated_at": observed.isoformat()})]

    def _package_ledger(self, options, now):
        ledger = _read_json(self.account["ledger_file"], "account.ledger_file")
        if ledger.get("account_id") != self.account_id:
            raise ProviderError("source_identity_mismatch", "ledger.account_id")
        updated = _fresh_time(ledger.get("updated_at"), now,
                              options.get("source_max_age_seconds", 172800), "ledger.updated_at",
                              default_timezone=LEDGER_TIMEZONE)
        rows = ledger.get("packages")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ProviderError("invalid_response", "ledger.packages")
        metrics = []
        for resource in options["package_ids"]:
            matched = [row for row in rows if row.get("id") == resource]
            if len(matched) != 1:
                raise ProviderError("record_missing" if not matched else "duplicate_record", "ledger.packages")
            row = matched[0]
            unit = _alias(row.get("unit"), "ledger.packages.unit")
            remaining = decimal_number(row.get("remaining"), "ledger.packages.remaining")
            if remaining < 0:
                raise ProviderError("invalid_response", "ledger.packages.remaining")
            dims = {"source": "ledger", "product": "commercial_package",
                    "source_updated_at": updated.isoformat(), "original_unit": unit,
                    "identity": {"source": "ledger", "product": "commercial_package", "original_unit": unit}}
            metrics.append(self._metric(resource, "package_remaining", remaining, unit, now, dims))
            metrics.append(self._metric(resource, "ledger_age_seconds", max(0, (now - updated).total_seconds()),
                                        "seconds", now, dims))
            if "total" in row:
                total = decimal_number(row["total"], "ledger.packages.total")
                if total < 0 or remaining > total:
                    raise ProviderError("invalid_response", "ledger.packages.total")
                if total > 0:
                    metrics.append(self._metric(resource, "package_remaining_percent", remaining / total * 100,
                                                "percent", now, dims))
            if "expires_at" in row:
                expires = timestamp(row["expires_at"], "ledger.packages.expires_at",
                                    default_timezone=LEDGER_TIMEZONE)
                metrics.append(self._metric(resource, "commercial_expiry_days", (expires - now).total_seconds() / 86400,
                                            "days", now, {**dims, "expires_at": expires.isoformat()}))
        return metrics
