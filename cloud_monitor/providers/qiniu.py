"""Read-only Qiniu finance, Kodo and CDN collectors using official SDK signing."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode

from cloud_monitor.models import Metric
from cloud_monitor.providers.common import (
    ProviderError, decimal_number, read_secret_file, request_json, require_text,
)
from cloud_monitor.providers.relay import trusted_relay_client

SUPPORTED_KINDS = frozenset({
    "balance", "billing_snapshot", "resource_packages_month", "resource_packages",
    "kodo_usage", "cdn_usage", "cdn_status",
})
CHINA = timezone(timedelta(hours=8))
UTC = timezone.utc
FINANCE = "https://api.qiniu.com/billing-api/"
FUSION = "https://fusion.qiniuapi.com"
MONEY_SCALE = Decimal(100_000_000)


def _mapping(value, field):
    if not isinstance(value, dict):
        raise ProviderError("response_schema_invalid", field)
    return value


def _integer(value, field, minimum=0):
    result = decimal_number(value, field)
    if result != result.to_integral_value() or result < minimum:
        raise ProviderError("numeric_value_invalid", field)
    return int(result)


def _nonnegative(value, field):
    result = decimal_number(value, field)
    if result < 0:
        raise ProviderError("numeric_value_invalid", field)
    return result


def _time(value, field, *, pattern=None):
    try:
        if pattern:
            result = datetime.strptime(require_text(value, field), pattern)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            result = datetime.fromtimestamp(value, UTC)
        else:
            result = datetime.fromisoformat(require_text(value, field).replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=CHINA)
        return result.astimezone(UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        raise ProviderError("timestamp_invalid", field) from None


def _texts(value, field, limit=100):
    if not isinstance(value, list) or not 1 <= len(value) <= limit:
        raise ProviderError("configuration_invalid", field)
    result = [require_text(item, field) for item in value]
    if len(result) != len(set(result)):
        raise ProviderError("configuration_invalid", field)
    return result


def validate_account(account):
    require_text(account.get("id"), "account.id")
    if trusted_relay_client(account) is None:
        require_text(account.get("access_key_id"), "account.access_key_id")
        if account.get("access_key_secret_file"):
            require_text(account["access_key_secret_file"], "account.access_key_secret_file")
        else:
            require_text(account.get("access_key_secret"), "account.access_key_secret")
    timeout = account.get("request_timeout_seconds", 20)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
        raise ProviderError("configuration_invalid", "account.request_timeout_seconds")


def validate_options(kind, options):
    if kind not in SUPPORTED_KINDS or not isinstance(options, dict):
        raise ProviderError("configuration_invalid", "job.kind")
    allowed = {
        "balance": {"available_balance_field", "available_balance_confirmed"},
        "billing_snapshot": {"monthly_budget", "budget_currency"},
        "resource_packages_month": {"page_size", "max_pages"},
        "resource_packages": {"page_size", "max_pages", "packages", "max_source_age_seconds"},
        "kodo_usage": {"buckets", "max_source_age_seconds"},
        "cdn_usage": {"domains", "max_source_age_seconds"},
        "cdn_status": {"domains", "max_source_age_seconds", "minimum_requests"},
    }[kind]
    if set(options) - allowed:
        raise ProviderError("configuration_invalid", "job.options")
    for key, default, maximum in (("page_size", 200, 200), ("max_pages", 1000, 10000),
                                  ("max_source_age_seconds", 900, 31_536_000)):
        if key in options:
            value = options.get(key, default)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ProviderError("configuration_invalid", "options." + key)
    if kind == "balance":
        field = options.get("available_balance_field")
        confirmed = options.get("available_balance_confirmed", False)
        if type(confirmed) is not bool:
            raise ProviderError("configuration_invalid", "options.available_balance_confirmed")
        if field is not None and field not in {"balance", "available_balance"}:
            raise ProviderError("configuration_invalid", "options.available_balance_field")
        if (field is not None) != confirmed:
            raise ProviderError("field_confirmation_required", "options.available_balance_field")
    if kind == "billing_snapshot" and "monthly_budget" in options:
        if decimal_number(options["monthly_budget"], "options.monthly_budget") <= 0:
            raise ProviderError("configuration_invalid", "options.monthly_budget")
        require_text(options.get("budget_currency"), "options.budget_currency")
    if kind == "resource_packages":
        packages = options.get("packages")
        if not isinstance(packages, list) or not packages:
            raise ProviderError("configuration_invalid", "options.packages")
        identities = set()
        for package in packages:
            package = _mapping(package, "options.packages")
            order = require_text(package.get("order_hash"), "options.packages.order_hash")
            po_id = _integer(package.get("po_id"), "options.packages.po_id", 1)
            if (order, po_id) in identities:
                raise ProviderError("configuration_invalid", "options.packages")
            identities.add((order, po_id))
    if kind == "kodo_usage":
        _texts(options.get("buckets"), "options.buckets")
    if kind in {"cdn_usage", "cdn_status"}:
        for domain in _texts(options.get("domains"), "options.domains", 50):
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", domain):
                raise ProviderError("configuration_invalid", "options.domains")
    if kind == "cdn_status":
        _integer(options.get("minimum_requests", 100), "options.minimum_requests", 1)


class QiniuCollector:
    SUPPORTED_KINDS = SUPPORTED_KINDS

    def __init__(self, account, transport=None):
        self.account = account
        self.account_id = require_text(account.get("id"), "account.id")
        self.transport = transport or request_json

    def collect(self, kind, options, now):
        validate_account(self.account)
        validate_options(kind, options)
        if now.tzinfo is None:
            raise ProviderError("timestamp_invalid", "now")
        return getattr(self, "_" + kind)(options, now.astimezone(UTC))

    def _request(self, method, url, now, payload=None, qbox=False):
        relay = trusted_relay_client(self.account)
        response = (relay.request(method, url, body=payload) if relay is not None
                    else self._signed_request(method, url, now, payload, qbox))
        response = _mapping(response, "response")
        if url.startswith(FINANCE) or qbox:
            expected = 200 if qbox else 0
            if type(response.get("code")) is not int or response["code"] != expected:
                raise ProviderError("provider_query_failed", "response.code")
        elif "error" in response:
            raise ProviderError("provider_query_failed", "response.error")
        return response

    def _signed_request(self, method, url, now, payload, qbox):
        from qiniu import Auth
        from qiniu.auth import QiniuMacAuth

        secret = self.account.get("access_key_secret")
        if self.account.get("access_key_secret_file"):
            secret = read_secret_file(self.account["access_key_secret_file"], "account.access_key_secret_file")
        key = self.account["access_key_id"]
        content_type = "application/json" if payload is not None else "application/x-www-form-urlencoded"
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
        headers = {"Content-Type": content_type}
        if qbox:
            headers["Authorization"] = "QBox " + Auth(key, secret).token_of_request(
                url, body.decode() if body else None, content_type,
            )
        else:
            auth = QiniuMacAuth(key, secret)
            headers["X-Qiniu-Date"] = now.strftime("%Y%m%dT%H%M%SZ")
            headers["Authorization"] = "Qiniu " + auth.token_of_request(
                method, None, url, auth.qiniu_headers(headers), content_type, body,
            )
        return self.transport(method, url, headers=headers, body=body,
                              timeout=self.account.get("request_timeout_seconds", 20))

    def _metric(self, resource, name, value, unit, now, dimensions=None, resource_name=None):
        return Metric("qiniu", self.account_id, resource, resource_name or resource,
                      name, float(value), unit, now, {"source": "official_api", **(dimensions or {})})

    def _balance(self, options, now):
        data = _mapping(self._request("GET", FINANCE + "v1/account/balance-overview", now).get("data"),
                        "data")
        currency = require_text(data.get("currency"), "data.currency")
        account_id = require_text(data.get("account_id"), "data.account_id")
        fields = {"cash_balance": "qiniu_cash_balance", "present_balance": "qiniu_present_balance",
                  "credit_line": "qiniu_credit_line", "unpaid_money": "qiniu_unpaid_money"}
        if options.get("available_balance_confirmed"):
            fields[options["available_balance_field"]] = "qiniu_available_balance"
        result = []
        for field, metric in fields.items():
            raw = decimal_number(data.get(field), "data." + field)
            if raw != raw.to_integral_value():
                raise ProviderError("numeric_value_invalid", "data." + field)
            dimensions = {"currency": currency, "source_field": field, "source_unit": "1e-8 " + currency,
                          "raw_value": str(raw), "identity": {"currency": currency}}
            result.append(self._metric(account_id, metric, raw / MONEY_SCALE, currency, now, dimensions))
        return result

    def _billing_snapshot(self, options, now):
        local = now.astimezone(CHINA)
        cutoff = local.replace(hour=0, minute=0, second=0, microsecond=0)
        if local.hour < 8:
            if local.day == 1:
                raise ProviderError("source_not_ready", "snapshot.date")
            cutoff -= timedelta(days=1)
        billing_month = (cutoff - timedelta(days=1)).strftime("%Y-%m")
        url = FINANCE + "v2/bill/snapshot?" + urlencode({"date": cutoff.strftime("%Y-%m-%dT00:00:00")})
        data = _mapping(self._request("GET", url, now).get("data"), "data")
        currency = require_text(data.get("currency"), "data.currency")
        raw = decimal_number(data.get("total_money"), "data.total_money")
        if raw != raw.to_integral_value():
            raise ProviderError("numeric_value_invalid", "data.total_money")
        amount = raw / MONEY_SCALE
        dimensions = {"currency": currency, "billing_period": billing_month,
                      "snapshot_cutoff": cutoff.isoformat(), "source_time": cutoff.isoformat(),
                      "source_unit": "1e-8 " + currency, "raw_value": str(raw),
                      "identity": {"currency": currency}}
        result = [self._metric(self.account_id, "qiniu_estimated_cost", amount, currency, now, dimensions)]
        if "monthly_budget" in options:
            if currency != options["budget_currency"]:
                raise ProviderError("currency_mismatch", "data.currency")
            if billing_month == local.strftime("%Y-%m"):
                budget = decimal_number(options["monthly_budget"], "options.monthly_budget")
                result.append(self._metric(self.account_id, "qiniu_budget_used_percent", amount / budget * 100,
                                           "percent", now, dimensions))
        return result

    def _pages(self, endpoint, options, now):
        size = options.get("page_size", 200)
        seen = set()
        result = []
        for page in range(1, options.get("max_pages", 1000) + 1):
            url = FINANCE + endpoint + "?" + urlencode({"page": page, "page_size": size})
            rows = self._request("GET", url, now).get("data")
            if not isinstance(rows, list) or len(rows) > size or any(not isinstance(row, dict) for row in rows):
                raise ProviderError("response_schema_invalid", "data")
            if not rows:
                return result
            digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).digest()
            if digest in seen:
                raise ProviderError("pagination_repeated", "data")
            seen.add(digest)
            result.extend(rows)
            if len(rows) < size:
                return result
        raise ProviderError("pagination_incomplete", "data")

    def _resource_packages_month(self, options, now):
        result, seen = [], set()
        rows = self._pages("v1/respack/month-overview", options, now)
        for row in rows:
            identity = {field: require_text(row.get(field), "data." + field)
                        for field in ("item_name", "zone_name", "available_time", "respack_unit")}
            encoded = json.dumps(identity, sort_keys=True)
            if encoded in seen:
                raise ProviderError("duplicate_resource", "data")
            seen.add(encoded)
            resource = "month-package:" + hashlib.sha256(encoded.encode()).hexdigest()[:24]
            resource_name = " / ".join(identity.values())
            values = {field: _nonnegative(row.get(field), "data." + field)
                      for field in ("total_surplus", "month_used", "month_remain")}
            if values["month_remain"] > values["total_surplus"]:
                raise ProviderError("numeric_value_invalid", "data.month_remain")
            dimensions = {**identity, "identity": identity, "source_unit": identity["respack_unit"],
                          "billing_period": now.astimezone(CHINA).strftime("%Y-%m")}
            for field, metric in (("total_surplus", "qiniu_package_month_total"),
                                  ("month_used", "qiniu_package_month_used"),
                                  ("month_remain", "qiniu_package_month_remaining")):
                result.append(self._metric(resource, metric, values[field], identity["respack_unit"], now,
                                           {**dimensions, "raw_value": str(values[field])}, resource_name))
            if values["total_surplus"] > 0:
                result.append(self._metric(resource, "qiniu_package_remaining_percent",
                                           values["month_remain"] / values["total_surplus"] * 100, "percent",
                                           now, dimensions, resource_name))
        return result

    def _resource_packages(self, options, now):
        selected = {(p["order_hash"], _integer(p["po_id"], "options.packages.po_id", 1))
                    for p in options["packages"]}
        result, found = [], set()
        for row in self._pages("v1/respack/list", options, now):
            identity = (require_text(row.get("order_hash"), "data.order_hash"),
                        _integer(row.get("po_id"), "data.po_id", 1))
            if identity not in selected:
                continue
            if identity in found:
                raise ProviderError("duplicate_resource", "data")
            found.add(identity)
            end = _time(row.get("effective_end"), "data.effective_end")
            start = _time(row.get("effective_start"), "data.effective_start")
            source = _time(row.get("data_update_time"), "data.data_update_time")
            age = (now - source).total_seconds()
            if age < -300 or age > options.get("max_source_age_seconds", 172800):
                raise ProviderError("source_data_stale", "data.data_update_time")
            status = _integer(row.get("status"), "data.status", 1)
            if status not in {1, 2, 3, 4} or end < start:
                raise ProviderError("response_schema_invalid", "data.status")
            name = require_text(row.get("respack_name"), "data.respack_name")
            dimensions = {"order_hash": identity[0], "po_id": str(identity[1]),
                          "source_time": source.isoformat(), "expires_at": end.isoformat(),
                          "source_unit": "seconds", "status": str(status)}
            resource = "package:" + identity[0] + ":" + str(identity[1])
            for metric, value, unit in (
                ("qiniu_package_expiry_days", (end - now).total_seconds() / 86400, "days"),
                ("qiniu_package_unavailable", int(status in {3, 4} or end <= now or start > now), "boolean"),
                ("qiniu_package_data_age_seconds", max(0, age), "seconds"),
            ):
                result.append(self._metric(resource, metric, value, unit, now, dimensions, name))
        if found != selected:
            raise ProviderError("target_not_found", "options.packages")
        return result

    def _latest(self, times, now, max_age, field, pattern=None):
        if not isinstance(times, list):
            raise ProviderError("response_schema_invalid", field)
        points = [_time(value, field, pattern=pattern) for value in times]
        if len(set(points)) != len(points):
            raise ProviderError("response_schema_invalid", field)
        # A five-minute interval must be complete and allow five minutes for reporting delay.
        candidates = [(point, index) for index, point in enumerate(points)
                      if point + timedelta(minutes=10) <= now]
        if not candidates:
            raise ProviderError("source_not_ready", field)
        source, index = max(candidates)
        if (now - source).total_seconds() > max_age:
            raise ProviderError("source_data_stale", field)
        return source, index

    @staticmethod
    def _series(values, size, index, field):
        if not isinstance(values, list) or len(values) != size:
            raise ProviderError("response_schema_invalid", field)
        return _nonnegative(values[index], field)

    def _kodo_usage(self, options, now):
        local = now.astimezone(CHINA)
        begin = local.replace(hour=0, minute=0, second=0, microsecond=0)
        result = []
        for bucket in options["buckets"]:
            for endpoint, metric, unit in (("space", "qiniu_storage_bytes", "bytes"),
                                          ("count", "qiniu_object_count", "count")):
                query = {"begin": begin.strftime("%Y%m%d%H%M%S"), "end": local.strftime("%Y%m%d%H%M%S"),
                         "g": "5min", "bucket": bucket}
                data = self._request("GET", "https://api.qiniuapi.com/v6/" + endpoint + "?" + urlencode(query),
                                     now)
                source, index = self._latest(data.get("times"), now, options.get("max_source_age_seconds", 1800),
                                             "times")
                value = self._series(data.get("datas"), len(data["times"]), index, "datas")
                dimensions = {"storage_class": "standard", "source_time": source.isoformat(),
                              "source_unit": "Byte" if endpoint == "space" else "count", "raw_value": str(value)}
                result.append(self._metric(bucket, metric, value, unit, source, dimensions))
        return result

    def _cdn_usage(self, options, now):
        local = now.astimezone(CHINA)
        payload = {"domains": ";".join(options["domains"]),
                   "startDate": (local - timedelta(days=1)).strftime("%Y-%m-%d"),
                   "endDate": local.strftime("%Y-%m-%d"), "granularity": "5min"}
        result = []
        for endpoint, metric, unit in (("flow", "qiniu_cdn_flow_bytes", "bytes"),
                                      ("bandwidth", "qiniu_cdn_bandwidth_bps", "bits/s")):
            response = self._request("POST", FUSION + "/v2/tune/monitoring/" + endpoint, now, payload, True)
            source, index = self._latest(response.get("time"), now, options.get("max_source_age_seconds", 1800),
                                         "time")
            data = _mapping(response.get("data"), "data")
            for domain in options["domains"]:
                values = _mapping(data.get(domain), "data.domain")
                for region in ("china", "oversea"):
                    value = self._series(values.get(region), len(response["time"]), index, "data.domain.region")
                    dimensions = {"region": region, "source_time": source.isoformat(), "source_unit": unit,
                                  "window_seconds": 300, "raw_value": str(value)}
                    result.append(self._metric(domain, metric, value, unit, source, dimensions))
        return result

    def _cdn_status(self, options, now):
        local = now.astimezone(CHINA)
        result = []
        for domain in options["domains"]:
            payload = {"domains": [domain], "startDate": (local - timedelta(days=1)).strftime("%Y-%m-%d"),
                       "endDate": local.strftime("%Y-%m-%d"), "freq": "5min", "region": "global", "isp": "all"}
            response = self._request("POST", FUSION + "/v2/tune/loganalyze/statuscode", now, payload, True)
            data = _mapping(response.get("data"), "data")
            source, index = self._latest(data.get("points"), now, options.get("max_source_age_seconds", 1800),
                                         "data.points", "%Y-%m-%d-%H-%M")
            codes = _mapping(data.get("codes"), "data.codes")
            if not codes:
                raise ProviderError("response_schema_invalid", "data.codes")
            total, errors = Decimal(0), Decimal(0)
            for code, values in codes.items():
                if not re.fullmatch(r"[1-5][0-9]{2}", code):
                    raise ProviderError("response_schema_invalid", "data.codes")
                value = self._series(values, len(data["points"]), index, "data.codes.count")
                if value != value.to_integral_value():
                    raise ProviderError("numeric_value_invalid", "data.codes.count")
                total += value
                if code.startswith("5"):
                    errors += value
            dimensions = {"region": "global", "source_time": source.isoformat(), "window_seconds": 300}
            for metric, value in (("qiniu_cdn_requests", total), ("qiniu_cdn_5xx_requests", errors)):
                result.append(self._metric(domain, metric, value, "count", source, dimensions))
            if total >= _integer(options.get("minimum_requests", 100), "options.minimum_requests", 1):
                result.append(self._metric(domain, "qiniu_cdn_5xx_percent", errors / total * 100,
                                           "percent", source, dimensions))
        return result
