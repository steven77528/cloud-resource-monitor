"""Read-only WeChat mini program APIs using a business-managed token cache."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode

from cloud_monitor.models import Metric
from cloud_monitor.providers.common import (
    ProviderError,
    decimal_number,
    number,
    read_secret_file,
    request_json,
    require_text,
    timestamp,
)
from cloud_monitor.providers.relay import trusted_relay_client


SUPPORTED_KINDS = frozenset({"api_quota", "charge_packages", "annual_review", "js_errors", "performance"})
CHINA = timezone(timedelta(hours=8))
BASE_URL = "https://api.weixin.qq.com"
CGI_PATH = re.compile(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\Z")
HASH = re.compile(r"[A-Fa-f0-9]{16,64}\Z")


class WechatAPIError(ProviderError):
    def __init__(self, code: int):
        super().__init__("wechat_api_error", "response.errcode")
        self.api_code = code
        self.args = (f"{self.reason} field={self.field} code={code}",)


def _integer(value, field: str, minimum: int = 0) -> int:
    parsed = decimal_number(value, field)
    if parsed != parsed.to_integral_value() or parsed < minimum:
        raise ProviderError("invalid_integer", field)
    return int(parsed)


def _option_integer(value, field: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ProviderError("invalid_option", field)
    return value


def _mapping(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise ProviderError("invalid_object", field)
    return value


def _text_list(value, field: str, *, required: bool = True) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise ProviderError("invalid_list", field)
    items = [require_text(item, field) for item in value]
    if len(set(items)) != len(items):
        raise ProviderError("duplicate_item", field)
    return items


def validate_account(account: dict) -> None:
    _mapping(account, "account")
    for field in ("id", "appid"):
        require_text(account.get(field), f"account.{field}")
    if trusted_relay_client(account) is None:
        require_text(account.get("token_file"), "account.token_file")
    if account.get("token_type", "access_token") not in {"access_token", "authorizer_access_token"}:
        raise ProviderError("invalid_option", "account.token_type")
    if type(account.get("third_party_authorized", False)) is not bool:
        raise ProviderError("invalid_boolean", "account.third_party_authorized")
    permissions = account.get("permission_sets", [])
    if not isinstance(permissions, list) or not all(type(item) is int and item > 0 for item in permissions):
        raise ProviderError("invalid_list", "account.permission_sets")
    timeout = number(account.get("request_timeout_seconds", 20), "account.request_timeout_seconds")
    if not 0 < timeout <= 120:
        raise ProviderError("invalid_option", "account.request_timeout_seconds")


def validate_options(kind: str, options: dict) -> None:
    if kind not in SUPPORTED_KINDS:
        raise ProviderError("unsupported_kind", "kind")
    _mapping(options, "options")
    if kind in {"charge_packages", "js_errors"}:
        _option_integer(options.get("max_pages", 100), "options.max_pages", 1000)
    if kind == "api_quota":
        for path in _text_list(options.get("cgi_paths"), "options.cgi_paths"):
            if not CGI_PATH.fullmatch(path) or "/sns/" in path or ".." in path.split("/"):
                raise ProviderError("invalid_cgi_path", "options.cgi_paths")
    elif kind == "charge_packages":
        products = options.get("products")
        if not isinstance(products, list) or not products:
            raise ProviderError("invalid_list", "options.products")
        seen = set()
        for product in products:
            _mapping(product, "options.products")
            spu = require_text(product.get("spu_id"), "options.products.spu_id")
            if spu in seen:
                raise ProviderError("duplicate_item", "options.products.spu_id")
            seen.add(spu)
            product_type = product.get("product_type")
            if product_type not in {"resource_pack", "subscription"}:
                raise ProviderError("invalid_option", "options.products.product_type")
            if product_type == "resource_pack":
                require_text(product.get("unit"), "options.products.unit")
            if "resource_name" in product:
                require_text(product["resource_name"], "options.products.resource_name")
            _text_list(product.get("track_expired_package_ids", []),
                       "options.products.track_expired_package_ids", required=False)
    elif kind == "annual_review":
        if options.get("method", "POST") not in {"POST", "GET"}:
            raise ProviderError("invalid_option", "options.method")
    elif kind == "js_errors":
        require_text(options.get("app_version", "0"), "options.app_version")
        if options.get("error_type", "0") not in {"0", "1", "2", "3"}:
            raise ProviderError("invalid_option", "options.error_type")
        _option_integer(options.get("day_offset", 1), "options.day_offset", 7)
    elif kind == "performance":
        types = options.get("cost_time_types", [1, 2, 3])
        if (not isinstance(types, list) or not types or len(set(map(str, types))) != len(types)
                or any(type(item) is not int or item not in {1, 2, 3} for item in types)):
            raise ProviderError("invalid_list", "options.cost_time_types")
        _option_integer(options.get("lookback_days", 8), "options.lookback_days", 30)
        _option_integer(options.get("max_data_age_days", 2), "options.max_data_age_days", 30)
        for field in ("device", "networktype", "scene", "is_download_code"):
            require_text(options.get(field, "@_all"), f"options.{field}")


def validate_job(account: dict, kind: str, options: dict) -> None:
    validate_account(account)
    validate_options(kind, options)
    third_party = account.get("token_type", "access_token") == "authorizer_access_token"
    required = 30 if kind == "annual_review" else 18 if kind in {
        "charge_packages", "js_errors", "performance"} else None
    if kind == "annual_review" and not third_party:
        raise ProviderError("third_party_authorization_required", "account.token_type")
    if third_party:
        if not account.get("third_party_authorized", False):
            raise ProviderError("third_party_authorization_required", "account.third_party_authorized")
        if required and required not in account.get("permission_sets", []):
            raise ProviderError("permission_set_required", "account.permission_sets")


class WechatCollector:
    SUPPORTED_KINDS = SUPPORTED_KINDS

    def __init__(self, account: dict, transport=None):
        validate_account(account)
        self.account = account
        self.account_id = account["id"]
        self.appid = account["appid"]
        self._transport = transport or request_json

    def collect(self, kind: str, options: dict, now: datetime) -> list[Metric]:
        validate_job(self.account, kind, options)
        if now.tzinfo is None:
            raise ProviderError("invalid_timestamp", "now")
        token = None if trusted_relay_client(self.account) is not None else self._token(now)
        return getattr(self, f"_{kind}")(options, now, token)

    def _token(self, now: datetime) -> str:
        raw = read_secret_file(self.account["token_file"], "account.token_file")
        try:
            cache = json.loads(raw)
        except (ValueError, TypeError):
            raise ProviderError("invalid_token_cache", "account.token_file") from None
        _mapping(cache, "token_cache")
        token_type = self.account.get("token_type", "access_token")
        token = require_text(cache.get(token_type), f"token_cache.{token_type}")
        if any(character.isspace() for character in token):
            raise ProviderError("invalid_token_cache", "token_cache.token")
        expires_at = timestamp(_integer(cache.get("expires_at"), "token_cache.expires_at", 1),
                               "token_cache.expires_at")
        if expires_at <= now + timedelta(seconds=30):
            raise ProviderError("token_cache_expired", "token_cache.expires_at")
        if cache.get("appid") != self.appid:
            raise ProviderError("token_account_mismatch", "token_cache.appid")
        return token

    def _request(self, method: str, path: str, token: str, params: dict | None = None) -> dict:
        relay = trusted_relay_client(self.account)
        query = {} if relay is not None else {"access_token": token}
        body = None
        if method == "GET":
            query.update(params or {})
        elif params is not None:
            body = json.dumps(params, separators=(",", ":")).encode("utf-8")
        try:
            url = BASE_URL + path + ("?" + urlencode(query) if query else "")
            if relay is not None:
                response = relay.request(method, url, body=params if method == "POST" else None)
            else:
                response = self._transport(
                    method, url, headers={"Content-Type": "application/json"}, body=body,
                    timeout=float(self.account.get("request_timeout_seconds", 20)),
                )
        except ProviderError:
            raise
        except Exception:
            # Transport exceptions can embed the URL, including access_token.
            raise ProviderError("wechat_request_failed", "transport") from None
        response = _mapping(response, "response")
        code = response.get("errcode")
        if type(code) is not int:
            raise ProviderError("invalid_response", "response.errcode")
        if code != 0:
            if not -1000000000 <= code <= 1000000000:
                raise ProviderError("invalid_response", "response.errcode")
            raise WechatAPIError(code)
        if response.get("success") is False:
            raise ProviderError("wechat_api_unsuccessful", "response.success")
        return response

    def _metric(self, resource_id: str, name: str, metric: str, value, unit: str,
                now: datetime, dimensions: dict | None = None) -> Metric:
        dimensions = {"appid": self.appid, "source": "wechat_official_api", **(dimensions or {})}
        dimensions.setdefault("identity", {"appid": self.appid, "unit": unit})
        return Metric("wechat", self.account_id, resource_id, name, metric,
                      number(value, metric), unit, now, dimensions)

    def _api_quota(self, options: dict, now: datetime, token: str) -> list[Metric]:
        result = []
        for path in options["cgi_paths"]:
            response = self._request("POST", "/cgi-bin/openapi/quota/get", token, {"cgi_path": path})
            quota = _mapping(response.get("quota"), "response.quota")
            limit = _integer(quota.get("daily_limit"), "quota.daily_limit")
            used = _integer(quota.get("used"), "quota.used")
            remain = _integer(quota.get("remain"), "quota.remain")
            dims = {"cgi_path": path, "data_date": now.astimezone(CHINA).date().isoformat()}
            for name, value in (("api_quota_daily_limit", limit), ("api_quota_used", used),
                                ("api_quota_remaining", remain)):
                result.append(self._metric(path, path, name, value, "count", now, dims))
            if limit > 0:
                result.append(self._metric(path, path, "api_quota_remaining_percent",
                                           Decimal(remain) * 100 / Decimal(limit), "percent", now, dims))
        return result

    def _package_pages(self, product: dict, options: dict, token: str) -> list[dict]:
        rows, seen = [], set()
        total = None
        for _ in range(options.get("max_pages", 100)):
            response = self._request("GET", "/wxa/charge/usage/get", token,
                                     {"spuId": product["spu_id"], "offset": len(rows), "limit": 20})
            count = _integer(response.get("total"), "response.total")
            if total is not None and total != count:
                raise ProviderError("pagination_changed", "response.total")
            total = count
            page = response.get("detailList")
            if not isinstance(page, list) or len(page) > 20:
                raise ProviderError("invalid_list", "response.detailList")
            for row in page:
                _mapping(row, "detailList")
                pkg_id = require_text(row.get("pkgId"), "detailList.pkgId")
                if pkg_id in seen:
                    raise ProviderError("pagination_duplicate", "detailList.pkgId")
                seen.add(pkg_id)
                if require_text(row.get("spuId"), "detailList.spuId") != product["spu_id"]:
                    raise ProviderError("product_mismatch", "detailList.spuId")
            rows.extend(page)
            if len(rows) > total:
                raise ProviderError("pagination_invalid", "response.total")
            if len(rows) == total:
                return rows
            if not page:
                raise ProviderError("pagination_incomplete", "response.detailList")
        raise ProviderError("pagination_limit", "options.max_pages")

    def _charge_packages(self, options: dict, now: datetime, token: str) -> list[Metric]:
        result = []
        for product in options["products"]:
            spu = product["spu_id"]
            name = product.get("resource_name", spu)
            dims = {"spu_id": spu, "product_type": product["product_type"]}
            if product["product_type"] == "subscription":
                response = self._request("GET", "/wxa/charge/usage/get", token,
                                         {"spuId": spu, "offset": 0, "limit": 20})
                start = timestamp(response.get("startServiceTime"), "response.startServiceTime")
                end = timestamp(response.get("endServiceTime"), "response.endServiceTime")
                if end <= start or end.timestamp() <= 0:
                    raise ProviderError("invalid_period", "response.endServiceTime")
                dims.update({"starts_at": start.isoformat(), "expires_at": end.isoformat()})
                result.append(self._metric(f"spu:{spu}", name, "subscription_expiry_days",
                                           (end - now).total_seconds() / 86400, "days", now, dims))
                result.append(self._metric(f"spu:{spu}", name, "subscription_active",
                                           int(start <= now < end), "bool", now, dims))
                continue
            rows = self._package_pages(product, options, token)
            active_total, active_used, active_count = 0, 0, 0
            unit = product["unit"]
            for row in rows:
                pkg_id = row["pkgId"]
                status = _integer(row.get("status"), "detailList.status", 1)
                if status not in {1, 2, 3}:
                    raise ProviderError("invalid_enum", "detailList.status")
                start = timestamp(row.get("startTime"), "detailList.startTime")
                end = timestamp(row.get("endTime"), "detailList.endTime")
                if end <= start:
                    raise ProviderError("invalid_period", "detailList.endTime")
                capacity = _integer(row.get("all"), "detailList.all")
                used = _integer(row.get("used"), "detailList.used")
                if used > capacity:
                    raise ProviderError("invalid_usage", "detailList.used")
                active = status == 1 and start <= now < end
                if active:
                    active_count += 1
                    active_total += capacity
                    active_used += used
                historical = status == 3 or end <= now
                if historical and pkg_id not in product.get("track_expired_package_ids", []):
                    continue
                package_dims = {**dims, "package_id": pkg_id, "status": status,
                                "starts_at": start.isoformat(), "expires_at": end.isoformat(),
                                "raw_total": str(capacity), "raw_used": str(used),
                                "raw_remaining": str(capacity - used), "quota_unit": unit,
                                "identity": {"appid": self.appid, "spu_id": spu, "quota_unit": unit}}
                resource = f"spu:{spu}:package:{pkg_id}"
                for metric, value, value_unit in (
                    ("package_total", capacity, unit), ("package_used", used, unit),
                    ("package_remaining", capacity - used, unit), ("package_status", status, "status"),
                    ("package_expiry_days", (end - now).total_seconds() / 86400, "days"),
                ):
                    result.append(self._metric(resource, f"{name} / {pkg_id}", metric, value,
                                               value_unit, now, package_dims))
                if active and capacity > 0:
                    result.append(self._metric(resource, f"{name} / {pkg_id}", "package_remaining_percent",
                                               Decimal(capacity - used) * 100 / Decimal(capacity),
                                               "percent", now, package_dims))
            aggregate_dims = {**dims, "quota_unit": unit, "raw_total": str(active_total),
                              "raw_used": str(active_used), "raw_remaining": str(active_total - active_used),
                              "identity": {"appid": self.appid, "spu_id": spu, "quota_unit": unit}}
            result.append(self._metric(f"spu:{spu}", name, "charge_active_package_count",
                                       active_count, "count", now, aggregate_dims))
            if active_count:
                for metric, value in (("charge_active_total", active_total), ("charge_active_used", active_used),
                                      ("charge_active_remaining", active_total - active_used)):
                    result.append(self._metric(f"spu:{spu}", name, metric, value, unit, now, aggregate_dims))
                if active_total > 0:
                    result.append(self._metric(f"spu:{spu}", name, "charge_active_remaining_percent",
                                               Decimal(active_total - active_used) * 100 / Decimal(active_total),
                                               "percent", now, aggregate_dims))
        return result

    def _annual_review(self, options: dict, now: datetime, token: str) -> list[Metric]:
        response = self._request(options.get("method", "POST"),
                                 "/cgi-bin/account/getaccountbasicinfo", token)
        if response.get("appid") != self.appid:
            raise ProviderError("account_mismatch", "response.appid")
        if type(response.get("account_type")) is not int or response["account_type"] != 3:
            raise ProviderError("account_type_mismatch", "response.account_type")
        verify = _mapping(response.get("wx_verify_info"), "response.wx_verify_info")
        verified = verify.get("qualification_verify")
        if type(verified) is not bool:
            raise ProviderError("invalid_boolean", "wx_verify_info.qualification_verify")
        name = self.account.get("name", self.appid)
        result = [self._metric(self.appid, name, "qualification_verified", int(verified), "bool", now)]
        if not verified:
            return result
        review = verify.get("annual_review")
        if type(review) is not bool:
            raise ProviderError("invalid_boolean", "wx_verify_info.annual_review")
        begin = timestamp(verify.get("annual_review_begin_time"), "wx_verify_info.annual_review_begin_time")
        end = timestamp(verify.get("annual_review_end_time"), "wx_verify_info.annual_review_end_time")
        if end <= begin or end.timestamp() <= 0:
            raise ProviderError("invalid_period", "wx_verify_info.annual_review_end_time")
        dims = {"review_begins_at": begin.isoformat(), "expires_at": end.isoformat()}
        result.append(self._metric(self.appid, name, "annual_review_required", int(review), "bool", now, dims))
        result.append(self._metric(self.appid, name, "annual_review_expiry_days",
                                   (end - now).total_seconds() / 86400, "days", now, dims))
        return result

    def _js_errors(self, options: dict, now: datetime, token: str) -> list[Metric]:
        day = now.astimezone(CHINA).date() - timedelta(days=options.get("day_offset", 1))
        window_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), CHINA)
        result, seen, offset, expected, occurrences = [], set(), 0, None, 0
        version, error_type = options.get("app_version", "0"), options.get("error_type", "0")
        dims = {"data_date": day.isoformat(), "window_end": window_end.isoformat(),
                "collected_at": now.isoformat(), "app_version": version, "error_type": error_type}
        for _ in range(options.get("max_pages", 100)):
            response = self._request("POST", "/wxaapi/log/jserr_list", token, {
                "appVersion": version, "errType": error_type,
                "startTime": day.isoformat(), "endTime": day.isoformat(), "keyword": "", "openid": "",
                "orderby": "pv", "desc": "1", "offset": offset, "limit": 30,
            })
            total = _integer(response.get("totalCount"), "response.totalCount")
            if expected is not None and expected != total:
                raise ProviderError("pagination_changed", "response.totalCount")
            expected = total
            rows = response.get("data")
            if not isinstance(rows, list) or len(rows) > 30:
                raise ProviderError("invalid_list", "response.data")
            for row in rows:
                _mapping(row, "data")
                hashes = tuple(require_text(row.get(field), f"data.{field}")
                               for field in ("errorMsgMd5", "errorStackMd5"))
                if any(not HASH.fullmatch(value) for value in hashes):
                    raise ProviderError("invalid_hash", "data.error_hash")
                if hashes in seen:
                    raise ProviderError("pagination_duplicate", "data.error_hash")
                seen.add(hashes)
                error_dims = {**dims, "error_message_hash": hashes[0], "error_stack_hash": hashes[1],
                              "identity": {"appid": self.appid, "app_version": version, "error_type": error_type}}
                resource = f"{self.appid}:js:{hashes[0]}:{hashes[1]}"
                occurrences += _integer(row.get("pv"), "data.pv")
                for field, metric in (("pv", "js_error_occurrences"), ("uv", "js_error_affected_users")):
                    result.append(self._metric(resource, hashes[0], metric,
                                               _integer(row.get(field), f"data.{field}"), "count",
                                               window_end, error_dims))
            offset += len(rows)
            if offset > total:
                raise ProviderError("pagination_invalid", "response.totalCount")
            if offset == total:
                for metric, value in (("js_error_group_count", total), ("js_error_total_occurrences", occurrences)):
                    result.append(self._metric(self.appid, self.appid, metric, value,
                                               "count", window_end, {**dims, "identity": {
                                                   "appid": self.appid, "app_version": version,
                                                   "error_type": error_type}}))
                return result
            if not rows:
                raise ProviderError("pagination_incomplete", "response.data")
        raise ProviderError("pagination_limit", "options.max_pages")

    def _performance(self, options: dict, now: datetime, token: str) -> list[Metric]:
        end = datetime.combine(now.astimezone(CHINA).date(), datetime.min.time(), CHINA)
        start = end - timedelta(days=options.get("lookback_days", 8))
        filters = {field: options.get(field, "@_all") for field in (
            "device", "networktype", "scene", "is_download_code")}
        result = []
        for kind in options.get("cost_time_types", [1, 2, 3]):
            response = self._request("POST", "/wxaapi/log/get_performance", token, {
                "cost_time_type": kind, "default_start_time": int(start.timestamp()),
                "default_end_time": int(end.timestamp()) - 1, **filters,
            })
            raw = response.get("default_time_data")
            if not isinstance(raw, str):
                raise ProviderError("invalid_json_field", "response.default_time_data")
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                raise ProviderError("invalid_json_field", "response.default_time_data") from None
            rows = _mapping(parsed, "default_time_data").get("list")
            if not isinstance(rows, list):
                raise ProviderError("invalid_list", "default_time_data.list")
            days = {}
            for row in rows:
                _mapping(row, "default_time_data.list")
                try:
                    date = datetime.strptime(require_text(row.get("ref_date"), "list.ref_date"), "%Y%m%d")
                    date = date.replace(tzinfo=CHINA)
                except ValueError:
                    raise ProviderError("invalid_date", "list.ref_date") from None
                if type(row.get("cost_time_type")) is not int or row["cost_time_type"] != kind:
                    raise ProviderError("metric_type_mismatch", "list.cost_time_type")
                cost = decimal_number(row.get("cost_time"), "list.cost_time")
                if cost < 0:
                    raise ProviderError("invalid_number", "list.cost_time")
                if date in days:
                    raise ProviderError("duplicate_date", "list.ref_date")
                if start <= date < end:
                    days[date] = cost
            if not days:
                raise ProviderError("no_complete_data", "default_time_data.list")
            latest = max(days)
            if end - latest > timedelta(days=options.get("max_data_age_days", 2)):
                raise ProviderError("stale_data", "list.ref_date")
            dims = {**filters, "data_date": latest.date().isoformat(), "collected_at": now.isoformat(),
                    "cost_time_type": kind, "identity": {"appid": self.appid, "cost_time_type": kind, **filters}}
            result.append(self._metric(f"{self.appid}:performance:{kind}", self.appid,
                                       "performance_cost_milliseconds", days[latest], "milliseconds",
                                       latest + timedelta(days=1), dims))
        return result
