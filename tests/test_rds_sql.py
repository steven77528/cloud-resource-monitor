import copy
import json
import sys
import types
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from cloud_monitor.providers.rds_sql import RdsSqlCollector, validate_rds_sql_options


NOW = datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc)


def options(engine="MySQL", **changes):
    result = {
        "instances": [{
            "engine": engine, "resource_id": "db-example", "region": "cn-hangzhou",
            "resource_name": "Orders database", "host": "db.example.invalid",
            "database": "monitoring", "user": "monitor", "password": "example-secret",
        }],
    }
    result.update(changes)
    return result


def mysql_row(seconds=0, **changes):
    row = {
        "slow_queries": 100, "uptime": 10000 + seconds, "flush_uptime": 5000 + seconds,
        "deadlocks": 10, "deadlocks_generation": "2026-09-10 00:00:00/",
        "slow_query_threshold_seconds": 2,
    }
    row.update(changes)
    return row


def pg_row(datid=1, **changes):
    row = {
        "datid": datid, "deadlocks": 10, "blks_hit": 100, "blks_read": 100,
        "stats_reset": "2026-09-10 00:00:00+00", "can_read_stats": True,
        "track_counts": "on", "track_activities": "on", "disabled_activities": 0,
        "long_running_queries": 0,
    }
    row.update(changes)
    return row


def by_name(metrics):
    return {metric.metric: metric for metric in metrics}


class FakeCollector(RdsSqlCollector):
    def __init__(self, state_store=None, account_id="ali-example"):
        super().__init__(account_id, state_store=state_store)
        self.rows = []

    def _query(self, instance, config):
        return copy.deepcopy(self.rows)

    def sample(self, config, seconds, *rows):
        self.rows = rows
        return by_name(self.collect(config, NOW + timedelta(seconds=seconds)))


class JsonStateStore:
    def __init__(self):
        self.serialized = {}

    @contextmanager
    def transaction_state(self, key):
        stored = self.serialized.get(key)
        yield self, json.loads(stored) if stored is not None else None

    def save_state(self, db, key, state):
        self.serialized[key] = json.dumps(state)


class RdsSqlCollectorTests(unittest.TestCase):
    def test_mysql_cold_start_has_no_invented_rate_or_deadlock_count(self):
        collector = FakeCollector()
        self.assertEqual(collector.sample(options(), 0, mysql_row()), {})

    def test_mysql_slow_rate_uses_actual_elapsed_time(self):
        collector = FakeCollector()
        config = options()
        collector.sample(config, 0, mysql_row())
        metrics = collector.sample(config, 37, mysql_row(37, slow_queries=470))
        metric = metrics["db_slow_queries_per_second"]
        self.assertEqual(metric.value, 10)
        self.assertEqual(metric.unit, "count/s")
        self.assertEqual(metric.dimensions["sample_seconds"], 37)
        self.assertEqual(metric.dimensions["slow_query_threshold_seconds"], 2)
        self.assertEqual(metric.resource_name, "Orders database")
        self.assertEqual(metric.observed_at, NOW + timedelta(seconds=37))

    def test_mysql_counter_or_generation_reset_discards_cross_reset_deltas(self):
        cases = [
            {"slow_queries": 1}, {"deadlocks": 1}, {"uptime": 1},
            {"flush_uptime": 1}, {"deadlocks_generation": "new-generation"},
        ]
        for changed in cases:
            with self.subTest(changed=changed):
                collector = FakeCollector()
                collector.sample(options(), 0, mysql_row())
                self.assertEqual(collector.sample(options(), 60, mysql_row(60, **changed)), {})

    def test_mysql_restart_is_detected_even_when_new_uptime_and_counters_are_larger(self):
        collector = FakeCollector()
        collector.sample(options(), 0, mysql_row(uptime=10, flush_uptime=10))
        metrics = collector.sample(options(), 60, mysql_row(
            60, uptime=20, flush_uptime=20, slow_queries=1000, deadlocks=100,
        ))
        self.assertEqual(metrics, {})

    def test_mysql_flush_is_detected_even_when_new_flush_uptime_is_larger(self):
        collector = FakeCollector()
        collector.sample(options(), 0, mysql_row(flush_uptime=10))
        self.assertEqual(collector.sample(options(), 60, mysql_row(
            60, flush_uptime=20, slow_queries=1000, deadlocks=100,
        )), {})

    def test_mysql_small_uptime_sampling_difference_does_not_discard_valid_rate(self):
        collector = FakeCollector()
        collector.sample(options(), 0, mysql_row())
        metrics = collector.sample(options(), 60, mysql_row(
            60, uptime=10063, flush_uptime=5062, slow_queries=700,
        ))
        self.assertEqual(metrics["db_slow_queries_per_second"].value, 10)

    def test_long_gap_restarts_baseline_and_does_not_invent_a_continuous_window(self):
        collector = FakeCollector()
        collector.sample(options(), 0, mysql_row())
        self.assertEqual(collector.sample(options(), 121, mysql_row(121, slow_queries=1000)), {})
        metrics = collector.sample(options(), 181, mysql_row(181, slow_queries=1060))
        self.assertEqual(metrics["db_slow_queries_per_second"].value, 1)
        self.assertNotIn("db_deadlocks_last_hour", metrics)

    def test_four_new_deadlocks_trigger_before_full_hour_is_observed(self):
        collector = FakeCollector()
        collector.sample(options(), 0, mysql_row())
        metrics = collector.sample(options(), 60, mysql_row(60, deadlocks=14))
        self.assertEqual(metrics["db_deadlocks_last_hour"].value, 4)

    def test_at_most_three_deadlocks_need_full_hour_before_a_recovery_value(self):
        collector = FakeCollector()
        config = options()
        collector.sample(config, 0, mysql_row())
        for second in range(60, 3600, 60):
            metrics = collector.sample(config, second, mysql_row(second, deadlocks=13))
            self.assertNotIn("db_deadlocks_last_hour", metrics)
        metrics = collector.sample(config, 3600, mysql_row(3600, deadlocks=13))
        self.assertEqual(metrics["db_deadlocks_last_hour"].value, 3)

    def test_unknown_deadlock_event_time_at_window_boundary_cannot_signal_recovery(self):
        collector = FakeCollector()
        config = options()
        collector.sample(config, 0, mysql_row())
        for second in range(60, 3601, 60):
            metrics = collector.sample(config, second, mysql_row(second, deadlocks=14))
        self.assertEqual(metrics["db_deadlocks_last_hour"].value, 4)
        metrics = collector.sample(config, 3630, mysql_row(3630, deadlocks=14))
        self.assertNotIn("db_deadlocks_last_hour", metrics)
        metrics = collector.sample(config, 3660, mysql_row(3660, deadlocks=14))
        self.assertEqual(metrics["db_deadlocks_last_hour"].value, 0)

    def test_postgres_long_queries_are_available_without_counter_history(self):
        collector = FakeCollector()
        metrics = collector.sample(options("PostgreSQL"), 0, pg_row(long_running_queries=2))
        self.assertEqual(list(metrics), ["db_long_running_queries"])
        self.assertEqual(metrics["db_long_running_queries"].value, 2)
        self.assertEqual(metrics["db_long_running_queries"].dimensions["long_query_seconds"], 30)

    def test_postgres_cache_ratio_weights_interval_reads_across_databases(self):
        collector = FakeCollector()
        config = options("PostgreSQL")
        collector.sample(config, 0, pg_row(1), pg_row(2))
        metrics = collector.sample(config, 60,
                                   pg_row(1, blks_hit=190, blks_read=110),
                                   pg_row(2, blks_hit=100, blks_read=1000))
        self.assertEqual(metrics["db_cache_hit_percent"].value, 9)

    def test_postgres_one_database_reset_is_not_hidden_by_other_database_growth(self):
        collector = FakeCollector()
        config = options("PostgreSQL")
        collector.sample(config, 0, pg_row(1, deadlocks=1000), pg_row(2))
        metrics = collector.sample(config, 60,
                                   pg_row(1, deadlocks=1, stats_reset="new-generation"),
                                   pg_row(2, deadlocks=3000, blks_hit=100000))
        self.assertNotIn("db_cache_hit_percent", metrics)
        self.assertNotIn("db_deadlocks_last_hour", metrics)
        self.assertIn("db_long_running_queries", metrics)

    def test_postgres_zero_cache_denominator_does_not_mean_zero_percent(self):
        collector = FakeCollector()
        collector.sample(options("PostgreSQL"), 0, pg_row())
        metrics = collector.sample(options("PostgreSQL"), 60, pg_row())
        self.assertNotIn("db_cache_hit_percent", metrics)

    def test_postgres_missing_visibility_or_disabled_statistics_are_errors(self):
        for changed in ({"can_read_stats": False}, {"can_read_stats": None}, {"track_counts": "off"}):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValueError, "pg_read_all_stats|track_counts"):
                    FakeCollector().sample(options("PostgreSQL"), 0, pg_row(**changed))

    def test_postgres_disabled_activity_tracking_cannot_emit_a_false_recovery(self):
        with self.assertRaises(ValueError):
            FakeCollector().sample(options("PostgreSQL"), 0,
                                   pg_row(track_activities="off", long_running_queries=0))

    def test_postgres_session_with_disabled_activity_tracking_is_not_silently_ignored(self):
        with self.assertRaises(ValueError):
            FakeCollector().sample(options("PostgreSQL"), 0,
                                   pg_row(disabled_activities=1, long_running_queries=0))

    def test_duplicate_database_rows_are_not_double_counted(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            FakeCollector().sample(options("PostgreSQL"), 0, pg_row(), pg_row())

    def test_duplicate_or_older_sample_cannot_replace_the_counter_baseline(self):
        for second in (0, -60):
            with self.subTest(second=second):
                collector = FakeCollector()
                collector.sample(options(), 0, mysql_row())
                self.assertEqual(collector.sample(options(), second, mysql_row(slow_queries=900)), {})
                metrics = collector.sample(options(), 60, mysql_row(60, slow_queries=700))
                self.assertEqual(metrics["db_slow_queries_per_second"].value, 10)

    def test_invalid_or_disabled_counter_does_not_become_zero(self):
        for changed in ({"deadlocks": None}, {"slow_queries": "nan"},
                        {"deadlocks": -1}, {"deadlocks_generation": None}):
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    FakeCollector().sample(options(), 0, mysql_row(**changed))
        with self.assertRaisesRegex(ValueError, "no statistics"):
            FakeCollector().sample(options(), 0)

    def test_state_survives_collector_restart_without_credentials_or_sql(self):
        store = JsonStateStore()
        first = FakeCollector(store)
        row = mysql_row()
        row.update({"query": "SELECT private_business_data", "password": "database-secret"})
        first.sample(options(), 0, row)
        second = FakeCollector(store)
        metrics = second.sample(options(), 60, mysql_row(60, slow_queries=700, deadlocks=14))
        self.assertEqual(metrics["db_slow_queries_per_second"].value, 10)
        self.assertEqual(metrics["db_deadlocks_last_hour"].value, 4)
        serialized = json.dumps(store.serialized)
        for secret in ("example-secret", "database-secret", "db.example.invalid", "private_business_data"):
            self.assertNotIn(secret, serialized)

    def test_state_isolated_between_cloud_accounts(self):
        store = JsonStateStore()
        FakeCollector(store, "account-one").sample(options(), 0, mysql_row())
        metrics = FakeCollector(store, "account-two").sample(options(), 60, mysql_row(60, slow_queries=700))
        self.assertEqual(metrics, {})
        self.assertEqual(len(store.serialized), 2)

    def test_postgres_driver_binds_long_query_threshold_and_uses_fresh_readonly_transaction(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [pg_row()]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        driver = types.ModuleType("psycopg")
        driver.connect = MagicMock()
        driver.connect.return_value.__enter__.return_value = connection
        rows_module = types.ModuleType("psycopg.rows")
        rows_module.dict_row = object()
        with patch.dict(sys.modules, {"psycopg": driver, "psycopg.rows": rows_module}):
            RdsSqlCollector("ali-example").collect(options("PostgreSQL", long_query_seconds=30), NOW)
        statement, params = cursor.execute.call_args.args
        self.assertEqual(params, (30,))
        self.assertIn("pg_stat_activity", statement)
        self.assertIn("pid <> pg_backend_pid()", statement)
        self.assertIn("backend_type = 'client backend'", statement)
        self.assertTrue(driver.connect.call_args.kwargs["autocommit"])
        self.assertIn("default_transaction_read_only=on", driver.connect.call_args.kwargs["options"])
        self.assertEqual(driver.connect.call_args.kwargs["sslmode"], "verify-full")

    def test_mysql_rejects_server_without_tls_before_authentication_sends_credentials(self):
        from pymysql.connections import Connection
        from cloud_monitor.providers.rds_sql import _mysql_connection_class

        connection = object.__new__(_mysql_connection_class())
        connection.server_capabilities = 0
        connection._sock = None
        with patch.object(Connection, "_request_authentication") as authenticate:
            with self.assertRaisesRegex(ValueError, "requires server TLS before authentication"):
                connection._request_authentication()
            authenticate.assert_not_called()

    def test_mysql_tls_capable_server_continues_through_standard_verified_handshake(self):
        from pymysql.connections import Connection
        from pymysql.constants import CLIENT
        from cloud_monitor.providers.rds_sql import _mysql_connection_class

        connection = object.__new__(_mysql_connection_class())
        connection.server_capabilities = CLIENT.SSL
        connection._sock = None
        with patch.object(Connection, "_request_authentication") as authenticate:
            connection._request_authentication()
            authenticate.assert_called_once_with()

    def test_invalid_options_fail_before_database_access(self):
        for field, value in (("long_query_seconds", 0), ("long_query_seconds", True),
                             ("sample_max_gap_seconds", 0), ("timeout_seconds", 61)):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    validate_rds_sql_options(options(**{field: value}))

    def test_sql_instances_require_separate_jobs_to_preserve_each_counter_checkpoint(self):
        config = options()
        another = {**config["instances"][0], "resource_id": "db-other"}
        config["instances"].append(another)
        collector = FakeCollector()
        with patch.object(collector, "_query") as query:
            with self.assertRaisesRegex(ValueError, "exactly one instance per job"):
                collector.collect(config, NOW)
            query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
