"""Read-only Huawei Cloud BSS, ECS, RDS, SCM and CES collectors."""

from __future__ import annotations

import importlib
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from cloud_monitor.alerting import certificate_domains
from cloud_monitor.models import Metric

SUPPORTED_KINDS = frozenset(
    {"ecs_expiry", "rds_expiry", "slb_expiry", "certificates", "balance", "packages", "metrics", "network_traffic"}
)
SERVICE_TYPES = {
    "ecs_expiry": "hws.service.type.ec2",
    "slb_expiry": "hws.service.type.elb",
}


class HuaweiCollectionError(RuntimeError):
    """An API request or expected response cannot produce trustworthy data."""


def endpoint_region(service: str, region: str):
    versions = {"bss": "v2", "scm": "v3", "ces": "v1", "rds": "v3", "ecs": "v2"}
    module = importlib.import_module(
        f"huaweicloudsdk{service}.{versions[service]}.region.{service}_region"
    )
    try:
        return getattr(module, f"{service.title()}Region").value_of(region)
    except (KeyError, TypeError):
        defaults = {"bss": "cn-north-1", "scm": "cn-north-4"}
        if service in defaults:
            raise ValueError(
                f"Huawei {service} endpoint region is unsupported; "
                f"set account.{service}_region to {defaults[service]} for mainland China"
            ) from None
        raise ValueError(f"Huawei {service} endpoint region is unsupported; check the resource region") from None


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _number(value: Any, label: str) -> float:
    if value is None or isinstance(value, bool):
        raise HuaweiCollectionError(f"Missing or invalid numeric field: {label}")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise HuaweiCollectionError(f"Invalid numeric field: {label}") from exc
    if not math.isfinite(result):
        raise HuaweiCollectionError(f"Non-finite numeric field: {label}")
    return result


def _time(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HuaweiCollectionError(f"Invalid time field: {label}") from exc
    else:
        raise HuaweiCollectionError(f"Missing time field: {label}")
    # BSS and SCM expiry fields are documented in UTC, including strings without offsets.
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _rows(response: Any, key: str) -> list:
    result = _get(response, key)
    if not isinstance(result, list):
        raise HuaweiCollectionError(f"Response has no valid {key} list")
    return result


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


class HuaweiCollector:
    SUPPORTED_KINDS = SUPPORTED_KINDS

    def __init__(self, account: dict):
        self.account = account
        self._clients: dict[tuple[str, str], Any] = {}
        self._network_collector = None

    def _client(self, service: str, region: str):
        key = (service, region)
        if key in self._clients:
            return self._clients[key]
        resolved_region = endpoint_region(service, region)
        from huaweicloudsdkcore.auth.credentials import BasicCredentials, GlobalCredentials
        from huaweicloudsdkcore.http.http_config import HttpConfig

        ak, sk = self.account.get("access_key_id"), self.account.get("access_key_secret")
        if not ak or not sk:
            raise ValueError("Huawei access_key_id and access_key_secret are required")
        versions = {"bss": "v2", "scm": "v3", "ces": "v1", "rds": "v3", "ecs": "v2"}
        module = importlib.import_module(f"huaweicloudsdk{service}.{versions[service]}")
        if service in {"ces", "rds", "ecs"}:
            project_id = self.account.get("project_ids", {}).get(region)
            if not project_id:
                raise ValueError(f"Huawei project_ids[{region}] is required for {service.upper()}")
            credentials = BasicCredentials(ak, sk, project_id)
        else:
            credentials = GlobalCredentials(ak, sk, self.account.get("domain_id") or None)
        config = HttpConfig.get_default_config()
        config.timeout = (5, float(self.account.get("timeout_seconds", 20)))
        config.retry_times = 2
        client = (
            getattr(module, f"{service.title()}Client").new_builder()
            .with_credentials(credentials)
            .with_http_config(config)
            .with_region(resolved_region)
            .build()
        )
        self._clients[key] = client
        return client

    def _call(self, service: str, region: str, operation: str, request: Any):
        try:
            return getattr(self._client(service, region), operation)(request)
        except (ValueError, HuaweiCollectionError):
            raise
        except Exception as exc:
            status = getattr(exc, "status_code", "unknown")
            code = getattr(exc, "error_code", type(exc).__name__)
            # Provider error messages can echo request headers or credentials.
            raise HuaweiCollectionError(
                f"Huawei {service}.{operation} failed (status={status}, code={code})"
            ) from None

    def _metric(self, rid: str, name: str, metric: str, value: float, unit: str,
                now: datetime, dimensions: dict | None = None) -> Metric:
        if not rid:
            raise HuaweiCollectionError("Resource has no identifier")
        return Metric(provider="huawei", account=self.account["id"], resource_id=str(rid),
                      resource_name=name or str(rid), metric=metric, value=value,
                      unit=unit, observed_at=now, dimensions=dimensions or {})

    def _expiry(self, rid: str, name: str, expires: Any, now: datetime,
                dimensions: dict) -> Metric:
        expiry = _time(expires, f"{rid}.expire_time")
        return self._metric(rid, name, "expiry_days", (expiry - now).total_seconds() / 86400,
                            "days", now, {**dimensions, "expires_at": expiry.isoformat()})

    def _pages(self, fetch, field: str, size: int, max_pages: int = 10000):
        offset = 0
        for _ in range(max_pages):
            response = fetch(offset, size)
            rows = _rows(response, field)
            total = _get(response, "total_count")
            if not rows:
                if total is not None and offset < int(total):
                    raise HuaweiCollectionError("Pagination ended before total_count")
                return
            yield from rows
            offset += len(rows)
            if total is not None:
                if offset >= int(total):
                    return
            elif len(rows) < size:
                return
        raise HuaweiCollectionError("Pagination exceeded safety limit")

    def collect(self, kind: str, options: dict, now: datetime) -> list[Metric]:
        if kind not in SUPPORTED_KINDS:
            raise ValueError(f"Unsupported Huawei collector kind: {kind}")
        now = _time(now, "now")
        if kind in SERVICE_TYPES:
            return self._subscriptions(kind, options, now)
        return getattr(self, f"_{kind}")(options, now)

    def _subscriptions(self, kind: str, options: dict, now: datetime) -> list[Metric]:
        from huaweicloudsdkbss.v2 import ListPayPerUseCustomerResourcesRequest, QueryResourcesReq

        service_type = options.get("service_type_code", SERVICE_TYPES[kind])
        region = self.account.get("bss_region", "cn-north-1")
        requested = options.get("resource_ids", [])
        if len(requested) > 50:
            raise ValueError("Huawei subscription resource_ids accepts at most 50 IDs per job")

        def fetch(offset, size):
            body = QueryResourcesReq(service_type_code=service_type, only_main_resource=1,
                                     status_list=[2, 4, 5], offset=offset, limit=size,
                                     resource_ids=requested or None)
            return self._call("bss", region, "list_pay_per_use_customer_resources",
                              ListPayPerUseCustomerResourcesRequest(body=body))

        result = []
        seen = set()
        for row in self._pages(fetch, "data", 100):
            rid = _get(row, "resource_id")
            if requested and rid not in requested:
                continue
            if _get(row, "service_type_code") not in (None, service_type):
                continue
            selected_regions = options.get("regions", self.account.get("regions", []))
            if selected_regions and _get(row, "region_code") not in selected_regions:
                continue
            seen.add(rid)
            result.append(self._expiry(rid, _get(row, "resource_name"), _get(row, "expire_time"),
                                       now, {"kind": kind, "region": _get(row, "region_code"),
                                             "service_type_code": service_type,
                                             "expire_policy": _get(row, "expire_policy")}))
        if requested and set(requested) - seen:
            raise HuaweiCollectionError("Configured subscription IDs absent from BSS response; check billing mode and region")
        return result

    def _rds_expiry(self, options: dict, now: datetime) -> list[Metric]:
        from huaweicloudsdkrds.v3 import ListInstancesRequest

        regions = options.get("regions", self.account.get("regions", []))
        if not regions:
            raise ValueError("Huawei RDS expiry requires at least one region")
        engines = options.get("engines", ["MySQL", "PostgreSQL"])
        if not engines or not set(engines).issubset({"MySQL", "PostgreSQL"}):
            raise ValueError("Huawei RDS engines must be MySQL and/or PostgreSQL")
        requested = set(options.get("resource_ids", []))
        seen = set()
        result = []
        for region in regions:
            for engine in engines:
                def fetch(offset, size):
                    request = ListInstancesRequest(datastore_type=engine, offset=offset, limit=size,
                                                   eps_id=options.get("enterprise_project_id"))
                    return self._call("rds", region, "list_instances", request)

                for row in self._pages(fetch, "instances", 100):
                    rid = _get(row, "id")
                    if requested and rid not in requested:
                        continue
                    datastore = _get(_get(row, "datastore"), "type")
                    if not isinstance(datastore, str):
                        raise HuaweiCollectionError("RDS instance has no datastore.type")
                    if datastore.lower() != engine.lower():
                        continue
                    seen.add(rid)
                    charge_mode = _get(_get(row, "charge_info"), "charge_mode")
                    if charge_mode == "postPaid":
                        continue
                    if charge_mode != "prePaid":
                        raise HuaweiCollectionError("RDS instance has unknown charge_info.charge_mode")
                    result.append(self._expiry(rid, _get(row, "name"), _get(row, "expiration_time"),
                                               now, {"kind": "rds_expiry", "region": region, "engine": engine}))
        if requested - seen:
            raise HuaweiCollectionError("Configured RDS IDs absent from selected MySQL/PostgreSQL inventory")
        return result

    def _balance(self, options: dict, now: datetime) -> list[Metric]:
        from huaweicloudsdkbss.v2 import ShowCustomerAccountBalancesRequest

        response = self._call("bss", self.account.get("bss_region", "cn-north-1"),
                              "show_customer_account_balances", ShowCustomerAccountBalancesRequest())
        balance_type = _integer(options.get("account_type", 1), "account_type")
        rows = [r for r in _rows(response, "account_balances") if _get(r, "account_type") == balance_type]
        if not rows:
            raise HuaweiCollectionError(f"Balance account_type={balance_type} absent from response")
        result = []
        for row in rows:
            currency = _get(row, "currency") or _get(response, "currency")
            if not currency:
                raise HuaweiCollectionError("Balance currency absent from response")
            if _get(row, "measure_id", 1) not in (None, 1):
                raise HuaweiCollectionError("Unsupported balance measure_id; expected currency units")
            result.append(self._metric(_get(row, "account_id") or self.account["id"],
                                       f"{self.account['id']} account balance", "balance",
                                       _number(_get(row, "amount"), "balance.amount"), currency,
                                       now, {"kind": "balance", "account_type": balance_type}))
        return result

    def _certificates(self, options: dict, now: datetime) -> list[Metric]:
        from huaweicloudsdkscm.v3 import ListCertificatesRequest

        region = options.get("region", self.account.get("scm_region", "cn-north-4"))

        def fetch(offset, size):
            request = ListCertificatesRequest(limit=size, offset=offset, status="ALL",
                                              enterprise_project_id=options.get("enterprise_project_id"))
            return self._call("scm", region, "list_certificates", request)

        result = []
        selected = options.get("resource_ids", [])
        for row in self._pages(fetch, "certificates", 50):
            rid = _get(row, "id")
            if selected and rid not in selected:
                continue
            if _get(row, "status") not in {"ISSUED", "EXPIRED", "UPLOAD"}:
                continue
            result.append(self._expiry(rid, _get(row, "name") or _get(row, "domain"),
                                       _get(row, "expire_time"), now,
                                       {"kind": "certificates", "domain": _get(row, "domain"), "region": region,
                                        "domains": certificate_domains(_get(row, "domain"), _get(row, "sans"))}))
        if selected and set(selected) - {m.resource_id for m in result}:
            raise HuaweiCollectionError("Configured certificate IDs absent or not issued/uploaded/expired")
        return result

    def _packages(self, options: dict, now: datetime) -> list[Metric]:
        from huaweicloudsdkbss.v2 import ListFreeResourceInfosReq, ListFreeResourceInfosRequest

        service_types = options.get("service_type_codes", ["hws.service.type.obs"])
        if not service_types:
            raise ValueError("Huawei package service_type_codes must not be empty")
        region = self.account.get("bss_region", "cn-north-1")

        def fetch(offset, size):
            request = ListFreeResourceInfosRequest(body=ListFreeResourceInfosReq(
                offset=offset, limit=size, service_type_code_list=service_types,
                region_code=options.get("region"), product_id=options.get("product_id")))
            return self._call("bss", region, "list_free_resource_infos", request)

        result = []
        for row in self._pages(fetch, "free_resource_packages", 100):
            if _get(row, "status") not in (1, 2, 3):
                continue
            rid = _get(row, "order_instance_id")
            selected = options.get("resource_ids", [])
            if selected and rid not in selected:
                continue
            name = _get(row, "product_name") or rid
            dimensions = {"kind": "packages", "region": _get(row, "region_code"),
                          "service_type_code": _get(row, "service_type_code"),
                          "quota_reuse_mode": _get(row, "quota_reuse_mode")}
            result.append(self._expiry(rid, name, _get(row, "expire_time"), now, dimensions))
            for item in _rows(row, "free_resources"):
                item_id = _get(item, "free_resource_id")
                if not item_id:
                    raise HuaweiCollectionError("Package resource item has no free_resource_id")
                remaining = _number(_get(item, "amount"), f"{item_id}.amount")
                total = _number(_get(item, "original_amount"), f"{item_id}.original_amount")
                if total <= 0 or remaining < 0:
                    raise HuaweiCollectionError("Package quota must have positive total and nonnegative remaining amount")
                item_dimensions = {**dimensions, "package_id": rid,
                                   "usage_type_name": _get(item, "usage_type_name"),
                                   "measure_id": _get(item, "measure_id"),
                                   "remaining": remaining, "total": total}
                result.extend([
                    self._metric(item_id, f"{name} / {_get(item, 'usage_type_name') or item_id}",
                                 "package_remaining_percent", remaining / total * 100, "%", now, item_dimensions),
                    self._metric(item_id, f"{name} / {_get(item, 'usage_type_name') or item_id}",
                                 "package_used_percent", (total - remaining) / total * 100, "%", now, item_dimensions),
                ])
        selected = options.get("resource_ids", [])
        if selected and set(selected) - {m.resource_id for m in result if m.metric == "expiry_days"}:
            raise HuaweiCollectionError("Configured package IDs absent or not active/exhausted/expired")
        return result

    def _network_traffic(self, options: dict, now: datetime) -> list[Metric]:
        from .network_traffic import NetworkTrafficCollector
        if self._network_collector is None:
            self._network_collector = NetworkTrafficCollector(self, "huawei")
        return self._network_collector.collect(options, now)

    def _metrics(self, options: dict, now: datetime) -> list[Metric]:
        from huaweicloudsdkces.v1 import ShowMetricDataRequest

        queries = options.get("metrics", [])
        if not queries:
            raise ValueError("Huawei metrics collector needs explicit options.metrics entries")
        result = []
        for spec in queries:
            for field in ("namespace", "metric_name", "region", "resource_id", "output_metric"):
                if not spec.get(field):
                    raise ValueError(f"Huawei CES metric requires {field}")
            dimensions = spec.get("dimensions")
            if not isinstance(dimensions, dict) or not 1 <= len(dimensions) <= 4:
                raise ValueError("Huawei CES requires 1-4 ordered dimensions as a mapping")
            period = _integer(spec.get("period", 300), "CES period")
            if period not in (1, 60, 300, 1200, 3600, 14400, 86400):
                raise ValueError("Unsupported Huawei CES period")
            aggregate = spec.get("filter", "max")
            if aggregate not in ("average", "min", "max", "sum", "variance"):
                raise ValueError("Unsupported Huawei CES filter")
            lookback = _integer(spec.get("lookback_seconds", max(period * 3, 900)), "lookback_seconds")
            max_age = _integer(spec.get("max_age_seconds", lookback), "max_age_seconds")
            if lookback < period or max_age <= 0:
                raise ValueError("CES lookback must cover a period and max_age_seconds must be positive")
            request_args = {f"dim_{index}": f"{key},{value}" for index, (key, value) in enumerate(dimensions.items())}
            request = ShowMetricDataRequest(namespace=spec["namespace"], metric_name=spec["metric_name"],
                                            filter=aggregate, period=period,
                                            _from=int((now - timedelta(seconds=lookback)).timestamp() * 1000),
                                            to=int(now.timestamp() * 1000), **request_args)
            response = self._call("ces", spec["region"], "show_metric_data", request)
            points = _rows(response, "datapoints")
            if not points:
                raise HuaweiCollectionError(f"CES has no datapoints for {spec['resource_id']}/{spec['metric_name']}")
            stamped = [(point, _number(_get(point, "timestamp"), "CES timestamp")) for point in points]
            point, timestamp = max(stamped, key=lambda entry: entry[1])
            observed = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
            age = (now - observed).total_seconds()
            if age > max_age or age < -60:
                raise HuaweiCollectionError(f"CES datapoint is stale or in the future for {spec['resource_id']}")
            value = _number(_get(point, aggregate), f"CES {aggregate}")
            output = spec["output_metric"]
            network = output in {"network_in_bps", "network_out_bps"}
            multiplier = spec.get("multiplier", 1)
            if network and spec.get("source_units"):
                source_unit = _get(point, "unit")
                if not isinstance(source_unit, str) or source_unit not in spec["source_units"]:
                    raise HuaweiCollectionError("CES network rate has an unexpected unit")
                multiplier = spec["source_units"][source_unit]
            value *= _number(multiplier, "multiplier")
            if network and value < 0:
                raise HuaweiCollectionError("CES returned a negative network rate")
            if output.endswith("_percent") or output.endswith("_ratio"):
                limit = _number(spec.get("limit"), "normalization limit")
                if limit <= 0:
                    raise ValueError("Huawei normalized metric limit must be positive")
                value /= limit
                if output.endswith("_percent"):
                    value *= 100
            unit = "%" if output.endswith("_percent") else "ratio" if output.endswith("_ratio") else _get(point, "unit") or spec.get("unit", "")
            if network:
                unit = "bit/s"
            metadata = {key: spec.get(key, options.get(key)) for key in ("product", "scope", "engine")
                        if spec.get(key, options.get(key)) is not None}
            identity = {"region": spec["region"], "namespace": spec["namespace"],
                        "metric_name": spec["metric_name"], "dimensions": dimensions}
            if network:
                identity.update(metadata)
            timing = {"sample_max_gap_seconds": spec["sample_max_gap_seconds"]} if network and "sample_max_gap_seconds" in spec else {}
            if network:
                timing["source_unit"] = _get(point, "unit")
            result.append(self._metric(spec["resource_id"], spec.get("resource_name", spec["resource_id"]),
                                       output, value, unit, observed,
                                        {**dimensions, **metadata, **timing, "kind": "metrics", "region": spec["region"],
                                        "namespace": spec["namespace"], "metric_name": spec["metric_name"],
                                         "identity": identity,
                                        "period": period, "filter": aggregate}))
        return result
