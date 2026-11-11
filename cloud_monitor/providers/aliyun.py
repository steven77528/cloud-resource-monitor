"""Read-only Alibaba Cloud RPC collectors; credentials are loaded on first request."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from cloud_monitor.alerting import certificate_domains
from cloud_monitor.models import Metric


SUPPORTED_KINDS = frozenset(
    {"ecs_expiry", "rds_expiry", "rds_performance", "rds_cloudmonitor", "rds_sql", "domains", "certificates", "packages",
     "slb_expiry", "balance", "metrics", "sms_statistics", "network_traffic"}
)
UTC = timezone.utc
CHINA = timezone(timedelta(hours=8))
ENDPOINTS = {
    "ecs": "ecs.aliyuncs.com", "rds": "rds.aliyuncs.com",
    "bssopenapi": "business.aliyuncs.com", "domain": "domain.aliyuncs.com",
    "cas": "cas.aliyuncs.com", "dysmsapi": "dysmsapi.aliyuncs.com",
}


class SmsStatisticsInsufficientData(ValueError):
    """A successful statistics query cannot provide a trustworthy growth rate."""


def _required(data: dict, key: str) -> Any:
    value = data.get(key)
    if value is None or value == "":
        raise ValueError(f"Alibaba Cloud response/configuration is missing {key}")
    return value


def _number(value: Any, field: str) -> float:
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid numeric {field}: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"Non-finite numeric {field}")
    return result


def _time(value: Any, naive_timezone: timezone = UTC) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (float, int)) or str(value).isdigit():
        timestamp = _number(value, "timestamp")
        if timestamp >= 100_000_000_000:
            timestamp /= 1000
        result = datetime.fromtimestamp(timestamp, UTC)
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=naive_timezone)
    return result.astimezone(UTC)


def _path(data: dict, keys: tuple[str, ...]) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise ValueError(f"Alibaba Cloud response is missing {'.'.join(keys)}")
        current = current[key]
    return current


def _quota(value: Any, unit: str) -> tuple[float, str]:
    """BSS can return total and remaining quota in different storage units."""
    normalized = unit.strip().upper()
    sizes = {"B": 1, "BYTE": 1, "BYTES": 1, "KB": 1024, "MB": 1024**2,
             "GB": 1024**3, "TB": 1024**4, "PB": 1024**5,
             "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}
    number = _number(value, "resource package quota")
    if normalized in sizes:
        return number * sizes[normalized], "bytes"
    return number, normalized


class AliyunCollector:
    SUPPORTED_KINDS = SUPPORTED_KINDS

    def __init__(self, account: dict):
        self.account = account
        self.account_id = str(_required(account, "id"))
        self._clients: dict[str, Any] = {}
        self._performance_collector = None
        self._rds_cloudmonitor_collector = None
        self._sql_collector = None
        self._network_collector = None
        self.state_store = None

    def collect(self, kind: str, options: dict, now: datetime) -> list[Metric]:
        if kind not in SUPPORTED_KINDS:
            raise ValueError(f"Unsupported Alibaba Cloud collector: {kind}")
        return getattr(self, f"_{kind}")(options, _time(now))

    def _rpc(self, service: str, action: str, version: str, params: dict,
             region: str | None = None) -> dict:
        from aliyunsdkcore.client import AcsClient
        from aliyunsdkcore.request import CommonRequest
        from cloud_monitor.aliyun_compat import ensure_aliyun_tls_compatibility

        ensure_aliyun_tls_compatibility()
        region = region or "cn-hangzhou"
        if region not in self._clients:
            self._clients[region] = AcsClient(
                _required(self.account, "access_key_id"),
                _required(self.account, "access_key_secret"), region,
                timeout=int(self.account.get("request_timeout_seconds", self.account.get("timeout_seconds", 20))),
                auto_retry=False,
            )
        request = CommonRequest()
        request.set_accept_format("json")
        request.set_protocol_type("https")
        request.set_method("POST")
        request.set_domain(
            self.account.get("endpoints", {}).get(service)
            or (f"metrics.{region}.aliyuncs.com" if service == "cms" else ENDPOINTS[service])
        )
        request.set_version(version)
        request.set_action_name(action)
        for key, value in params.items():
            if value is not None:
                if isinstance(value, (dict, list, bool)):
                    value = json.dumps(value, separators=(",", ":"))
                request.add_query_param(key, str(value))
        response = json.loads(self._clients[region].do_action_with_exception(request))
        if not isinstance(response, dict):
            raise ValueError(f"Alibaba Cloud {action} returned a non-object response")
        if "Success" in response and not isinstance(response["Success"], bool):
            raise ValueError(f"Alibaba Cloud {action} returned an invalid Success flag")
        code = str(response.get("Code", ""))
        if response.get("Success") is False or (code and code not in {"200", "OK", "Success", "success"}):
            # Do not echo response bodies, which can contain credential-like fields.
            raise RuntimeError(f"Alibaba Cloud {service}.{action} failed: code={code or 'unknown'}")
        return response

    def _regions(self, options: dict) -> list[str]:
        regions = [options["region"]] if options.get("region") else self.account.get("regions", [])
        if not isinstance(regions, list) or not regions or not all(isinstance(x, str) and x for x in regions):
            raise ValueError("Set account.regions or job.options.region for regional resources")
        return list(dict.fromkeys(regions))

    def _pages(self, service: str, action: str, version: str, params: dict,
               rows_path: tuple[str, ...], total_path: tuple[str, ...],
               region: str | None = None, page_key: str = "PageNumber",
               size_key: str = "PageSize", page_size: int = 100) -> Iterator[dict]:
        fetched = 0
        for page in range(1, 10001):
            response = self._rpc(service, action, version,
                                 {**params, page_key: page, size_key: page_size}, region)
            rows = _path(response, rows_path)
            if not isinstance(rows, list):
                raise ValueError(f"{action} returned invalid resource list")
            if not all(isinstance(row, dict) for row in rows):
                raise ValueError(f"{action} returned an invalid resource entry")
            raw_total = _path(response, total_path)
            if isinstance(raw_total, bool):
                raise ValueError(f"{action} returned an invalid total count")
            numeric_total = _number(raw_total, "total count")
            if numeric_total < 0 or not numeric_total.is_integer():
                raise ValueError(f"{action} returned an invalid total count")
            total = int(numeric_total)
            fetched += len(rows)
            yield from rows
            if fetched >= total:
                return
            if not rows:
                raise ValueError(f"{action} pagination ended before TotalCount")
        raise ValueError(f"{action} pagination exceeded safety limit")

    def _metric(self, resource_id: str, name: str, metric: str, value: float,
                unit: str, now: datetime, dimensions: dict) -> Metric:
        return Metric(provider="aliyun", account=self.account_id,
                      resource_id=str(resource_id), resource_name=str(name or resource_id),
                      metric=metric, value=value, unit=unit, observed_at=now,
                      dimensions=dimensions)

    def _expiry(self, resource_id: str, name: str, expires: Any, now: datetime,
                dimensions: dict) -> Metric:
        end = _time(expires)
        return self._metric(resource_id, name, "expiry_days", (end - now).total_seconds() / 86400,
                            "days", now, {**dimensions, "expires_at": end.isoformat()})

    def _ecs_expiry(self, options: dict, now: datetime) -> list[Metric]:
        metrics = []
        for region in self._regions(options):
            for row in self._pages("ecs", "DescribeInstances", "2014-05-26", {"RegionId": region},
                                   ("Instances", "Instance"), ("TotalCount",), region):
                if row.get("InstanceChargeType") == "PostPaid":
                    continue
                resource = str(_required(row, "InstanceId"))
                if options.get("resource_ids") and resource not in options["resource_ids"]:
                    continue
                metrics.append(self._expiry(resource, row.get("InstanceName", resource),
                                            _required(row, "ExpiredTime"), now,
                                            {"product": "ecs", "region": region}))
        return metrics

    def _rds_expiry(self, options: dict, now: datetime) -> list[Metric]:
        metrics = []
        engines = options.get("engines", ["MySQL", "PostgreSQL"])
        for region in self._regions(options):
            for row in self._pages("rds", "DescribeDBInstances", "2014-08-15", {"RegionId": region},
                                   ("Items", "DBInstance"), ("TotalRecordCount",), region):
                if _required(row, "Engine") not in engines or row.get("PayType") == "Postpaid":
                    continue
                resource = str(_required(row, "DBInstanceId"))
                if options.get("resource_ids") and resource not in options["resource_ids"]:
                    continue
                metrics.append(self._expiry(resource, row.get("DBInstanceDescription", resource),
                                            _required(row, "ExpireTime"), now,
                                            {"product": "rds", "engine": row["Engine"], "region": region}))
        return metrics

    def _rds_performance(self, options: dict, now: datetime) -> list[Metric]:
        from .rds_performance import RdsPerformanceCollector
        if self._performance_collector is None:
            self._performance_collector = RdsPerformanceCollector(self)
        return self._performance_collector.collect(options, now)

    def _rds_cloudmonitor(self, options: dict, now: datetime) -> list[Metric]:
        from .rds_cloudmonitor import RdsCloudMonitorCollector
        if self._rds_cloudmonitor_collector is None:
            self._rds_cloudmonitor_collector = RdsCloudMonitorCollector(self)
        return self._rds_cloudmonitor_collector.collect(options, now)

    def _rds_sql(self, options: dict, now: datetime) -> list[Metric]:
        from .rds_sql import RdsSqlCollector
        if self._sql_collector is None:
            self._sql_collector = RdsSqlCollector(self.account_id, self.state_store)
        return self._sql_collector.collect(options, now)

    def _network_traffic(self, options: dict, now: datetime) -> list[Metric]:
        from .network_traffic import NetworkTrafficCollector
        if self._network_collector is None:
            self._network_collector = NetworkTrafficCollector(self, "aliyun")
        return self._network_collector.collect(options, now)

    def _domains(self, options: dict, now: datetime) -> list[Metric]:
        metrics = []
        for row in self._pages("domain", "QueryDomainList", "2018-01-29", {},
                               ("Data", "Domain"), ("TotalItemNum",), page_key="PageNum"):
            name = str(_required(row, "DomainName"))
            if options.get("domain_names") and name not in options["domain_names"]:
                continue
            expires = row.get("ExpirationDateLong")
            if expires is None:
                expires = _time(_required(row, "ExpirationDate"), CHINA)
            metrics.append(self._expiry(name, name, expires, now, {"product": "domain"}))
        return metrics

    def _certificates(self, options: dict, now: datetime) -> list[Metric]:
        metrics, seen = [], set()
        for status in options.get("statuses", [None, "EXPIRED"]):
            for row in self._pages("cas", "ListUserCertificateOrder", "2020-04-07",
                                   {"OrderType": "CERT", "Status": status},
                                   ("CertificateOrderList",), ("TotalCount",),
                                   page_key="CurrentPage", size_key="ShowSize", page_size=50):
                resource = str(_required(row, "CertificateId"))
                if resource in seen or (options.get("certificate_ids") and resource not in
                                         {str(x) for x in options["certificate_ids"]}):
                    continue
                seen.add(resource)
                detail = self._rpc("cas", "GetUserCertificateDetail", "2020-04-07",
                                   {"CertId": resource, "CertFilter": True})
                # CAS uses Common in certificate details and CommonName in order listings.
                domain = next((value.strip() for value in
                               (detail.get("Common"), row.get("CommonName"),
                                detail.get("Sans"), row.get("Sans"))
                               if isinstance(value, str) and value.strip()), "")
                name = domain or next((value.strip() for value in
                                       (detail.get("Name"), row.get("Name"))
                                       if isinstance(value, str) and value.strip()), resource)
                metrics.append(self._expiry(resource, name,
                                            _required(detail, "NotAfter"), now,
                                            {"product": "ssl", "domain": domain,
                                             "domains": certificate_domains(
                                                 detail.get("Common"), row.get("CommonName"),
                                                 detail.get("Sans"), row.get("Sans"))}))
        return metrics

    def _packages(self, options: dict, now: datetime) -> list[Metric]:
        metrics = []
        product = str(_required(options, "product_code"))
        for row in self._pages("bssopenapi", "QueryResourcePackageInstances", "2017-12-14",
                               {"ProductCode": product}, ("Data", "Instances", "Instance"),
                               ("Data", "TotalCount"), page_key="PageNum"):
            resource = str(_required(row, "InstanceId"))
            if options.get("package_ids") and resource not in options["package_ids"]:
                continue
            name = row.get("Remark") or row.get("PackageType", resource)
            dimensions = {"product": options.get("product", product),
                          "product_code": product, "commodity_code": row.get("CommodityCode", ""),
                          "status": row.get("Status", ""), "region": row.get("Region", "")}
            expiry = self._expiry(resource, name, _required(row, "ExpiryTime"), now, dimensions)
            metrics.append(expiry)
            if options.get("expiry_only", False):
                continue
            total, total_unit = _quota(_required(row, "TotalAmount"), str(_required(row, "TotalAmountUnit")))
            remaining, remaining_unit = _quota(_required(row, "RemainingAmount"), str(_required(row, "RemainingAmountUnit")))
            if total_unit != remaining_unit:
                raise ValueError(f"Package {resource} has incompatible total/remaining units")
            if total <= 0 or remaining < 0 or remaining > total * 1.000001:
                raise ValueError(f"Package {resource} has invalid total/remaining quota")
            remaining_percent = remaining / total * 100
            for metric, value in (("package_used_percent", 100 - remaining_percent),
                                  ("package_remaining_percent", remaining_percent)):
                metrics.append(self._metric(resource, name, metric, value, "%", now, dimensions))
        return metrics

    def _slb_expiry(self, options: dict, now: datetime) -> list[Metric]:
        params = {"ProductCode": options.get("product_code", "slb"), "SubscriptionType": "Subscription"}
        if options.get("region"):
            params["Region"] = options["region"]
        metrics = []
        for row in self._pages("bssopenapi", "QueryAvailableInstances", "2017-12-14", params,
                               ("Data", "InstanceList"), ("Data", "TotalCount"), page_key="PageNum"):
            if row.get("SubscriptionType") == "PayAsYouGo":
                continue
            resource = str(_required(row, "InstanceID"))
            if options.get("resource_ids") and resource not in options["resource_ids"]:
                continue
            metrics.append(self._expiry(resource, resource, _required(row, "EndTime"), now,
                                        {"product": "slb", "region": row.get("Region", ""),
                                         "renew_status": row.get("RenewStatus", "")}))
        return metrics

    def _balance(self, options: dict, now: datetime) -> list[Metric]:
        data = _path(self._rpc("bssopenapi", "QueryAccountBalance", "2017-12-14", {}), ("Data",))
        field = options.get("balance_field", "AvailableAmount")
        if field not in {"AvailableAmount", "AvailableCashAmount"}:
            raise ValueError("balance_field must be AvailableAmount or AvailableCashAmount")
        currency = str(_required(data, "Currency"))
        expected_currency = options.get("currency", "CNY")
        if currency != expected_currency:
            raise ValueError(f"Balance currency is {currency}, but thresholds are configured for {expected_currency}")
        return [self._metric(self.account_id, self.account.get("name", self.account_id),
                             "balance", _number(_required(data, field), field), currency, now,
                             {"product": "account", "currency": currency, "balance_field": field})]

    def _metrics(self, options: dict, now: datetime) -> list[Metric]:
        entries = _required(options, "metrics")
        if not isinstance(entries, list) or not entries:
            raise ValueError("options.metrics must be a non-empty list")
        return [self._cloud_metric(entry, options, now) for entry in entries]

    def _cloud_metric(self, entry: dict, options: dict, now: datetime) -> Metric:
        namespace = str(_required(entry, "namespace"))
        metric_name = str(_required(entry, "metric_name"))
        resource = str(_required(entry, "resource_id"))
        dimensions = _required(entry, "dimensions")
        if not isinstance(dimensions, dict) or not dimensions:
            raise ValueError("Each metric needs one exact, non-empty dimensions mapping")
        output = str(_required(entry, "output_metric"))
        network = output in {"network_in_bps", "network_out_bps"}
        if output not in {"storage_used_percent", "qps_ratio", "bandwidth_used_percent"} and not network:
            raise ValueError(f"Unsupported normalized CloudMonitor metric: {output}")
        limit = 1 if network else _number(_required(entry, "limit"), "limit")
        multiplier = _number(entry.get("multiplier", 1), "multiplier")
        if limit <= 0 or multiplier <= 0:
            raise ValueError("Metric limit and multiplier must be positive")
        period = int(entry.get("period", 60))
        max_age = int(entry.get("max_age_seconds", max(300, period * 3)))
        if period <= 0 or max_age <= 0 or max_age > 30 * 86400:
            raise ValueError("Metric period/max_age_seconds must be positive; max age <= 30 days")
        aggregation = entry.get("aggregation", "Average")
        if aggregation not in {"Average", "Maximum", "Minimum", "Sum", "Value"}:
            raise ValueError("Use the exact CloudMonitor statistic name in aggregation")
        region = entry.get("region") or options.get("region") or self._regions(options)[0]
        params = {"Namespace": namespace, "MetricName": metric_name,
                  "Dimensions": [dimensions], "Period": period, "Length": 1440,
                  "StartTime": int((now - timedelta(seconds=max_age + period)).timestamp() * 1000),
                  "EndTime": int(now.timestamp() * 1000)}
        points, seen_tokens = [], set()
        while True:
            response = self._rpc("cms", "DescribeMetricList", "2019-01-01", params, region)
            data = _required(response, "Datapoints")
            data = json.loads(data) if isinstance(data, str) else data
            if not isinstance(data, list):
                raise ValueError("CloudMonitor Datapoints must be a JSON list")
            points.extend(data)
            token = response.get("NextToken")
            if not token:
                break
            if token in seen_tokens or len(seen_tokens) >= 100:
                raise ValueError("CloudMonitor pagination token repeated or exceeded limit")
            seen_tokens.add(token)
            params["NextToken"] = token
        if not points:
            raise ValueError(f"No CloudMonitor data for {resource}/{metric_name}")
        dated = [(_time(_required(point, "timestamp")), point) for point in points]
        observed, latest = max(dated, key=lambda item: item[0])
        if (now - observed).total_seconds() > max_age or observed > now + timedelta(seconds=60):
            raise ValueError(f"Stale or future CloudMonitor data for {resource}/{metric_name}")
        simultaneous = [point for timestamp, point in dated if timestamp == observed]
        if len(simultaneous) > 1 and any(point != latest for point in simultaneous):
            raise ValueError(f"Ambiguous CloudMonitor series for {resource}; specify all dimensions")
        raw = _number(_required(latest, aggregation), aggregation)
        if raw < 0:
            raise ValueError(f"Negative CloudMonitor measurement for {resource}/{metric_name}")
        value = raw * multiplier / limit * (1 if network or output == "qps_ratio" else 100)
        product = entry.get("product", options.get("product", "oss"))
        metadata = {key: entry.get(key, options.get(key)) for key in ("scope", "engine")
                    if entry.get(key, options.get(key)) is not None}
        identity = {"region": region, "namespace": namespace,
                    "metric_name": metric_name, "dimensions": dimensions}
        if network:
            identity.update({"product": product, **metadata})
        timing = {key: entry[key] for key in ("sample_max_gap_seconds", "source_unit")
                  if network and key in entry}
        return self._metric(resource, entry.get("resource_name", resource), output, value,
                            "bit/s" if network else "ratio" if output == "qps_ratio" else "%", observed,
                            {**dimensions, **metadata, **timing, "product": product, "region": region,
                             "namespace": namespace, "metric_name": metric_name,
                             "identity": identity,
                             "aggregation": aggregation, "period": period,
                             "raw_value": raw, "limit": limit, "multiplier": multiplier})

    def _sms_statistics(self, options: dict, now: datetime) -> list[Metric]:
        day_timezone = timezone(timedelta(hours=float(options.get("timezone_offset_hours", 8))))
        end = now.astimezone(day_timezone).date() - timedelta(days=1)
        expected = [end - timedelta(days=n) for n in range(8)]
        expected_dates = {day.strftime("%Y%m%d") for day in expected}
        params = {"StartDate": expected[-1].strftime("%Y%m%d"), "EndDate": end.strftime("%Y%m%d"),
                  "IsGlobe": options.get("is_globe", 1), "SignName": options.get("sign_name"),
                  "TemplateType": options.get("template_type")}
        counts: dict[str, float] = {}
        for row in self._pages("dysmsapi", "QuerySendStatistics", "2017-05-25", params,
                               ("Data", "TargetList"), ("Data", "TotalSize"),
                               page_key="PageIndex", page_size=50):
            day = str(_required(row, "SendDate"))
            if day not in expected_dates:
                raise ValueError("SMS statistics contains invalid or out-of-range date")
            count = _number(_required(row, "TotalCount"), "TotalCount")
            if count < 0 or day in counts:
                raise ValueError("SMS statistics contains negative counts or duplicate dates")
            counts[day] = count
        # An empty successful result has no baseline; do not invent zero-volume samples.
        if not counts:
            return []
        missing = [day.isoformat() for day in expected if day.strftime("%Y%m%d") not in counts]
        if missing:
            raise SmsStatisticsInsufficientData("SMS daily statistics missing dates: " + ", ".join(missing))
        current = counts[end.strftime("%Y%m%d")]
        baseline = sum(counts[day.strftime("%Y%m%d")] for day in expected[1:]) / 7
        if baseline == 0 and current > 0:
            raise SmsStatisticsInsufficientData("SMS seven-day baseline is zero; percentage growth is undefined")
        growth = (current / baseline - 1) * 100 if baseline else 0
        resource = str(options.get("resource_id", f"sms:{options.get('is_globe', 1)}:{options.get('sign_name', 'all')}:{options.get('template_type', 'all')}"))
        return [self._metric(resource, options.get("resource_name", "SMS daily volume"),
                             "sms_growth_percent", growth, "%", now,
                             {"product": "sms", "statistics_date": end.isoformat(),
                              "daily_count": current, "baseline_daily_count": baseline,
                              "baseline_days": 7, "complete_day": True})]
