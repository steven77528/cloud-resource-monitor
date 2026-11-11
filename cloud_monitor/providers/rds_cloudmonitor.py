"""Validated, batched CloudMonitor series supplementing native RDS performance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from typing import TYPE_CHECKING, Any

from cloud_monitor.models import Metric

if TYPE_CHECKING:
    from cloud_monitor.providers.aliyun import AliyunCollector


UTC = timezone.utc
API_VERSION = "2019-01-01"
NAMESPACE = "acs_rds_dashboard"
_DEFAULTS = {
    "period": 60,
    "metadata_cache_seconds": 86400,
    "lookback_seconds": 900,
    "max_age_seconds": 300,
    "sample_max_gap_seconds": 90,
}
_SAFE_ACTIONS = frozenset({"DescribeMetricMetaList", "DescribeMetricList"})
_SAFE_ERROR_TYPES = frozenset({
    "ClientException", "ServerException", "APIResponseError", "TimeoutError", "ConnectionError",
    "ConnectTimeout", "ReadTimeout", "ProxyError", "SSLError", "ConnectionResetError",
    "OSError", "RuntimeError", "ValueError", "TypeError", "KeyError", "AttributeError",
    "ImportError", "ModuleNotFoundError", "JSONDecodeError", "UnicodeDecodeError",
})
_SAFE_API_CODES = frozenset({
    "Forbidden", "Forbidden.RAM", "NoPermission", "AccessDenied",
    "Throttling", "Throttling.User", "Throttling.Api", "Throttling.System",
    "ServiceUnavailable", "InternalError", "InternalFailure", "RequestTimeout",
    "InvalidAccessKeyId.NotFound", "InvalidAccessKeySecret", "SignatureDoesNotMatch",
    "InvalidSecurityToken.Expired", "SDK.HttpError", "SDK.ServerUnreachable",
    "SDK.InvalidCredential", "SDK.EndpointResolvingError", "SDK.InvalidRegionId",
    "SDK.InvalidRequest", "SDK.UnknownServerError",
    "400", "401", "403", "404", "408", "429", "500", "502", "503", "504",
})
_MESSAGES = {
    "invalid_options": "RDS CloudMonitor requires an explicit nonempty instances list",
    "invalid_instance": "RDS CloudMonitor instance requires resource_id, engine and region",
    "invalid_engine": "RDS CloudMonitor engine must be MySQL or PostgreSQL",
    "invalid_name": "RDS CloudMonitor resource_name must be a nonempty string",
    "unsupported_option": "RDS CloudMonitor configuration contains an unsupported option or instance field",
    "duplicate_instance": "RDS CloudMonitor instances must not repeat a region and resource_id",
    "invalid_period": "RDS CloudMonitor time settings must be positive integers and period must be 60",
    "invalid_window": "RDS CloudMonitor lookback must exceed the monitoring period",
    "invalid_time": "RDS CloudMonitor timestamps must be valid timezone-aware dates",
    "cloud_request_failed": "RDS CloudMonitor request failed; check cloud permissions and connectivity",
    "invalid_response": "RDS CloudMonitor response is malformed",
    "invalid_metadata": "RDS CloudMonitor metric metadata is missing or malformed",
    "metadata_mismatch": "RDS CloudMonitor metric metadata does not match the supported metric contract",
    "invalid_unit": "RDS CloudMonitor metric metadata has an unexpected unit",
    "invalid_pagination": "RDS CloudMonitor pagination is incomplete, repeated or exceeds the safety limit",
    "unknown_resource": "RDS CloudMonitor returned a resource outside the requested batch",
    "invalid_value": "RDS CloudMonitor returned an invalid or out-of-range metric value",
    "future_point": "RDS CloudMonitor returned a future data point",
    "duplicate_point": "RDS CloudMonitor returned conflicting values at one timestamp",
    "no_data": "RDS CloudMonitor returned no data for a requested instance",
    "stale_series": "RDS CloudMonitor is missing recent data for a requested instance",
}


class RdsCloudMonitorError(ValueError):
    """Expose only fixed error messages, never response bodies or credentials."""

    def __init__(self, code: str, *, action: str | None = None, error_type: str | None = None,
                 api_code: str | None = None, http_status: int | None = None):
        self.code = code
        message = _MESSAGES[code]
        for name, value, allowed in (
            ("action", action, _SAFE_ACTIONS),
            ("error_type", error_type, _SAFE_ERROR_TYPES),
            ("api_code", api_code, _SAFE_API_CODES),
        ):
            if type(value) is str and value in allowed:
                message += f"; {name}={value}"
        if type(http_status) is int and 100 <= http_status <= 599:
            message += f"; http_status={http_status}"
        super().__init__(message)


@dataclass(frozen=True)
class _Spec:
    name: str
    output: str
    unit: str
    maximum: float | None = None


_SPECS = {
    "MySQL": _Spec("MySQL_SlowQueries", "db_slow_queries_per_second", "count/s"),
    "PostgreSQL": _Spec("PG_SharedBufferHitRate", "db_cache_hit_percent", "%", 100),
}
OUTPUT_METRICS = frozenset(spec.output for spec in _SPECS.values())


def validate_rds_cloudmonitor_options(options: dict) -> None:
    """Validate configuration without loading credentials or querying cloud APIs."""
    if not isinstance(options, dict):
        raise RdsCloudMonitorError("invalid_options")
    if options.keys() - (_DEFAULTS.keys() | {"instances"}):
        raise RdsCloudMonitorError("unsupported_option")
    for name, default in _DEFAULTS.items():
        value = options.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RdsCloudMonitorError("invalid_period")
    if options.get("period", 60) != 60:
        raise RdsCloudMonitorError("invalid_period")
    if options.get("lookback_seconds", 900) <= 60:
        raise RdsCloudMonitorError("invalid_window")
    instances = options.get("instances")
    if not isinstance(instances, list) or not instances:
        raise RdsCloudMonitorError("invalid_options")
    seen = set()
    for instance in instances:
        if not isinstance(instance, dict) or any(
            not isinstance(instance.get(name), str)
            or not instance[name].strip()
            or instance[name] != instance[name].strip()
            for name in ("resource_id", "engine", "region")
        ):
            raise RdsCloudMonitorError("invalid_instance")
        if instance["engine"] not in _SPECS:
            raise RdsCloudMonitorError("invalid_engine")
        if instance.keys() - {"resource_id", "resource_name", "engine", "region"}:
            raise RdsCloudMonitorError("unsupported_option")
        if "resource_name" in instance and (
            not isinstance(instance["resource_name"], str) or not instance["resource_name"].strip()
        ):
            raise RdsCloudMonitorError("invalid_name")
        identity = (instance["region"], instance["resource_id"])
        if identity in seen:
            raise RdsCloudMonitorError("duplicate_instance")
        seen.add(identity)


def _date(value: Any) -> datetime:
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            timestamp = float(value)
            if timestamp >= 100_000_000_000:
                timestamp /= 1000
            result = datetime.fromtimestamp(timestamp, UTC)
        else:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        raise RdsCloudMonitorError("invalid_time") from None


def _number(value: Any, maximum: float | None) -> float:
    if value is None or isinstance(value, bool):
        raise RdsCloudMonitorError("invalid_value")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise RdsCloudMonitorError("invalid_value") from None
    if not math.isfinite(number) or number < 0 or (maximum is not None and number > maximum):
        raise RdsCloudMonitorError("invalid_value")
    return number


def _tokens(value: Any) -> set[str]:
    if not isinstance(value, str) or not value.strip():
        raise RdsCloudMonitorError("invalid_metadata")
    tokens = [part.strip() for part in value.split(",")]
    if not all(tokens) or len(tokens) != len(set(tokens)):
        raise RdsCloudMonitorError("invalid_metadata")
    return set(tokens)


class RdsCloudMonitorCollector:
    def __init__(self, provider: AliyunCollector):
        self.provider = provider
        self._metadata_cache: dict[tuple[str, str], datetime] = {}

    def _rpc(self, action: str, params: dict, region: str) -> dict:
        try:
            response = self.provider._rpc("cms", action, API_VERSION, params, region)
        except Exception as exc:
            from aliyunsdkcore.acs_exception.exceptions import ClientException, ServerException

            api_code, http_status = None, None
            if type(exc) in (ClientException, ServerException):
                api_code = exc.get_error_code()
                if type(exc) is ServerException:
                    http_status = exc.get_http_status()
            elif type(exc) is RuntimeError:
                # The shared provider wraps unsuccessful response codes in this fixed message.
                message = str(exc)
                api_code = next((code for code in _SAFE_API_CODES
                                 if message == f"Alibaba Cloud cms.{action} failed: code={code}"), None)
            raise RdsCloudMonitorError(
                "cloud_request_failed", action=action, error_type=type(exc).__name__,
                api_code=api_code, http_status=http_status,
            ) from None
        if not isinstance(response, dict):
            raise RdsCloudMonitorError("invalid_response")
        if ("Success" in response and response["Success"] is not True) or (
            "Code" in response and str(response["Code"]) not in {"200", "OK", "Success", "success"}
        ):
            code = response.get("Code")
            if type(code) is int:
                code = str(code)
            raise RdsCloudMonitorError(
                "cloud_request_failed", action=action, error_type="APIResponseError", api_code=code,
            )
        return response

    def collect(self, options: dict, now: datetime) -> list[Metric]:
        validate_rds_cloudmonitor_options(options)
        now = _date(now)
        settings = {name: options.get(name, default) for name, default in _DEFAULTS.items()}
        start = (now - timedelta(seconds=settings["lookback_seconds"])).replace(second=0, microsecond=0)
        groups: dict[tuple[str, str], list[dict]] = {}
        for instance in options["instances"]:
            groups.setdefault((instance["region"], instance["engine"]), []).append(instance)
        result = []
        for (region, engine), instances in groups.items():
            spec = _SPECS[engine]
            self._metadata(region, spec, now, settings["metadata_cache_seconds"])
            for offset in range(0, len(instances), 50):
                batch = instances[offset:offset + 50]
                points = self._query(region, spec, batch, start, now)
                result.extend(self._parse(points, batch, spec, start, now, settings))
        return sorted(result, key=lambda metric: (metric.observed_at, metric.key()))

    def _metadata(self, region: str, spec: _Spec, now: datetime, ttl: int) -> None:
        cache_key = (region, spec.name)
        cached = self._metadata_cache.get(cache_key)
        if cached is not None and 0 <= (now - cached).total_seconds() < ttl:
            return
        fetched, matching = 0, []
        total = None
        for page in range(1, 101):
            response = self._rpc("DescribeMetricMetaList", {
                "Namespace": NAMESPACE, "MetricName": spec.name, "PageNumber": page, "PageSize": 100,
            }, region)
            try:
                rows = response["Resources"]["Resource"]
                raw_total = response["TotalCount"]
            except (KeyError, TypeError):
                raise RdsCloudMonitorError("invalid_metadata") from None
            if (not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows)
                    or isinstance(raw_total, bool)
                    or not isinstance(raw_total, (int, str)) or not str(raw_total).isdigit()):
                raise RdsCloudMonitorError("invalid_metadata")
            page_total = int(raw_total)
            if total is not None and total != page_total:
                raise RdsCloudMonitorError("invalid_pagination")
            total = page_total
            fetched += len(rows)
            if fetched > total:
                raise RdsCloudMonitorError("invalid_pagination")
            matching.extend(row for row in rows if row.get("MetricName") == spec.name)
            if fetched == total:
                break
            if not rows:
                raise RdsCloudMonitorError("invalid_pagination")
        else:
            raise RdsCloudMonitorError("invalid_pagination")
        if len(matching) != 1:
            raise RdsCloudMonitorError("invalid_metadata")
        row = matching[0]
        if row.get("Namespace") != NAMESPACE:
            raise RdsCloudMonitorError("metadata_mismatch")
        if row.get("Unit") != spec.unit:
            raise RdsCloudMonitorError("invalid_unit")
        if (_tokens(row.get("Dimensions")) != {"userId", "instanceId"}
                or "60" not in _tokens(row.get("Periods"))
                or "Average" not in _tokens(row.get("Statistics"))):
            raise RdsCloudMonitorError("metadata_mismatch")
        self._metadata_cache[cache_key] = now

    def _query(self, region: str, spec: _Spec, instances: list[dict],
               start: datetime, now: datetime) -> list[dict]:
        params = {
            "Namespace": NAMESPACE, "MetricName": spec.name,
            "Dimensions": [{"instanceId": instance["resource_id"]} for instance in instances],
            "Period": 60, "Length": 1440,
            "StartTime": int(start.timestamp() * 1000), "EndTime": int(now.timestamp() * 1000),
        }
        points, seen_tokens = [], set()
        for _ in range(100):
            response = self._rpc("DescribeMetricList", params, region)
            data = response.get("Datapoints")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (ValueError, RecursionError):
                    raise RdsCloudMonitorError("invalid_response") from None
            if not isinstance(data, list) or not all(isinstance(point, dict) for point in data):
                raise RdsCloudMonitorError("invalid_response")
            points.extend(data)
            token = response.get("NextToken")
            if token is None or token == "":
                return points
            if not isinstance(token, str) or token in seen_tokens:
                raise RdsCloudMonitorError("invalid_pagination")
            seen_tokens.add(token)
            params["NextToken"] = token
        raise RdsCloudMonitorError("invalid_pagination")

    def _parse(self, points: list[dict], instances: list[dict], spec: _Spec,
               start: datetime, now: datetime, settings: dict) -> list[Metric]:
        series: dict[str, dict[datetime, float]] = {instance["resource_id"]: {} for instance in instances}
        for point in points:
            resource_id = point.get("instanceId")
            if not isinstance(resource_id, str) or resource_id not in series:
                raise RdsCloudMonitorError("unknown_resource")
            observed = _date(point.get("timestamp"))
            if observed > now:
                raise RdsCloudMonitorError("future_point")
            value = _number(point.get("Average"), spec.maximum)
            previous = series[resource_id].get(observed)
            if previous is not None and previous != value:
                raise RdsCloudMonitorError("duplicate_point")
            series[resource_id][observed] = value
        result = []
        for instance in instances:
            values = series[instance["resource_id"]]
            if not values:
                raise RdsCloudMonitorError("no_data")
            recent = {observed: value for observed, value in values.items() if observed >= start}
            if not recent or (now - max(recent)).total_seconds() > settings["max_age_seconds"]:
                raise RdsCloudMonitorError("stale_series")
            timestamps = sorted(recent)
            max_gap = min(settings["sample_max_gap_seconds"], 90)
            if len(timestamps) > 1:
                # An observed faster cadence tightens continuity; sparse data never relaxes it.
                smallest_gap = min((right - left).total_seconds()
                                   for left, right in zip(timestamps, timestamps[1:]))
                max_gap = min(max_gap, smallest_gap * 1.5)
            identity = {
                "region": instance["region"], "engine": instance["engine"],
                "source": "rds_cloudmonitor", "namespace": NAMESPACE, "metric_name": spec.name,
            }
            for observed in timestamps:
                dimensions = {
                    "product": "rds", **identity, "identity": dict(identity),
                    "source_api": "DescribeMetricList", "source_key": spec.name,
                    "source_unit": spec.unit, "aggregation": "Average", "period": 60,
                    "sample_max_gap_seconds": max_gap,
                }
                result.append(self.provider._metric(
                    instance["resource_id"], instance.get("resource_name") or instance["resource_id"],
                    spec.output, recent[observed], spec.unit, observed, dimensions))
        return result
