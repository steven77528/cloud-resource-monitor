"""Read-only, batched RDS performance queries with explicit series contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException, InvalidOperation
import math
import re
from typing import TYPE_CHECKING, Any

from cloud_monitor.models import Metric

if TYPE_CHECKING:
    from cloud_monitor.providers.aliyun import AliyunCollector


UTC = timezone.utc
API_VERSION = "2014-08-15"
OUTPUT_METRICS = frozenset({
    "db_cpu_percent", "db_memory_percent", "db_disk_used_percent",
    "db_connections_used_percent", "db_replication_delay_seconds",
    "db_deadlocks_per_second", "db_long_running_queries",
    "db_row_lock_waits_per_second",
})
_DEFAULTS = {
    "metadata_cache_seconds": 86400,
    "lookback_seconds": 900,
    "max_age_seconds": 300,
    "sample_max_gap_seconds": 90,
}
_SAFE_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_MESSAGES = {
    "invalid_options": "RDS performance options must contain an explicit nonempty instances list",
    "invalid_instance": "RDS performance instance requires resource_id, engine and region",
    "invalid_engine": "RDS performance engine must be MySQL or PostgreSQL",
    "invalid_role": "RDS performance replication_role must be primary or readonly for MySQL",
    "invalid_serverless": "RDS performance serverless must be a boolean and requires MySQL",
    "invalid_row_lock_waits": "RDS performance row_lock_waits must be a boolean and requires MySQL",
    "invalid_deadlock_rate": "RDS performance deadlock_rate must be a boolean and requires PostgreSQL",
    "invalid_long_query": "RDS performance long_query_seconds must be an integer 1, 3 or 5 and requires PostgreSQL",
    "invalid_node": "RDS performance node_id must be a nonempty string",
    "invalid_name": "RDS performance resource_name must be a nonempty string",
    "invalid_fields": "RDS performance field_names must map supported metric names to safe ValueFormat field names",
    "invalid_period": "RDS performance time settings must be positive integers",
    "duplicate_instance": "RDS performance instances must not repeat a region, resource_id and node_id",
    "inconsistent_instance": "RDS performance instances disagree about the engine for one resource",
    "invalid_time": "RDS performance timestamps must be valid timezone-aware dates",
    "invalid_metadata": "RDS instance metadata is missing or malformed",
    "metadata_mismatch": "RDS instance metadata does not match the requested resource and engine",
    "invalid_capacity": "RDS instance capacity or maximum connections is missing or invalid",
    "role_mismatch": "RDS instance metadata does not match the configured replication role",
    "invalid_response": "RDS performance response is malformed",
    "response_mismatch": "RDS performance response does not match the requested resource and engine",
    "missing_key": "RDS performance response is missing a requested metric key",
    "duplicate_key": "RDS performance response repeats a requested metric key",
    "invalid_unit": "RDS performance response has an unsupported or missing metric unit",
    "invalid_format": "RDS performance response has an invalid or ambiguous ValueFormat",
    "missing_field": "RDS performance ValueFormat needs an explicit matching field_names entry",
    "invalid_value": "RDS performance response has an invalid or negative metric value",
    "invalid_values": "RDS performance response has a malformed or empty values list",
    "future_point": "RDS performance response contains a future data point",
    "duplicate_point": "RDS performance response contains conflicting values at one timestamp",
    "stale_series": "RDS performance response is missing recent data for a requested metric",
    "counter_baseline": "RDS performance counter needs two valid consecutive samples to calculate a rate",
}


class RdsPerformanceError(ValueError):
    """Expose stable error codes without including cloud responses or identifiers."""

    def __init__(self, code: str, *, metric: str | None = None,
                 fields: tuple[str, ...] = ()):
        self.code = code
        message = _MESSAGES[code]
        if metric in OUTPUT_METRICS:
            message += f"; metric={metric}"
        if fields and all(_SAFE_FIELD.fullmatch(field) for field in fields):
            message += "; available_fields=" + ",".join(fields[:30])
        super().__init__(message)


@dataclass(frozen=True)
class _Spec:
    metric: str
    key: str
    fields: tuple[str, ...]
    units: frozenset[str]
    divisor: str | None = None
    single: bool = False


_PERCENT = frozenset({"%", "percent", "percentage"})
_COUNT = frozenset({"count", "counts", "number", "int", "integer", "connections", "connection"})
_COUNTER_UNITS = frozenset({"count", "counts", "number", "int", "integer"})
_ROW_LOCK_RATE = "db_row_lock_waits_per_second"
_SECONDS = frozenset({"s", "sec", "second", "seconds"})
_DEADLOCK_RATE = frozenset({
    "count/s", "counts/s", "count/sec", "counts/sec", "count/second", "counts/second",
    "1/s", "\u6b21/\u79d2",
})
_QUERY_COUNT = frozenset({"count", "counts", "number", "int", "integer", "queries", "query", "\u4e2a", "\u6761"})
_LONG_QUERY_FIELDS = {
    1: "mean_one_second_executing_sqls",
    3: "mean_three_seconds_executing_sqls",
    5: "mean_five_seconds_executing_sqls",
}
_OUTPUT_UNITS = {
    "db_replication_delay_seconds": "seconds", "db_deadlocks_per_second": "count/s",
    "db_long_running_queries": "count",
    _ROW_LOCK_RATE: "count/s",
}
_MYSQL_SPECS = (
    _Spec("db_cpu_percent", "MySQL_MemCpuUsage", ("cpuusage",), _PERCENT),
    _Spec("db_memory_percent", "MySQL_MemCpuUsage", ("memusage",), _PERCENT),
    _Spec("db_disk_used_percent", "MySQL_DetailedSpaceUsage", ("ins_size",),
          frozenset({"mb", "mbyte", "mbytes", "mib"}), "storage_mb"),
    _Spec("db_connections_used_percent", "MySQL_Sessions", ("total_session",),
          _COUNT, "connections"),
)
_POSTGRES_SPECS = (
    _Spec("db_cpu_percent", "CpuUsage", (), _PERCENT, single=True),
    _Spec("db_memory_percent", "MemoryUsage", (), _PERCENT, single=True),
    _Spec("db_disk_used_percent", "PgSQL_SpaceUsage", (),
          frozenset({"byte", "bytes", "b"}), "storage_bytes", single=True),
    _Spec("db_connections_used_percent", "PgSQL_Session", (),
          _COUNT, "connections", single=True),
)


def _positive_integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RdsPerformanceError("invalid_period")
    return value


def validate_rds_performance_options(options: dict) -> None:
    """Validate configuration without cloud credentials or network access."""
    if not isinstance(options, dict):
        raise RdsPerformanceError("invalid_options")
    for name, default in _DEFAULTS.items():
        _positive_integer(options.get(name, default))
    instances = options.get("instances")
    if not isinstance(instances, list) or not instances:
        raise RdsPerformanceError("invalid_options")
    seen, engines = set(), {}
    for instance in instances:
        if not isinstance(instance, dict) or any(
            not isinstance(instance.get(name), str) or not instance[name].strip()
            for name in ("resource_id", "engine", "region")
        ):
            raise RdsPerformanceError("invalid_instance")
        if instance["engine"] not in {"MySQL", "PostgreSQL"}:
            raise RdsPerformanceError("invalid_engine")
        if "resource_name" in instance and (
            not isinstance(instance["resource_name"], str) or not instance["resource_name"].strip()
        ):
            raise RdsPerformanceError("invalid_name")
        if "node_id" in instance and (
            not isinstance(instance["node_id"], str) or not instance["node_id"].strip()
        ):
            raise RdsPerformanceError("invalid_node")
        if "replication_role" in instance and (
            instance["engine"] != "MySQL"
            or not isinstance(instance["replication_role"], str)
            or instance["replication_role"] not in {"primary", "readonly"}
        ):
            raise RdsPerformanceError("invalid_role")
        if "serverless" in instance and (
            not isinstance(instance["serverless"], bool)
            or instance["engine"] != "MySQL"
        ):
            raise RdsPerformanceError("invalid_serverless")
        if "row_lock_waits" in instance and (
            not isinstance(instance["row_lock_waits"], bool)
            or instance["engine"] != "MySQL"
        ):
            raise RdsPerformanceError("invalid_row_lock_waits")
        if "deadlock_rate" in instance and (
            not isinstance(instance["deadlock_rate"], bool)
            or instance["engine"] != "PostgreSQL"
        ):
            raise RdsPerformanceError("invalid_deadlock_rate")
        if "long_query_seconds" in instance and (
            isinstance(instance["long_query_seconds"], bool)
            or not isinstance(instance["long_query_seconds"], int)
            or instance["long_query_seconds"] not in _LONG_QUERY_FIELDS
            or instance["engine"] != "PostgreSQL"
        ):
            raise RdsPerformanceError("invalid_long_query")
        fields = instance.get("field_names", {})
        enabled_metrics = {spec.metric for spec in _specs(instance)}
        if not isinstance(fields, dict) or any(
            name not in enabled_metrics or not isinstance(value, str)
            or not _SAFE_FIELD.fullmatch(value)
            for name, value in fields.items()
        ):
            raise RdsPerformanceError("invalid_fields")
        fixed_fields = {"db_deadlocks_per_second": "mean_deadlocks_delta",
                        _ROW_LOCK_RATE: "innodb_row_lock_waits"}
        if "long_query_seconds" in instance:
            fixed_fields["db_long_running_queries"] = _LONG_QUERY_FIELDS[instance["long_query_seconds"]]
        if any(fields[name].casefold() != expected
               for name, expected in fixed_fields.items() if name in fields):
            raise RdsPerformanceError("invalid_fields")
        resource = (instance["region"], instance["resource_id"])
        identity = (*resource, instance.get("node_id"))
        if identity in seen:
            raise RdsPerformanceError("duplicate_instance")
        if resource in engines and engines[resource] != instance["engine"]:
            raise RdsPerformanceError("inconsistent_instance")
        seen.add(identity)
        engines[resource] = instance["engine"]


def _number(value: Any, code: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or value is None:
        raise RdsPerformanceError(code)
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise RdsPerformanceError(code) from None
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        raise RdsPerformanceError(code)
    return result


def _date(value: Any) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise RdsPerformanceError("invalid_time") from None


def _counter(value: str) -> Decimal:
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            raise ValueError
        # Cloud samples of cumulative counters can contain fractional values.
        return number
    except (InvalidOperation, ValueError, OverflowError):
        raise RdsPerformanceError("invalid_value", metric=_ROW_LOCK_RATE) from None


def _specs(instance: dict) -> tuple[_Spec, ...]:
    result = _MYSQL_SPECS if instance["engine"] == "MySQL" else _POSTGRES_SPECS
    if instance.get("serverless"):
        result = tuple(
            _Spec(spec.metric, "MySQL_RCU_MemCpuUsage", spec.fields, spec.units,
                  spec.divisor, spec.single) if spec.key == "MySQL_MemCpuUsage" else spec
            for spec in result
        )
    if instance.get("replication_role"):
        key = "slavestat" if instance["replication_role"] == "readonly" else "MySQL_ReplicationDelay"
        result += (_Spec("db_replication_delay_seconds", key, (), _SECONDS, single=True),)
    if instance.get("row_lock_waits"):
        result += (_Spec(_ROW_LOCK_RATE, "MySQL_ROW_LOCK", ("innodb_row_lock_waits",), _COUNTER_UNITS),)
    if instance.get("deadlock_rate"):
        result += (_Spec("db_deadlocks_per_second", "PolarDBQPSTPS",
                         ("mean_deadlocks_delta",), _DEADLOCK_RATE),)
    if "long_query_seconds" in instance:
        result += (_Spec("db_long_running_queries", "PolarDBLongSQL",
                         (_LONG_QUERY_FIELDS[instance["long_query_seconds"]],), _QUERY_COUNT),)
    return result


class RdsPerformanceCollector:
    def __init__(self, provider: AliyunCollector):
        self.provider = provider
        self._metadata_cache: dict[tuple[str, str], tuple[datetime, dict]] = {}

    def collect(self, options: dict, now: datetime) -> list[Metric]:
        validate_rds_performance_options(options)
        now = _date(now)
        settings = {name: options.get(name, default) for name, default in _DEFAULTS.items()}
        start = (now - timedelta(seconds=settings["lookback_seconds"])).replace(second=0, microsecond=0)
        metrics = []
        for instance in options["instances"]:
            metadata = self._metadata(instance, now, settings["metadata_cache_seconds"])
            specs = _specs(instance)
            keys = list(dict.fromkeys(spec.key for spec in specs))
            params = {
                "DBInstanceId": instance["resource_id"], "Key": ",".join(keys),
                "StartTime": start.strftime("%Y-%m-%dT%H:%MZ"),
                "EndTime": now.strftime("%Y-%m-%dT%H:%MZ"),
            }
            if instance.get("node_id"):
                params["NodeId"] = instance["node_id"]
            response = self.provider._rpc(
                "rds", "DescribeDBInstancePerformance", API_VERSION, params, instance["region"])
            metrics.extend(self._parse(response, instance, metadata, specs, start, now, settings))
        return sorted(metrics, key=lambda metric: (metric.observed_at, metric.key()))

    def _metadata(self, instance: dict, now: datetime, ttl: int) -> dict:
        cache_key = (instance["region"], instance["resource_id"])
        cached = self._metadata_cache.get(cache_key)
        if cached and 0 <= (now - cached[0]).total_seconds() < ttl:
            metadata = cached[1]
        else:
            response = self.provider._rpc(
                "rds", "DescribeDBInstanceAttribute", API_VERSION,
                {"DBInstanceId": instance["resource_id"]}, instance["region"])
            try:
                rows = response["Items"]["DBInstanceAttribute"]
            except (KeyError, TypeError):
                raise RdsPerformanceError("invalid_metadata") from None
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
                raise RdsPerformanceError("invalid_metadata")
            row = rows[0]
            if row.get("DBInstanceDescription") is not None and not isinstance(row["DBInstanceDescription"], str):
                raise RdsPerformanceError("invalid_metadata")
            storage_gb = _number(row.get("DBInstanceStorage"), "invalid_capacity", positive=True)
            connections = _number(row.get("MaxConnections"), "invalid_capacity", positive=True)
            metadata = {
                "resource_id": row.get("DBInstanceId"), "engine": row.get("Engine"),
                "name": row.get("DBInstanceDescription"), "role": row.get("DBInstanceType"),
                "storage_mb": storage_gb * 1024, "storage_bytes": storage_gb * 1024**3,
                "connections": connections,
            }
            if not all(math.isfinite(metadata[name]) for name in ("storage_mb", "storage_bytes")):
                raise RdsPerformanceError("invalid_capacity")
            self._check_metadata(metadata, instance)
            self._metadata_cache[cache_key] = (now, metadata)
        self._check_metadata(metadata, instance)
        return metadata

    @staticmethod
    def _check_metadata(metadata: dict, instance: dict) -> None:
        if metadata["resource_id"] != instance["resource_id"] or metadata["engine"] != instance["engine"]:
            raise RdsPerformanceError("metadata_mismatch")
        role = instance.get("replication_role")
        if role and (not isinstance(metadata["role"], str) or metadata["role"].lower() != role):
            raise RdsPerformanceError("role_mismatch")

    def _parse(self, response: Any, instance: dict, metadata: dict, specs: tuple[_Spec, ...],
               start: datetime, now: datetime, settings: dict) -> list[Metric]:
        if not isinstance(response, dict):
            raise RdsPerformanceError("invalid_response")
        for name, expected in (("DBInstanceId", instance["resource_id"]), ("Engine", instance["engine"])):
            if response.get(name) != expected:
                raise RdsPerformanceError("response_mismatch")
        try:
            entries = response["PerformanceKeys"]["PerformanceKey"]
        except (KeyError, TypeError):
            raise RdsPerformanceError("invalid_response") from None
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise RdsPerformanceError("invalid_response")
        requested = {spec.key for spec in specs}
        by_key = {}
        for entry in entries:
            key = entry.get("Key")
            if not isinstance(key, str):
                raise RdsPerformanceError("invalid_response")
            if key in requested:
                if key in by_key:
                    raise RdsPerformanceError("duplicate_key")
                by_key[key] = entry
        if by_key.keys() != requested:
            raise RdsPerformanceError("missing_key")
        result = []
        for key, entry in by_key.items():
            key_specs = [spec for spec in specs if spec.key == key]
            result.extend(self._parse_key(entry, key_specs, instance, metadata, start, now, settings))
        return result

    def _parse_key(self, entry: dict, specs: list[_Spec], instance: dict, metadata: dict,
                   start: datetime, now: datetime, settings: dict) -> list[Metric]:
        unit, value_format = entry.get("Unit"), entry.get("ValueFormat")
        if not isinstance(unit, str) or any(unit.strip().casefold() not in spec.units for spec in specs):
            raise RdsPerformanceError("invalid_unit")
        if not isinstance(value_format, str):
            raise RdsPerformanceError("invalid_format")
        fields = tuple(value_format.split("&"))
        folded = [field.casefold() for field in fields]
        if not fields or any(not _SAFE_FIELD.fullmatch(field) for field in fields) or len(set(folded)) != len(fields):
            raise RdsPerformanceError("invalid_format")
        indices = []
        configured = instance.get("field_names", {})
        for spec in specs:
            if spec.metric in configured:
                matches = [i for i, field in enumerate(folded) if field == configured[spec.metric].casefold()]
            elif spec.single:
                matches = [0] if len(fields) == 1 else []
            else:
                matches = [i for i, field in enumerate(folded) if field in spec.fields]
            if len(matches) != 1:
                raise RdsPerformanceError("missing_field", metric=spec.metric, fields=fields)
            indices.append(matches[0])
        if len(set(indices)) != len(indices):
            raise RdsPerformanceError("invalid_format")
        counter_index = next((index for spec, index in zip(specs, indices)
                              if spec.metric == _ROW_LOCK_RATE), None)
        try:
            rows = entry["Values"]["PerformanceValue"]
        except (KeyError, TypeError):
            raise RdsPerformanceError("invalid_values") from None
        if not isinstance(rows, list) or not rows:
            raise RdsPerformanceError("invalid_values")
        points: dict[datetime, tuple[float | Decimal, ...]] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("Value"), str):
                raise RdsPerformanceError("invalid_values")
            observed = _date(row.get("Date"))
            if observed > now:
                raise RdsPerformanceError("future_point")
            raw_values = row["Value"].split("&")
            if len(raw_values) != len(fields):
                raise RdsPerformanceError("invalid_format")
            values = tuple(_counter(value) if index == counter_index else _number(value, "invalid_value")
                           for index, value in enumerate(raw_values))
            if observed in points and values != points[observed]:
                raise RdsPerformanceError("duplicate_point")
            points[observed] = values
        recent = {observed: values for observed, values in points.items() if observed >= start}
        if not recent or (now - max(recent)).total_seconds() > settings["max_age_seconds"]:
            raise RdsPerformanceError("stale_series")
        timestamps = sorted(recent)
        max_gap = settings["sample_max_gap_seconds"]
        if len(timestamps) > 1:
            # Tighten gap tolerance to observed cadence; sparse data cannot relax it.
            smallest_gap = min((right - left).total_seconds()
                               for left, right in zip(timestamps, timestamps[1:]))
            max_gap = min(max_gap, smallest_gap * 1.5)
        if counter_index is not None:
            max_gap = min(max_gap, 90)
        identity = {"region": instance["region"], "engine": instance["engine"]}
        if instance.get("node_id"):
            identity["node_id"] = instance["node_id"]
        name = instance.get("resource_name") or metadata["name"] or instance["resource_id"]
        metrics = []
        previous_counter = None
        for observed, values in sorted(recent.items()):
            for spec, index in zip(specs, indices):
                value = values[index]
                counter_details = {}
                if spec.metric == _ROW_LOCK_RATE:
                    previous, previous_counter = previous_counter, (observed, value)
                    if previous is None:
                        continue
                    elapsed = (observed - previous[0]).total_seconds()
                    if elapsed > max_gap or value < previous[1]:
                        # A gap or reset starts a new baseline, never a synthetic zero rate.
                        continue
                    try:
                        source_counter = int(value) if value == value.to_integral_value() else float(value)
                        value = float((value - previous[1]) / Decimal(str(elapsed)))
                    except (DecimalException, ValueError, OverflowError):
                        raise RdsPerformanceError("invalid_value", metric=_ROW_LOCK_RATE) from None
                    if isinstance(source_counter, float) and not math.isfinite(source_counter):
                        raise RdsPerformanceError("invalid_value", metric=_ROW_LOCK_RATE)
                    counter_details = {"sample_seconds": elapsed, "source_counter": source_counter}
                if spec.divisor:
                    value = value / metadata[spec.divisor] * 100
                if not math.isfinite(value):
                    raise RdsPerformanceError("invalid_value")
                dimensions = {
                    "product": "rds", **identity, "identity": dict(identity),
                    "source_api": "DescribeDBInstancePerformance", "source_key": spec.key,
                    "source_field": fields[index], "source_unit": unit,
                    "sample_max_gap_seconds": max_gap,
                    **counter_details,
                }
                if spec.metric == "db_long_running_queries":
                    seconds = instance["long_query_seconds"]
                    dimensions["long_query_seconds"] = seconds
                    dimensions["identity"]["long_query_seconds"] = seconds
                metrics.append(self.provider._metric(
                    instance["resource_id"], name, spec.metric, value,
                    _OUTPUT_UNITS.get(spec.metric, "%"),
                    observed, dimensions))
        if counter_index is not None:
            if not metrics:
                raise RdsPerformanceError("counter_baseline", metric=_ROW_LOCK_RATE)
            if (now - metrics[-1].observed_at).total_seconds() > settings["max_age_seconds"]:
                raise RdsPerformanceError("stale_series", metric=_ROW_LOCK_RATE)
        return metrics
