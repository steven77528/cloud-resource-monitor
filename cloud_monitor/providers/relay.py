"""Run local monitoring adapters through an authenticated, credential-free API relay."""

from copy import deepcopy
from functools import partial
from importlib import import_module
import json
import math
import re
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from ..models import Metric
from .common import ProviderError, is_http_relay_endpoint, read_secret_file, request_json, require_text, timestamp


METRICS = {
    ("qiniu", "balance"): {"qiniu_cash_balance", "qiniu_present_balance", "qiniu_credit_line",
                           "qiniu_unpaid_money", "qiniu_available_balance"},
    ("qiniu", "billing_snapshot"): {"qiniu_estimated_cost", "qiniu_budget_used_percent"},
    ("qiniu", "resource_packages_month"): {"qiniu_package_month_total", "qiniu_package_month_used",
        "qiniu_package_month_remaining", "qiniu_package_remaining_percent"},
    ("qiniu", "resource_packages"): {"qiniu_package_expiry_days", "qiniu_package_unavailable",
                                    "qiniu_package_data_age_seconds"},
    ("qiniu", "kodo_usage"): {"qiniu_storage_bytes", "qiniu_object_count"},
    ("qiniu", "cdn_usage"): {"qiniu_cdn_flow_bytes", "qiniu_cdn_bandwidth_bps"},
    ("qiniu", "cdn_status"): {"qiniu_cdn_requests", "qiniu_cdn_5xx_requests", "qiniu_cdn_5xx_percent"},
    ("huifu", "balance"): {"huifu_available_balance", "huifu_balance", "huifu_frozen_balance",
                           "huifu_account_normal"},
    ("huifu", "settlements"): {"huifu_settlement_failed", "huifu_settlement_returned",
                               "huifu_settlement_overdue", "huifu_tracked_settlements"},
    ("huifu", "local_certificate"): {"huifu_certificate_expiry_days"},
    ("asign", "company_verification"): {"company_verified"},
    ("asign", "company_verification_snapshot"): {"company_verified"},
    ("asign", "package_ledger"): {"package_remaining", "ledger_age_seconds", "package_remaining_percent",
                                  "commercial_expiry_days"},
    ("wechat", "api_quota"): {"api_quota_daily_limit", "api_quota_used", "api_quota_remaining",
                              "api_quota_remaining_percent"},
    ("wechat", "charge_packages"): {"subscription_expiry_days", "subscription_active", "package_total",
        "package_used", "package_remaining", "package_status", "package_expiry_days", "package_remaining_percent",
        "charge_active_package_count", "charge_active_total", "charge_active_used", "charge_active_remaining",
        "charge_active_remaining_percent"},
    ("wechat", "annual_review"): {"qualification_verified", "annual_review_required", "annual_review_expiry_days"},
    ("wechat", "js_errors"): {"js_error_occurrences", "js_error_affected_users", "js_error_group_count",
                              "js_error_total_occurrences"},
    ("wechat", "performance"): {"performance_cost_milliseconds"},
}
DIMENSIONS = {
    "qiniu": {"source", "currency", "source_field", "source_unit", "raw_value", "identity", "billing_period",
        "snapshot_cutoff", "source_time", "item_name", "zone_name", "available_time", "respack_unit",
        "order_hash", "po_id", "expires_at", "status", "storage_class", "region", "window_seconds"},
    "huifu": {"source", "product", "scope", "currency", "identity", "source_amount_unit", "raw_value", "expires_at"},
    "asign": {"source", "product", "expires_at", "source_updated_at", "original_unit",
              "identity"},
    "wechat": {"appid", "source", "identity", "cgi_path", "data_date", "spu_id", "product_type", "starts_at",
        "expires_at", "package_id", "status", "raw_total", "raw_used", "raw_remaining", "quota_unit",
        "review_begins_at", "window_end", "collected_at", "app_version", "error_type", "error_message_hash",
        "error_stack_hash", "device", "networktype", "scene", "is_download_code", "cost_time_type"},
}
IDENTITY_KEYS = {
    "qiniu": {"currency", "item_name", "zone_name", "available_time", "respack_unit"},
    "huifu": {"acct_type", "currency"},
    "asign": {"source", "product", "original_unit"},
    "wechat": {"appid", "unit", "spu_id", "quota_unit", "app_version", "error_type", "cost_time_type", "device",
               "networktype", "scene", "is_download_code"},
}
LOCAL_KINDS = {"package_ledger", "company_verification_snapshot", "local_certificate"}
ERROR_REASONS = {"signature_invalid", "permission_denied", "upstream_error", "no_data",
                 "source_data_stale", "rate_limited"}
METRIC_FIELDS = {"provider", "account", "resource_id", "resource_name", "metric", "value", "unit",
                 "observed_at", "dimensions"}
ACCOUNT_FIELDS = {"id", "provider", "enabled", "name", "transport", "relay"}
PUBLIC_FIELDS = {
    "qiniu": {"request_timeout_seconds"},
    "huifu": {"huifu_id", "credential_mode"},
    "asign": {"api_mode", "app_id", "endpoint", "timeout_seconds"},
    "wechat": {"appid", "token_type", "third_party_authorized", "permission_sets", "request_timeout_seconds"},
}
DIRECT_COLLECTORS = {"qiniu": "QiniuCollector", "huifu": "HuifuCollector",
                     "asign": "AsignCollector", "wechat": "WechatCollector"}
OFFICIAL_HOSTS = {
    "qiniu": {"api.qiniu.com", "api.qiniuapi.com", "fusion.qiniuapi.com"},
    "huifu": {"api.huifu.com"},
    "asign": {"oapi.asign.cn", "prev.asign.cn"},
    "wechat": {"api.weixin.qq.com"},
}
FORBIDDEN_QUERY = {"access_token", "authorizer_access_token", "token", "secret", "appsecret",
                   "access_key_id", "access_key_secret", "authorization", "sign", "signature"}


def _alias(value, field):
    require_text(value, field)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ProviderError("invalid_configuration", field)
    return value


def _positive(value, field, maximum):
    if (type(value) not in {int, float} or not math.isfinite(value) or not 0 < value <= maximum):
        raise ProviderError("invalid_configuration", field)
    return value


def validate_account(account):
    if not isinstance(account, dict):
        raise ProviderError("relay_account_fields_forbidden", "account")
    provider = account.get("provider")
    if not isinstance(provider, str) or provider not in PUBLIC_FIELDS:
        raise ProviderError("relay_required", "account.provider")
    if set(account) - (ACCOUNT_FIELDS | PUBLIC_FIELDS[provider]):
        raise ProviderError("relay_account_fields_forbidden", "account")
    _alias(account.get("id"), "account.id")
    if account.get("provider") not in DIMENSIONS or account.get("transport", "relay") != "relay":
        raise ProviderError("relay_required", "account.transport")
    relay = account.get("relay")
    if not isinstance(relay, dict) or set(relay) - {"url", "token_file", "timeout_seconds"}:
        raise ProviderError("relay_not_configured", "account.relay")
    url = relay.get("url")
    valid = False
    if isinstance(url, str):
        try:
            parsed = urlsplit(url)
            valid = ((parsed.scheme == "https" or is_http_relay_endpoint(url)) and bool(parsed.hostname)
                     and parsed.username is None and parsed.password is None
                     and not any(char.isspace() or ord(char) < 32 or char == "\\" for char in url)
                     and "?" not in url and "#" not in url
                     and (parsed.port is None or 1 <= parsed.port <= 65535))
        except ValueError:
            pass
    if not valid:
        raise ProviderError("invalid_endpoint", "account.relay.url")
    require_text(relay.get("token_file"), "account.relay.token_file")
    _positive(relay.get("timeout_seconds", 20), "account.relay.timeout_seconds", 120)


def validate_job(account, kind, options):
    if not isinstance(kind, str) or (account["provider"], kind) not in METRICS:
        raise ProviderError("unsupported_kind", "kind")
    if not isinstance(options, dict) or set(options) - {"operation", "source_max_age_seconds", "collector"}:
        raise ProviderError("invalid_configuration", "options")
    if kind not in LOCAL_KINDS or "operation" in options:
        _alias(options.get("operation"), "options.operation")
    _positive(options.get("source_max_age_seconds", 172800), "options.source_max_age_seconds", 31536000)
    collector_options = options.get("collector", {})
    if not isinstance(collector_options, dict):
        raise ProviderError("invalid_configuration", "options.collector")
    client = RelayCollector(account)
    backend_account = {**deepcopy(account), "_relay_client": client}
    module = import_module("." + account["provider"], __package__)
    module.validate_account(backend_account)
    if hasattr(module, "validate_job"):
        module.validate_job(backend_account, kind, collector_options)
    else:
        module.validate_options(kind, collector_options)


def trusted_relay_client(account):
    client = account.get("_relay_client")
    if client is not None and not isinstance(client, RelayCollector):
        raise ProviderError("invalid_configuration", "account")
    return client


def _safe_text(value, limit=256):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ProviderError("relay_invalid_response")
    return value


def _scalar(value):
    if isinstance(value, str):
        return _safe_text(value)
    if type(value) not in {int, float}:
        raise ProviderError("relay_invalid_response")
    try:
        if not math.isfinite(value):
            raise ValueError
    except (ValueError, OverflowError):
        raise ProviderError("relay_invalid_response") from None
    return value


def _dimensions(value, provider):
    if not isinstance(value, dict) or set(value) - DIMENSIONS[provider]:
        raise ProviderError("relay_invalid_response")
    result = {}
    for key, item in value.items():
        if key == "identity":
            if not isinstance(item, dict) or not item or set(item) - IDENTITY_KEYS[provider]:
                raise ProviderError("relay_invalid_response")
            result[key] = {name: _scalar(data) for name, data in item.items()}
        else:
            result[key] = _scalar(item)
    return result


def _source(provider, kind, metric):
    if provider == "wechat":
        return "wechat_official_api"
    if kind == "company_verification_snapshot":
        return "official_sdk_snapshot"
    if kind == "package_ledger":
        return "ledger"
    if kind == "local_certificate":
        return "local_certificate"
    if metric == "huifu_tracked_settlements":
        return "order_manifest"
    return "official_api"


def _contains_token(value, token):
    if isinstance(value, str):
        return token in value
    if isinstance(value, dict):
        return any(_contains_token(item, token) for item in value.values())
    if isinstance(value, list):
        return any(_contains_token(item, token) for item in value)
    return False


class RelayCollector:
    def __init__(self, account, *, transport=None):
        validate_account(account)
        self.account = deepcopy(account)
        self._transport = transport or partial(request_json, allow_http_relay=True)
        self.query_performed = False
        self._kind = None
        self._operation = None
        self._backend = None
        self._tokens = set()

    def collect_job(self, job, now):
        options = dict(job.get("options", {}))
        validate_job(self.account, job["kind"], options)
        options["source_max_age_seconds"] = min(options.get("source_max_age_seconds", 172800),
            job.get("max_age_seconds", max(900, job["interval_seconds"] * 2)))
        return self.collect(job["kind"], options, now)

    def collect(self, kind, options, now):
        self.query_performed = False
        validate_job(self.account, kind, options)
        now = timestamp(now, "now")
        self._kind, self._operation = kind, options.get("operation")
        self._tokens = set()
        try:
            if self._backend is None:
                module = import_module("." + self.account["provider"], __package__)
                backend_account = {**deepcopy(self.account), "_relay_client": self}
                self._backend = getattr(module, DIRECT_COLLECTORS[self.account["provider"]])(backend_account)
            metrics = self._backend.collect(kind, deepcopy(options.get("collector", {})), now)
            return self._validate_metrics(metrics, kind, options, now)
        finally:
            self._kind, self._operation = None, None

    def request(self, method, url, body=None):
        if self._kind is None or self._kind in LOCAL_KINDS:
            raise ProviderError("invalid_configuration", "relay.request")
        try:
            parsed = urlsplit(url)
            if (method not in {"GET", "POST"} or parsed.scheme != "https"
                    or parsed.hostname not in OFFICIAL_HOSTS[self.account["provider"]]
                    or parsed.port not in {None, 443} or parsed.username is not None or parsed.password is not None
                    or parsed.fragment or any(c.isspace() or ord(c) < 32 or c == "\\" for c in url)
                    or any(key.lower() in FORBIDDEN_QUERY for key, _ in parse_qsl(parsed.query))
                    or (body is not None and not isinstance(body, dict))):
                raise ValueError
        except (ValueError, TypeError):
            raise ProviderError("invalid_configuration", "relay.request") from None
        relay = self.account["relay"]
        token = read_secret_file(relay["token_file"], "account.relay.token_file")
        if not 24 <= len(token) <= 4096 or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ProviderError("invalid_configuration", "account.relay.token_file")
        self._tokens.add(token)
        identity = {"version": 2, "request_id": uuid4().hex, "provider": self.account["provider"],
                    "account": self.account["id"], "operation": self._operation, "kind": self._kind}
        payload = {**identity, "request": {"method": method, "url": url, "body": body}}
        try:
            response = self._transport("POST", relay["url"],
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                         "Accept": "application/json"},
                body=json.dumps(payload, allow_nan=False).encode("utf-8"),
                timeout=relay.get("timeout_seconds", 20))
        except Exception:
            # Even custom transports must not leak URLs, authorization headers or remote bodies.
            raise ProviderError("relay_request_failed") from None
        try:
            raw = self._parse(response, identity, token)
            self.query_performed = True
            return raw
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("relay_invalid_response") from None

    def _parse(self, response, payload, token):
        if (not isinstance(response, dict) or type(response.get("version")) is not int
                or any(response.get(key) != value for key, value in payload.items())):
            raise ProviderError("relay_identity_mismatch")
        if response.get("status") == "error":
            if set(response) != set(payload) | {"status", "reason"}:
                raise ProviderError("relay_invalid_response")
            reason = response["reason"]
            if not isinstance(reason, str) or reason not in ERROR_REASONS:
                raise ProviderError("relay_upstream_error")
            raise ProviderError(reason)
        if (response.get("status") != "ok" or set(response) != set(payload) | {
                "status", "signature_verified", "response"}):
            raise ProviderError("relay_invalid_response")
        verified = response["signature_verified"]
        provider, kind = payload["provider"], payload["kind"]
        if provider == "huifu" and kind in {"balance", "settlements"}:
            if verified is False:
                raise ProviderError("signature_invalid")
            if verified is not True:
                raise ProviderError("relay_invalid_response")
        elif verified is not None:
            raise ProviderError("relay_invalid_response")
        raw = response["response"]
        if not isinstance(raw, dict) or _contains_token(raw, token):
            raise ProviderError("relay_invalid_response")
        return raw

    def _validate_metrics(self, collected, kind, options, now):
        if not isinstance(collected, list) or not all(isinstance(metric, Metric) for metric in collected):
            raise ProviderError("invalid_response")
        rows = [metric.to_dict() for metric in collected]
        provider = self.account["provider"]
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ProviderError("relay_invalid_response")
        if not rows:
            raise ProviderError("no_data")
        metrics, seen = [], set()
        # Validate every row before exposing any metrics to the alert engine.
        for row in rows:
            if not isinstance(row, dict) or set(row) != METRIC_FIELDS:
                raise ProviderError("relay_invalid_response")
            if row["provider"] != provider or row["account"] != self.account["id"]:
                raise ProviderError("relay_identity_mismatch")
            name = _safe_text(row["metric"])
            if name not in METRICS[(provider, kind)]:
                raise ProviderError("relay_invalid_response")
            value = row["value"]
            if type(value) not in {int, float}:
                raise ProviderError("relay_invalid_response")
            _scalar(value)
            observed = timestamp(row["observed_at"], "metric.observed_at")
            age = (now - observed).total_seconds()
            if age < -60:
                raise ProviderError("source_time_in_future")
            if age > options.get("source_max_age_seconds", 172800):
                raise ProviderError("source_data_stale")
            dims = _dimensions(row["dimensions"], provider)
            if dims.get("source") != _source(provider, kind, name):
                raise ProviderError("relay_invalid_response")
            metric = Metric(provider, self.account["id"], _safe_text(row["resource_id"]),
                            _safe_text(row["resource_name"]), name, float(value), _safe_text(row["unit"], 64),
                            observed, dims)
            if any(_contains_token(metric.to_dict(), token) for token in self._tokens):
                raise ProviderError("relay_invalid_response")
            key = metric.key()
            if key in seen:
                raise ProviderError("relay_duplicate_metric")
            seen.add(key)
            metrics.append(metric)
        if provider == "huifu" and kind == "settlements" and not self.query_performed:
            if len(metrics) != 1 or metrics[0].metric != "huifu_tracked_settlements" or metrics[0].value != 0:
                raise ProviderError("relay_invalid_response")
        return metrics
