"""Read-only database statistics for metrics absent from the RDS performance API."""

from __future__ import annotations

import hashlib
import json
import math
import ssl

from cloud_monitor.models import Metric


MYSQL_QUERY = """
SELECT
 (SELECT VARIABLE_VALUE FROM performance_schema.global_status
  WHERE VARIABLE_NAME = 'Slow_queries') AS slow_queries,
 (SELECT VARIABLE_VALUE FROM performance_schema.global_status
  WHERE VARIABLE_NAME = 'Uptime') AS uptime,
 (SELECT VARIABLE_VALUE FROM performance_schema.global_status
  WHERE VARIABLE_NAME = 'Uptime_since_flush_status') AS flush_uptime,
 (SELECT COUNT FROM information_schema.INNODB_METRICS
  WHERE NAME = 'lock_deadlocks' AND STATUS = 'enabled') AS deadlocks,
 (SELECT CONCAT_WS('/', TIME_ENABLED, TIME_RESET) FROM information_schema.INNODB_METRICS
  WHERE NAME = 'lock_deadlocks') AS deadlocks_generation,
 @@GLOBAL.long_query_time AS slow_query_threshold_seconds
"""

POSTGRES_QUERY = """
SELECT datid, deadlocks, blks_hit, blks_read, stats_reset::text,
 pg_has_role(current_user, 'pg_read_all_stats', 'USAGE') AS can_read_stats,
 current_setting('track_counts') AS track_counts,
 current_setting('track_activities') AS track_activities,
 (SELECT count(*) FROM pg_stat_activity
  WHERE state = 'disabled' AND backend_type = 'client backend' AND pid <> pg_backend_pid()) AS disabled_activities,
 (SELECT count(*) FROM pg_stat_activity
  WHERE state = 'active' AND backend_type = 'client backend'
    AND pid <> pg_backend_pid()
    AND query_start < statement_timestamp() - (%s * interval '1 second')) AS long_running_queries
FROM pg_stat_database WHERE datid <> 0
"""


def _number(value):
    if isinstance(value, bool):
        raise ValueError("RDS SQL statistics are missing or invalid")
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError("RDS SQL statistics are missing or invalid") from None
    if not math.isfinite(result) or result < 0:
        raise ValueError("RDS SQL statistics are missing or invalid")
    return result


def validate_rds_sql_options(options):
    instances = options.get("instances")
    if not isinstance(instances, list) or not instances:
        raise ValueError("RDS SQL requires explicit instances")
    if len(instances) != 1:
        raise ValueError("RDS SQL requires exactly one instance per job")
    for instance in instances:
        if not isinstance(instance, dict) or instance.get("engine") not in {"MySQL", "PostgreSQL"}:
            raise ValueError("RDS SQL requires MySQL or PostgreSQL instances")
        for key in ("resource_id", "region", "host", "database", "user", "password"):
            if not isinstance(instance.get(key), str) or not instance[key].strip():
                raise ValueError(f"RDS SQL requires instance {key}")
        port = instance.get("port", 3306 if instance["engine"] == "MySQL" else 5432)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("RDS SQL port is invalid")
        if instance.get("sslmode", "verify-full") not in {"verify-full", "require", "disable"}:
            raise ValueError("RDS SQL sslmode must be verify-full, require or disable")
    for field, default, minimum, maximum in (
        ("long_query_seconds", 30, 1, 86400), ("timeout_seconds", 10, 1, 60),
        ("sample_max_gap_seconds", 90, 1, 300),
    ):
        value = options.get(field, default)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not minimum <= value <= maximum:
            raise ValueError(f"RDS SQL {field} is invalid")


def _mysql_connection_class():
    import pymysql
    from pymysql.constants import CLIENT

    class TLSRequiredConnection(pymysql.connections.Connection):
        def _request_authentication(self):
            # PyMySQL otherwise falls back to plaintext when the server does not advertise TLS.
            if not self.server_capabilities & CLIENT.SSL:
                raise ValueError("RDS SQL requires server TLS before authentication")
            return super()._request_authentication()

    return TLSRequiredConnection


class RdsSqlCollector:
    def __init__(self, account_id, state_store=None):
        self.account_id = account_id
        self.state_store = state_store
        self._state = {}

    def _query(self, instance, options):
        timeout = int(options.get("timeout_seconds", 10))
        mode = instance.get("sslmode", "verify-full")
        if instance["engine"] == "MySQL":
            import pymysql
            context = None
            if mode != "disable":
                context = ssl.create_default_context(cafile=instance.get("ssl_ca") or None)
                if mode == "require":
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
            connect = _mysql_connection_class() if mode != "disable" else pymysql.connect
            connection = connect(
                host=instance["host"], port=instance.get("port", 3306),
                user=instance["user"], password=instance["password"], database=instance["database"],
                ssl=context, connect_timeout=timeout, read_timeout=timeout, write_timeout=timeout,
                autocommit=True, cursorclass=pymysql.cursors.DictCursor,
            )
            try:
                with connection.cursor() as cursor:
                    cursor.execute(MYSQL_QUERY)
                    return list(cursor.fetchall())
            finally:
                connection.close()
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(
            host=instance["host"], port=instance.get("port", 5432),
            dbname=instance["database"], user=instance["user"], password=instance["password"],
            connect_timeout=timeout, sslmode=mode, sslrootcert=instance.get("ssl_ca") or None,
            options=f"-c default_transaction_read_only=on -c statement_timeout={timeout * 1000}",
            autocommit=True, row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(POSTGRES_QUERY, (options.get("long_query_seconds", 30),))
                return list(cursor.fetchall())

    def collect(self, options, now):
        validate_rds_sql_options(options)
        result = []
        for instance in options["instances"]:
            rows = self._query(instance, options)
            snapshot = self._snapshot(instance, rows)
            # Persist counters, never credentials, SQL text, connection addresses or session contents.
            identity = [self.account_id, instance["region"], instance["resource_id"], instance["engine"]]
            key = hashlib.sha256(("rds_sql:" + json.dumps(identity)).encode()).hexdigest()
            if self.state_store is None:
                metrics, state = self._derive(instance, options, snapshot, self._state.get(key), now)
                self._state[key] = state
            else:
                with self.state_store.transaction_state(key) as (db, previous):
                    metrics, state = self._derive(instance, options, snapshot, previous, now)
                    self.state_store.save_state(db, key, state)
            result.extend(metrics)
        return result

    @staticmethod
    def _snapshot(instance, rows):
        if not rows:
            raise ValueError("RDS SQL returned no statistics")
        if instance["engine"] == "MySQL":
            row = rows[0]
            values = {key: _number(row.get(key)) for key in
                      ("slow_queries", "uptime", "flush_uptime", "deadlocks", "slow_query_threshold_seconds")}
            if row.get("deadlocks_generation") is None:
                raise ValueError("RDS SQL deadlock statistics are disabled or unavailable")
            return {**values, "generation": str(row["deadlocks_generation"])}
        databases = {}
        for row in rows:
            if row.get("can_read_stats") is not True or row.get("track_counts") != "on":
                raise ValueError("RDS SQL requires pg_read_all_stats and track_counts")
            if row.get("track_activities") != "on" or _number(row.get("disabled_activities")) != 0:
                raise ValueError("RDS SQL requires observable PostgreSQL activities")
            key = str(row["datid"])
            if key in databases:
                raise ValueError("RDS SQL returned duplicate database statistics")
            databases[key] = {field: _number(row.get(field)) for field in ("deadlocks", "blks_hit", "blks_read")}
            databases[key]["generation"] = row.get("stats_reset")
        return {"databases": databases, "long_running_queries": _number(rows[0].get("long_running_queries"))}

    def _derive(self, instance, options, snapshot, previous, now):
        timestamp = now.timestamp()
        dimensions = {"product": "rds", "engine": instance["engine"], "region": instance["region"],
                      "source": "database_statistics", "sample_max_gap_seconds": options.get("sample_max_gap_seconds", 90)}
        metrics = []

        def emit(name, value, unit, extra=None):
            metrics.append(Metric("aliyun", self.account_id, instance["resource_id"],
                                  instance.get("resource_name") or instance["resource_id"],
                                  name, value, unit, now, {**dimensions, **(extra or {})}))

        if instance["engine"] == "PostgreSQL":
            emit("db_long_running_queries", snapshot["long_running_queries"], "count",
                 {"long_query_seconds": options.get("long_query_seconds", 30)})
        state = {"snapshot": snapshot, "observed": timestamp, "deadlock_intervals": []}
        if not previous:
            return metrics, state
        elapsed = timestamp - previous["observed"]
        if elapsed <= 0:
            return [], previous
        if elapsed > options.get("sample_max_gap_seconds", 90):
            return metrics, state
        old = previous["snapshot"]
        if instance["engine"] == "MySQL":
            if (snapshot["generation"] != old["generation"] or any(snapshot[key] < old[key] for key in
                    ("uptime", "flush_uptime", "slow_queries", "deadlocks")) or any(
                        abs(snapshot[key] - old[key] - elapsed) > 5 for key in ("uptime", "flush_uptime"))):
                return metrics, state
            deadlocks = snapshot["deadlocks"] - old["deadlocks"]
            emit("db_slow_queries_per_second", (snapshot["slow_queries"] - old["slow_queries"]) / elapsed,
                 "count/s", {"sample_seconds": elapsed,
                             "slow_query_threshold_seconds": snapshot["slow_query_threshold_seconds"]})
        else:
            if snapshot["databases"].keys() != old["databases"].keys():
                return metrics, state
            deadlocks, hits, reads = 0, 0, 0
            for name, current in snapshot["databases"].items():
                before = old["databases"][name]
                if current["generation"] != before["generation"] or any(
                        current[field] < before[field] for field in ("deadlocks", "blks_hit", "blks_read")):
                    return metrics, state
                deadlocks += current["deadlocks"] - before["deadlocks"]
                hits += current["blks_hit"] - before["blks_hit"]
                reads += current["blks_read"] - before["blks_read"]
            if hits + reads:
                emit("db_cache_hit_percent", hits / (hits + reads) * 100, "%", {"sample_seconds": elapsed})
        cutoff = timestamp - 3600
        intervals = [row for row in previous.get("deadlock_intervals", []) if row[1] > cutoff]
        intervals.append([previous["observed"], timestamp, deadlocks])
        state["deadlock_intervals"] = intervals
        # The boundary interval has unknown event times. Do not declare recovery if its count could breach.
        lower = sum(row[2] for row in intervals if row[0] >= cutoff)
        upper = sum(row[2] for row in intervals)
        if lower > 3 or (intervals[0][0] <= cutoff and upper <= 3):
            emit("db_deadlocks_last_hour", lower if lower > 3 else upper, "count",
                 {"window_seconds": 3600, "count_lower_bound": lower, "count_upper_bound": upper})
        return metrics, state
