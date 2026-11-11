import copy
from datetime import datetime, timedelta, timezone
import json
import unittest

from cloud_monitor.providers.aliyun import AliyunCollector
from cloud_monitor.providers.rds_performance import (
    RdsPerformanceCollector,
    RdsPerformanceError,
    validate_rds_performance_options,
)


NOW = datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc)
INSTANCE = {"resource_id": "rm-test", "engine": "MySQL", "region": "cn-hangzhou"}


class FakeProvider(AliyunCollector):
    def __init__(self, *responses):
        super().__init__({"id": "account-test"})
        self.responses = list(responses)
        self.calls = []

    def _rpc(self, service, action, version, params, region=None):
        self.calls.append((service, action, version, params.copy(), region))
        if not self.responses:
            raise AssertionError("Unexpected cloud request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def options(**changes):
    result = {"instances": [dict(INSTANCE)]}
    result.update(changes)
    return result


def metadata(**changes):
    row = {
        "DBInstanceId": "rm-test", "Engine": "MySQL", "DBInstanceDescription": "Test database",
        "DBInstanceStorage": 10, "MaxConnections": 100, "DBInstanceType": "Primary",
    }
    row.update(changes)
    return {"Items": {"DBInstanceAttribute": [row]}}


def entry(key, fields, values, unit, ages=(120, 60), now=NOW):
    return {
        "Key": key, "Unit": unit, "ValueFormat": fields,
        "Values": {"PerformanceValue": [
            {"Date": (now - timedelta(seconds=age)).isoformat(), "Value": values}
            for age in ages
        ]},
    }


def response(engine="MySQL", ages=(120, 60), now=NOW, resource="rm-test"):
    if engine == "MySQL":
        entries = [
            entry("MySQL_Sessions", "total_session&active_session", "40&10", "Count", ages, now),
            entry("MySQL_MemCpuUsage", "memusage&cpuusage", "70.5&83.75", "%", ages, now),
            entry("MySQL_DetailedSpaceUsage", "data_size&log_size&ins_size&other_size&tmp_size",
                  "4096&512&5120&256&256", "MB", ages, now),
        ]
    else:
        entries = [
            entry("CpuUsage", "cpu", "88.25", "%", ages, now),
            entry("MemoryUsage", "memory", "66.5", "%", ages, now),
            entry("PgSQL_SpaceUsage", "space", str(5 * 1024**3), "Byte", ages, now),
            entry("PgSQL_Session", "sessions", "30", "Count", ages, now),
        ]
    return {"DBInstanceId": resource, "Engine": engine,
            "PerformanceKeys": {"PerformanceKey": entries}}


def keyed(data, key):
    return next(row for row in data["PerformanceKeys"]["PerformanceKey"] if row["Key"] == key)


def pg_optional_response(*, deadlocks=True, long_queries=True, deadlock_unit="Count/s", long_query_unit="Count"):
    data = response("PostgreSQL")
    entries = data["PerformanceKeys"]["PerformanceKey"]
    if deadlocks:
        entries.append(entry(
            "PolarDBQPSTPS", "mean_commits_delta&mean_deadlocks_delta&mean_rollbacks_delta&mean_tps",
            "20&0.0166667&2&22", deadlock_unit))
    if long_queries:
        entries.append(entry(
            "PolarDBLongSQL", "mean_three_seconds_executing_sqls&mean_one_second_executing_sqls&mean_five_seconds_executing_sqls",
            "3&8&1", long_query_unit))
    return data


def row_lock_response(counts=(455, 455), ages=(120, 60), unit="int"):
    data = response()
    wait_entry = entry("MySQL_ROW_LOCK", "innodb_row_lock_waits&innodb_row_lock_time&innodb_row_lock_time_avg",
                       "455&9000&1.5", unit, ages)
    for row, count in zip(wait_entry["Values"]["PerformanceValue"], counts):
        row["Value"] = f"{count}&9000&1.5"
    data["PerformanceKeys"]["PerformanceKey"].append(wait_entry)
    return data


class RdsPerformanceTests(unittest.TestCase):
    def test_connection_integer_units_are_counts_but_bytes_are_rejected(self):
        for unit in ("int", "Integer"):
            data = response()
            keyed(data, "MySQL_Sessions")["Unit"] = unit
            metrics = RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(options(), NOW)
            self.assertEqual(next(m.value for m in metrics if m.metric == "db_connections_used_percent"), 40)
        data = response()
        keyed(data, "MySQL_Sessions")["Unit"] = "Bytes"
        with self.assertRaises(RdsPerformanceError):
            RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(options(), NOW)

    def test_mysql_batches_keys_and_reads_format_not_field_order(self):
        provider = FakeProvider(metadata(), response())
        metrics = RdsPerformanceCollector(provider).collect(options(), NOW)
        self.assertEqual(len(provider.calls), 2)
        query = provider.calls[1]
        self.assertEqual(query[:3], ("rds", "DescribeDBInstancePerformance", "2014-08-15"))
        self.assertEqual(query[4], "cn-hangzhou")
        self.assertEqual(set(query[3]["Key"].split(",")), {
            "MySQL_MemCpuUsage", "MySQL_DetailedSpaceUsage", "MySQL_Sessions"})
        self.assertEqual(query[3]["StartTime"], "2026-09-10T03:45Z")
        self.assertEqual(query[3]["EndTime"], "2026-09-10T04:00Z")
        self.assertEqual(len(metrics), 8)
        actual = {metric.metric: metric.value for metric in metrics}
        self.assertEqual(actual, {
            "db_cpu_percent": 83.75, "db_memory_percent": 70.5,
            "db_disk_used_percent": 50.0, "db_connections_used_percent": 40.0})
        self.assertEqual([m.observed_at for m in metrics], sorted(m.observed_at for m in metrics))
        self.assertEqual({m.observed_at for m in metrics}, {NOW - timedelta(seconds=120), NOW - timedelta(seconds=60)})
        self.assertTrue(all(m.resource_name == "Test database" for m in metrics))
        self.assertTrue(all(m.unit == "%" for m in metrics))

    def test_pg_queries_four_scalar_keys_and_converts_bytes(self):
        instance = {**INSTANCE, "engine": "PostgreSQL"}
        provider = FakeProvider(metadata(Engine="PostgreSQL"), response("PostgreSQL"))
        metrics = RdsPerformanceCollector(provider).collect(options(instances=[instance]), NOW)
        self.assertEqual({m.metric: m.value for m in metrics}, {
            "db_cpu_percent": 88.25, "db_memory_percent": 66.5,
            "db_disk_used_percent": 50.0, "db_connections_used_percent": 30.0})
        self.assertEqual(set(provider.calls[1][3]["Key"].split(",")), {
            "CpuUsage", "MemoryUsage", "PgSQL_SpaceUsage", "PgSQL_Session"})

    def test_format_order_and_metadata_changes_do_not_change_identity(self):
        second = response()
        keyed(second, "MySQL_MemCpuUsage")["ValueFormat"] = "cpuusage&memusage"
        for row in keyed(second, "MySQL_MemCpuUsage")["Values"]["PerformanceValue"]:
            row["Value"] = "83.75&70.5"
        provider = FakeProvider(metadata(), response(), metadata(DBInstanceDescription="Renamed", MaxConnections=200), second)
        collector = RdsPerformanceCollector(provider)
        first = collector.collect(options(metadata_cache_seconds=1), NOW)
        second_metrics = collector.collect(options(metadata_cache_seconds=1), NOW + timedelta(seconds=1))
        self.assertEqual({m.metric: m.key() for m in first}, {m.metric: m.key() for m in second_metrics})
        self.assertEqual(first[0].dimensions["identity"], {"region": "cn-hangzhou", "engine": "MySQL"})
        self.assertTrue(all(m.dimensions["sample_max_gap_seconds"] == 90 for m in first))

    def test_node_id_participates_in_identity_and_request(self):
        provider = FakeProvider(metadata(), response())
        instance = {**INSTANCE, "node_id": "node-a", "resource_name": "Configured name"}
        metrics = RdsPerformanceCollector(provider).collect(options(instances=[instance]), NOW)
        self.assertEqual(provider.calls[1][3]["NodeId"], "node-a")
        self.assertEqual(metrics[0].dimensions["identity"]["node_id"], "node-a")
        self.assertEqual(metrics[0].resource_name, "Configured name")

    def test_missing_resource_description_uses_resource_id(self):
        provider = FakeProvider(metadata(DBInstanceDescription=None), response())
        metrics = RdsPerformanceCollector(provider).collect(options(), NOW)
        self.assertTrue(all(m.resource_name == INSTANCE["resource_id"] for m in metrics))

    def test_metadata_cache_refreshes_at_ttl_boundary(self):
        later = NOW + timedelta(seconds=86400)
        provider = FakeProvider(metadata(), response(), response(now=later - timedelta(seconds=1)),
                                metadata(MaxConnections=200), response(now=later))
        collector = RdsPerformanceCollector(provider)
        collector.collect(options(), NOW)
        collector.collect(options(), later - timedelta(seconds=1))
        metrics = collector.collect(options(), later)
        self.assertEqual([c[1] for c in provider.calls], [
            "DescribeDBInstanceAttribute", "DescribeDBInstancePerformance", "DescribeDBInstancePerformance",
            "DescribeDBInstanceAttribute", "DescribeDBInstancePerformance"])
        self.assertEqual({m.value for m in metrics if m.metric == "db_connections_used_percent"}, {20.0})

    def test_failed_metadata_refresh_does_not_use_expired_capacity(self):
        later = NOW + timedelta(seconds=86400)
        provider = FakeProvider(metadata(), response(), RuntimeError("request failed"))
        collector = RdsPerformanceCollector(provider)
        collector.collect(options(), NOW)
        with self.assertRaisesRegex(RuntimeError, "request failed"):
            collector.collect(options(), later)
        self.assertEqual(len(provider.calls), 3)

    def test_metadata_cache_is_separate_by_region_and_resource(self):
        instances = [INSTANCE, {**INSTANCE, "region": "cn-shanghai"}, {**INSTANCE, "resource_id": "rm-other"}]
        provider = FakeProvider(metadata(), response(), metadata(), response(),
                                metadata(DBInstanceId="rm-other"), response(resource="rm-other"))
        metrics = RdsPerformanceCollector(provider).collect(options(instances=instances), NOW)
        self.assertEqual(len(provider.calls), 6)
        self.assertEqual(len({m.key() for m in metrics}), 12)

    def test_node_queries_share_instance_metadata_cache(self):
        instances = [{**INSTANCE, "node_id": "node-a"}, {**INSTANCE, "node_id": "node-b"}]
        provider = FakeProvider(metadata(), response(), response())
        metrics = RdsPerformanceCollector(provider).collect(options(instances=instances), NOW)
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(len({m.key() for m in metrics}), 8)

    def test_serverless_uses_the_rcu_key(self):
        data = response()
        keyed(data, "MySQL_MemCpuUsage")["Key"] = "MySQL_RCU_MemCpuUsage"
        provider = FakeProvider(metadata(), data)
        RdsPerformanceCollector(provider).collect(options(instances=[{**INSTANCE, "serverless": True}]), NOW)
        self.assertIn("MySQL_RCU_MemCpuUsage", provider.calls[1][3]["Key"].split(","))
        self.assertNotIn("MySQL_MemCpuUsage", provider.calls[1][3]["Key"].split(","))

    def test_primary_and_readonly_replication_use_different_keys(self):
        for role, metadata_role, key in (("primary", "Primary", "MySQL_ReplicationDelay"),
                                         ("readonly", "Readonly", "slavestat")):
            with self.subTest(role=role):
                data = response()
                data["PerformanceKeys"]["PerformanceKey"].append(entry(key, "delay", "2.5", "Seconds"))
                provider = FakeProvider(metadata(DBInstanceType=metadata_role), data)
                metrics = RdsPerformanceCollector(provider).collect(
                    options(instances=[{**INSTANCE, "replication_role": role}]), NOW)
                replication = [m for m in metrics if m.metric == "db_replication_delay_seconds"]
                self.assertEqual(len(replication), 2)
                self.assertEqual(replication[0].value, 2.5)
                self.assertEqual(replication[0].unit, "seconds")
                self.assertIn(key, provider.calls[1][3]["Key"].split(","))

    def test_replication_role_mismatch_fails_before_performance_query(self):
        provider = FakeProvider(metadata(DBInstanceType="Primary"))
        with self.assertRaisesRegex(RdsPerformanceError, "configured replication role"):
            RdsPerformanceCollector(provider).collect(
                options(instances=[{**INSTANCE, "replication_role": "readonly"}]), NOW)
        self.assertEqual(len(provider.calls), 1)

    def test_actual_replication_lag_field_supports_hyphen(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                data = response()
                data["PerformanceKeys"]["PerformanceKey"].append(
                    entry("MySQL_ReplicationDelay", "replication-lag", "2.5", "s"))
                instance = {**INSTANCE, "replication_role": "primary"}
                if explicit:
                    instance["field_names"] = {"db_replication_delay_seconds": "replication-lag"}
                provider = FakeProvider(metadata(), data)
                metrics = RdsPerformanceCollector(provider).collect(options(instances=[instance]), NOW)
                replication = [m for m in metrics if m.metric == "db_replication_delay_seconds"]
                self.assertEqual(len(replication), 2)
                self.assertEqual({m.value for m in replication}, {2.5})
                self.assertEqual({m.dimensions["source_field"] for m in replication}, {"replication-lag"})
                self.assertEqual(len(provider.calls), 2)

    def test_explicit_fields_read_exact_returned_columns(self):
        data = response()
        keyed(data, "MySQL_MemCpuUsage")["ValueFormat"] = "memory_fraction&cpu_fraction"
        provider = FakeProvider(metadata(), data)
        instance = {**INSTANCE, "field_names": {
            "db_cpu_percent": "cpu_fraction", "db_memory_percent": "memory_fraction"}}
        metrics = RdsPerformanceCollector(provider).collect(options(instances=[instance]), NOW)
        self.assertEqual({m.value for m in metrics if m.metric == "db_cpu_percent"}, {83.75})

    def test_unknown_composite_fields_return_safe_diagnostic(self):
        data = response()
        keyed(data, "MySQL_MemCpuUsage")["ValueFormat"] = "memory_fraction&cpu_fraction"
        provider = FakeProvider(metadata(), data)
        with self.assertRaises(RdsPerformanceError) as caught:
            RdsPerformanceCollector(provider).collect(options(), NOW)
        self.assertEqual(caught.exception.code, "missing_field")
        self.assertIn("metric=db_cpu_percent", str(caught.exception))
        self.assertIn("available_fields=memory_fraction,cpu_fraction", str(caught.exception))

    def test_scalar_key_rejects_multiple_fields_without_explicit_choice(self):
        data = response("PostgreSQL")
        keyed(data, "CpuUsage")["ValueFormat"] = "cpu&another"
        provider = FakeProvider(metadata(Engine="PostgreSQL"), data)
        with self.assertRaises(RdsPerformanceError) as caught:
            RdsPerformanceCollector(provider).collect(options(instances=[{**INSTANCE, "engine": "PostgreSQL"}]), NOW)
        self.assertEqual(caught.exception.code, "missing_field")

    def test_duplicate_same_values_are_deduplicated(self):
        data = response()
        rows = keyed(data, "MySQL_MemCpuUsage")["Values"]["PerformanceValue"]
        rows.append(copy.deepcopy(rows[0]))
        metrics = RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(options(), NOW)
        self.assertEqual(len(metrics), 8)

    def test_conflicting_duplicate_timestamps_fail(self):
        data = response()
        rows = keyed(data, "MySQL_MemCpuUsage")["Values"]["PerformanceValue"]
        rows.append({**rows[0], "Value": "10&90"})
        self.assert_code(data, "duplicate_point")

    def test_missing_key_is_failure_even_if_cpu_exists(self):
        data = response()
        data["PerformanceKeys"]["PerformanceKey"] = [keyed(data, "MySQL_MemCpuUsage")]
        self.assert_code(data, "missing_key")

    def test_empty_key_is_failure_even_if_other_keys_have_data(self):
        data = response()
        keyed(data, "MySQL_Sessions")["Values"]["PerformanceValue"] = []
        self.assert_code(data, "invalid_values")

    def test_duplicate_key_is_failure(self):
        data = response()
        data["PerformanceKeys"]["PerformanceKey"].append(copy.deepcopy(keyed(data, "MySQL_MemCpuUsage")))
        self.assert_code(data, "duplicate_key")

    def test_future_point_is_failure_even_when_recent_valid_points_exist(self):
        data = response()
        keyed(data, "MySQL_Sessions")["Values"]["PerformanceValue"].append(
            {"Date": (NOW + timedelta(seconds=1)).isoformat(), "Value": "40&10"})
        self.assert_code(data, "future_point")

    def test_one_stale_series_is_failure_even_when_other_series_are_current(self):
        data = response()
        keyed(data, "MySQL_Sessions")["Values"] = entry("x", "x", "40&10", "Count", ages=(301,))["Values"]
        self.assert_code(data, "stale_series")

    def test_max_age_boundary_is_allowed(self):
        metrics = RdsPerformanceCollector(FakeProvider(metadata(), response(ages=(300,)))).collect(options(), NOW)
        self.assertEqual(len(metrics), 4)

    def test_points_older_than_query_window_are_not_returned(self):
        data = response(ages=(1000, 120, 60))
        metrics = RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(options(), NOW)
        self.assertEqual(len(metrics), 8)

    def test_old_window_without_current_points_is_failure(self):
        self.assert_code(response(ages=(1000, 1200)), "stale_series")

    def test_gaps_preserve_timestamps_for_window_evaluator(self):
        data = response(ages=(600, 60))
        metrics = RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(
            options(sample_max_gap_seconds=90), NOW)
        self.assertEqual(len(metrics), 8)
        self.assertTrue(all(m.dimensions["sample_max_gap_seconds"] == 90 for m in metrics))

    def test_missing_one_minute_sample_exceeds_default_gap_tolerance(self):
        data = response(ages=(300, 240, 120, 60))
        metrics = RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(options(), NOW)
        cpu = [m for m in metrics if m.metric == "db_cpu_percent"]
        gaps = [(right.observed_at - left.observed_at).total_seconds()
                for left, right in zip(cpu, cpu[1:])]
        self.assertEqual(gaps, [60, 120, 60])
        self.assertTrue(all(m.dimensions["sample_max_gap_seconds"] == 90 for m in metrics))
        self.assertGreater(gaps[1], cpu[2].dimensions["sample_max_gap_seconds"])

    def test_missing_five_second_sample_exceeds_inferred_gap_tolerance(self):
        data = response(ages=(25, 20, 10, 5))
        metrics = RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(options(), NOW)
        cpu = [m for m in metrics if m.metric == "db_cpu_percent"]
        gaps = [(right.observed_at - left.observed_at).total_seconds()
                for left, right in zip(cpu, cpu[1:])]
        self.assertEqual(gaps, [5, 10, 5])
        self.assertTrue(all(m.dimensions["sample_max_gap_seconds"] == 7.5 for m in metrics))
        self.assertGreater(gaps[1], cpu[2].dimensions["sample_max_gap_seconds"])

    def test_gap_tolerance_is_derived_separately_for_each_series(self):
        data = response(ages=(120, 60))
        keyed(data, "MySQL_MemCpuUsage")["Values"] = entry(
            "ignored", "ignored", "70.5&83.75", "%", ages=(25, 20, 10, 5))["Values"]
        metrics = RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(options(), NOW)
        self.assertEqual({m.metric: m.dimensions["sample_max_gap_seconds"] for m in metrics}, {
            "db_cpu_percent": 7.5, "db_memory_percent": 7.5,
            "db_disk_used_percent": 90, "db_connections_used_percent": 90})

    def test_inferred_cadence_never_relaxes_explicit_gap_limit(self):
        data = response(ages=(25, 20, 10, 5))
        metrics = RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(
            options(sample_max_gap_seconds=6), NOW)
        self.assertTrue(all(m.dimensions["sample_max_gap_seconds"] == 6 for m in metrics))

    def test_five_minute_sparse_points_do_not_imply_continuity(self):
        data = response(ages=(900, 600, 300))
        metrics = RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(options(), NOW)
        self.assertEqual(len(metrics), 12)
        self.assertTrue(all(m.dimensions["sample_max_gap_seconds"] == 90 for m in metrics))

    def test_single_point_keeps_configured_gap_without_inferring_cadence(self):
        data = response(ages=(60,))
        metrics = RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(options(), NOW)
        self.assertEqual(len(metrics), 4)
        self.assertTrue(all(m.dimensions["sample_max_gap_seconds"] == 90 for m in metrics))

    def test_bad_unit_fails_instead_of_silently_changing_scale(self):
        for key, unit in (("MySQL_MemCpuUsage", "ratio"), ("MySQL_DetailedSpaceUsage", "Byte"),
                          ("MySQL_Sessions", "%"), ("MySQL_MemCpuUsage", None)):
            with self.subTest(key=key, unit=unit):
                data = response()
                keyed(data, key)["Unit"] = unit
                self.assert_code(data, "invalid_unit")

    def test_missing_invalid_and_nonfinite_values_are_never_zero(self):
        for value in (None, "", "NaN&80", "inf&80", "-1&80", "secret-value&80", "70&", "70"):
            with self.subTest(value=value):
                data = response()
                keyed(data, "MySQL_MemCpuUsage")["Values"]["PerformanceValue"][0]["Value"] = value
                with self.assertRaises(RdsPerformanceError) as caught:
                    RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(options(), NOW)
                self.assertNotIn("secret-value", str(caught.exception))

    def test_invalid_value_format_never_leaks_response_text(self):
        for fields in (None, "", "cpuusage&CPUUsage", "memusage&cpuusage&", "access_key=SECRET&cpuusage"):
            with self.subTest(fields=fields):
                data = response()
                keyed(data, "MySQL_MemCpuUsage")["ValueFormat"] = fields
                with self.assertRaises(RdsPerformanceError) as caught:
                    RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(options(), NOW)
                self.assertNotIn("SECRET", str(caught.exception))

    def test_two_metrics_cannot_map_to_same_column(self):
        instance = {**INSTANCE, "field_names": {"db_cpu_percent": "memusage", "db_memory_percent": "memusage"}}
        with self.assertRaises(RdsPerformanceError) as caught:
            RdsPerformanceCollector(FakeProvider(metadata(), response())).collect(options(instances=[instance]), NOW)
        self.assertEqual(caught.exception.code, "invalid_format")

    def test_invalid_dates_fail_without_echoing_values(self):
        for value in (None, "2026-09-10T03:59:00", "SECRET", 1234):
            with self.subTest(value=value):
                data = response()
                keyed(data, "MySQL_Sessions")["Values"]["PerformanceValue"][0]["Date"] = value
                self.assert_code(data, "invalid_time")

    def test_instance_metadata_requires_valid_capacity(self):
        for field in ("DBInstanceStorage", "MaxConnections"):
            for value in (None, 0, -1, True, "", "NaN", "infinity", "secret"):
                with self.subTest(field=field, value=value):
                    provider = FakeProvider(metadata(**{field: value}))
                    with self.assertRaises(RdsPerformanceError) as caught:
                        RdsPerformanceCollector(provider).collect(options(), NOW)
                    self.assertEqual(caught.exception.code, "invalid_capacity")
                    self.assertEqual(len(provider.calls), 1)

    def test_wrong_metadata_and_response_resource_are_rejected(self):
        for field, value in (("DBInstanceId", "rm-other"), ("Engine", "PostgreSQL")):
            with self.subTest(field=field):
                with self.assertRaises(RdsPerformanceError) as caught:
                    RdsPerformanceCollector(FakeProvider(metadata(**{field: value}))).collect(options(), NOW)
                self.assertEqual(caught.exception.code, "metadata_mismatch")
                data = response()
                data[field] = value
                self.assert_code(data, "response_mismatch")

    def test_malformed_top_level_and_key_lists(self):
        for data in ([], {}, {"DBInstanceId": "rm-test", "Engine": "MySQL"},
                     {"DBInstanceId": "rm-test", "Engine": "MySQL", "PerformanceKeys": {"PerformanceKey": {}}}):
            with self.subTest(data=data):
                with self.assertRaises(RdsPerformanceError):
                    RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(options(), NOW)

    def test_unrequested_key_does_not_replace_missing_requested_key(self):
        data = response()
        keyed(data, "MySQL_Sessions")["Key"] = "UnrequestedMetric"
        self.assert_code(data, "missing_key")

    def test_valid_zero_values_remain_valid(self):
        data = response()
        for row in keyed(data, "MySQL_MemCpuUsage")["Values"]["PerformanceValue"]:
            row["Value"] = "0&0"
        metrics = RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(options(), NOW)
        self.assertEqual({m.value for m in metrics if m.metric in {"db_cpu_percent", "db_memory_percent"}}, {0.0})

    def test_invalid_config_fails_without_any_network_access(self):
        invalid = [
            {}, {"instances": []}, {"instances": [None]}, {"instances": [{}]},
            options(instances=[{**INSTANCE, "engine": "SQLServer"}]),
            options(instances=[{**INSTANCE, "replication_role": "standby"}]),
            options(instances=[{**INSTANCE, "replication_role": []}]),
            options(instances=[{**INSTANCE, "serverless": "true"}]),
            options(instances=[{**INSTANCE, "engine": "PostgreSQL", "serverless": False}]),
            options(instances=[{**INSTANCE, "engine": "PostgreSQL", "replication_role": "primary"}]),
            options(instances=[{**INSTANCE, "field_names": {"unknown": "cpu"}}]),
            options(instances=[{**INSTANCE, "field_names": {"db_cpu_percent": "cpu&mem"}}]),
            options(instances=[{**INSTANCE, "field_names": {"db_replication_delay_seconds": "delay"}}]),
            options(instances=[{**INSTANCE, "node_id": ""}]),
            options(instances=[{**INSTANCE, "resource_name": 1}]),
            options(instances=[INSTANCE, INSTANCE]),
            options(instances=[{**INSTANCE, "node_id": "a"}, {**INSTANCE, "node_id": "b", "engine": "PostgreSQL"}]),
        ]
        for data in invalid:
            with self.subTest(data=data):
                provider = FakeProvider()
                with self.assertRaises(RdsPerformanceError):
                    RdsPerformanceCollector(provider).collect(data, NOW)
                self.assertEqual(provider.calls, [])

    def test_invalid_time_settings_are_rejected(self):
        for name in ("metadata_cache_seconds", "lookback_seconds", "max_age_seconds", "sample_max_gap_seconds"):
            for value in (0, -1, True, "60", 1.5, None):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(RdsPerformanceError):
                        validate_rds_performance_options(options(**{name: value}))

    def test_pg_optional_keys_are_batched_with_core_metrics(self):
        instance = {**INSTANCE, "engine": "PostgreSQL", "deadlock_rate": True, "long_query_seconds": 5}
        provider = FakeProvider(metadata(Engine="PostgreSQL"), pg_optional_response())
        metrics = RdsPerformanceCollector(provider).collect(options(instances=[instance]), NOW)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(set(provider.calls[1][3]["Key"].split(",")), {
            "CpuUsage", "MemoryUsage", "PgSQL_SpaceUsage", "PgSQL_Session", "PolarDBQPSTPS", "PolarDBLongSQL"})
        self.assertEqual(len(metrics), 12)
        deadlock = next(m for m in metrics if m.metric == "db_deadlocks_per_second")
        long_query = next(m for m in metrics if m.metric == "db_long_running_queries")
        self.assertEqual(deadlock.value, 0.0166667)
        self.assertEqual(deadlock.unit, "count/s")
        self.assertEqual(deadlock.dimensions["source_field"], "mean_deadlocks_delta")
        self.assertEqual(long_query.value, 1)
        self.assertEqual(long_query.unit, "count")
        self.assertEqual(long_query.dimensions["source_field"], "mean_five_seconds_executing_sqls")
        self.assertEqual(long_query.dimensions["long_query_seconds"], 5)
        self.assertEqual(long_query.dimensions["identity"]["long_query_seconds"], 5)
        self.assertNotIn("long_query_seconds", deadlock.dimensions)
        self.assertNotIn("long_query_seconds", deadlock.dimensions["identity"])

    def test_pg_optional_metrics_are_not_requested_unless_enabled(self):
        instance = {**INSTANCE, "engine": "PostgreSQL", "deadlock_rate": False}
        provider = FakeProvider(metadata(Engine="PostgreSQL"), response("PostgreSQL"))
        metrics = RdsPerformanceCollector(provider).collect(options(instances=[instance]), NOW)
        self.assertEqual(len(metrics), 8)
        self.assertNotIn("PolarDBQPSTPS", provider.calls[1][3]["Key"])
        self.assertNotIn("PolarDBLongSQL", provider.calls[1][3]["Key"])

    def test_pg_one_three_and_five_second_buckets_use_matching_fields(self):
        for seconds, expected_value, expected_field in (
            (1, 8, "mean_one_second_executing_sqls"),
            (3, 3, "mean_three_seconds_executing_sqls"),
            (5, 1, "mean_five_seconds_executing_sqls"),
        ):
            with self.subTest(seconds=seconds):
                instance = {**INSTANCE, "engine": "PostgreSQL", "long_query_seconds": seconds}
                provider = FakeProvider(metadata(Engine="PostgreSQL"), pg_optional_response(deadlocks=False))
                metrics = RdsPerformanceCollector(provider).collect(options(instances=[instance]), NOW)
                long_query = next(m for m in metrics if m.metric == "db_long_running_queries")
                self.assertEqual(long_query.value, expected_value)
                self.assertEqual(long_query.dimensions["source_field"], expected_field)
                self.assertEqual(long_query.dimensions["identity"]["long_query_seconds"], seconds)

    def test_pg_changing_long_query_bucket_changes_only_its_identity(self):
        provider = FakeProvider(metadata(Engine="PostgreSQL"), pg_optional_response(), pg_optional_response())
        collector = RdsPerformanceCollector(provider)
        instance = {**INSTANCE, "engine": "PostgreSQL", "deadlock_rate": True, "long_query_seconds": 1}
        first = collector.collect(options(instances=[instance]), NOW)
        second = collector.collect(options(instances=[{**instance, "long_query_seconds": 5}]), NOW)
        first_keys = {metric.metric: metric.key() for metric in first}
        second_keys = {metric.metric: metric.key() for metric in second}
        self.assertNotEqual(first_keys.pop("db_long_running_queries"), second_keys.pop("db_long_running_queries"))
        self.assertEqual(first_keys, second_keys)
        self.assertEqual(len(provider.calls), 3)

    def test_pg_deadlock_rate_is_not_converted_into_hourly_count(self):
        instance = {**INSTANCE, "engine": "PostgreSQL", "deadlock_rate": True}
        provider = FakeProvider(metadata(Engine="PostgreSQL"), pg_optional_response(long_queries=False))
        metrics = RdsPerformanceCollector(provider).collect(options(instances=[instance]), NOW)
        rates = [m for m in metrics if m.metric == "db_deadlocks_per_second"]
        self.assertEqual([m.value for m in rates], [0.0166667, 0.0166667])
        self.assertTrue(all(m.unit == "count/s" for m in rates))
        self.assertTrue(all("hour" not in m.metric for m in metrics))

    def test_pg_deadlock_rate_unit_aliases_are_key_specific(self):
        instance = {**INSTANCE, "engine": "PostgreSQL", "deadlock_rate": True}
        for unit in ("Count/s", "counts/sec", "COUNT/SECOND", "1/s", "\u6b21/\u79d2"):
            with self.subTest(unit=unit):
                provider = FakeProvider(metadata(Engine="PostgreSQL"),
                                        pg_optional_response(long_queries=False, deadlock_unit=unit))
                metrics = RdsPerformanceCollector(provider).collect(options(instances=[instance]), NOW)
                self.assertEqual({m.unit for m in metrics if m.metric == "db_deadlocks_per_second"}, {"count/s"})

    def test_pg_long_query_count_unit_aliases_are_key_specific(self):
        instance = {**INSTANCE, "engine": "PostgreSQL", "long_query_seconds": 5}
        for unit in ("Count", "counts", "int", "Integer", "queries", "\u4e2a", "\u6761"):
            with self.subTest(unit=unit):
                provider = FakeProvider(metadata(Engine="PostgreSQL"),
                                        pg_optional_response(deadlocks=False, long_query_unit=unit))
                metrics = RdsPerformanceCollector(provider).collect(options(instances=[instance]), NOW)
                self.assertEqual({m.unit for m in metrics if m.metric == "db_long_running_queries"}, {"count"})

    def test_pg_rate_and_count_units_are_not_interchangeable(self):
        for key, bad_units in (
            ("PolarDBQPSTPS", ("Count", "int", "Integer", "%", "bytes", "seconds", "", None)),
            ("PolarDBLongSQL", ("count/s", "\u6b21/\u79d2", "%", "bytes", "seconds", "", None)),
        ):
            for unit in bad_units:
                with self.subTest(key=key, unit=unit):
                    data = pg_optional_response()
                    keyed(data, key)["Unit"] = unit
                    instance = {**INSTANCE, "engine": "PostgreSQL", "deadlock_rate": True, "long_query_seconds": 5}
                    with self.assertRaises(RdsPerformanceError) as caught:
                        RdsPerformanceCollector(FakeProvider(metadata(Engine="PostgreSQL"), data)).collect(
                            options(instances=[instance]), NOW)
                    self.assertEqual(caught.exception.code, "invalid_unit")

    def test_pg_new_metric_field_mapping_accepts_matching_names_case_insensitively(self):
        data = pg_optional_response()
        instance = {**INSTANCE, "engine": "PostgreSQL", "deadlock_rate": True, "long_query_seconds": 5,
                    "field_names": {"db_deadlocks_per_second": "MEAN_DEADLOCKS_DELTA",
                                    "db_long_running_queries": "MEAN_FIVE_SECONDS_EXECUTING_SQLS"}}
        metrics = RdsPerformanceCollector(FakeProvider(metadata(Engine="PostgreSQL"), data)).collect(
            options(instances=[instance]), NOW)
        self.assertEqual({m.value for m in metrics if m.metric == "db_long_running_queries"}, {1})
        self.assertEqual({m.value for m in metrics if m.metric == "db_deadlocks_per_second"}, {0.0166667})

    def test_pg_deadlock_field_mapping_cannot_select_transactions_or_unknown_fields(self):
        for field in ("mean_commits_delta", "mean_rollbacks_delta", "mean_tps", "deadlocks_rate"):
            with self.subTest(field=field):
                instance = {**INSTANCE, "engine": "PostgreSQL", "deadlock_rate": True,
                            "field_names": {"db_deadlocks_per_second": field}}
                provider = FakeProvider()
                with self.assertRaises(RdsPerformanceError) as caught:
                    RdsPerformanceCollector(provider).collect(options(instances=[instance]), NOW)
                self.assertEqual(caught.exception.code, "invalid_fields")
                self.assertEqual(provider.calls, [])

    def test_pg_long_query_field_mapping_cannot_select_a_different_duration_bucket(self):
        fields = {1: "mean_one_second_executing_sqls", 3: "mean_three_seconds_executing_sqls",
                  5: "mean_five_seconds_executing_sqls"}
        for seconds, matching_field in fields.items():
            for chosen_field in (*fields.values(), "long_running_queries"):
                with self.subTest(seconds=seconds, chosen_field=chosen_field):
                    instance = {**INSTANCE, "engine": "PostgreSQL", "long_query_seconds": seconds,
                                "field_names": {"db_long_running_queries": chosen_field}}
                    if chosen_field == matching_field:
                        validate_rds_performance_options(options(instances=[instance]))
                    else:
                        provider = FakeProvider()
                        with self.assertRaises(RdsPerformanceError) as caught:
                            RdsPerformanceCollector(provider).collect(options(instances=[instance]), NOW)
                        self.assertEqual(caught.exception.code, "invalid_fields")
                        self.assertEqual(provider.calls, [])

    def test_pg_new_metric_missing_series_or_field_fails(self):
        instance = {**INSTANCE, "engine": "PostgreSQL", "deadlock_rate": True, "long_query_seconds": 5}
        for key in ("PolarDBQPSTPS", "PolarDBLongSQL"):
            for failure in ("missing_key", "missing_field", "invalid_values", "stale_series"):
                with self.subTest(key=key, failure=failure):
                    data = pg_optional_response()
                    entry_data = keyed(data, key)
                    if failure == "missing_key":
                        data["PerformanceKeys"]["PerformanceKey"].remove(entry_data)
                    elif failure == "missing_field":
                        entry_data["ValueFormat"] = "unknown_one&unknown_two&unknown_three"
                    elif failure == "invalid_values":
                        entry_data["Values"]["PerformanceValue"] = []
                    else:
                        for point in entry_data["Values"]["PerformanceValue"]:
                            point["Date"] = (NOW - timedelta(seconds=301)).isoformat()
                    with self.assertRaises(RdsPerformanceError) as caught:
                        RdsPerformanceCollector(FakeProvider(metadata(Engine="PostgreSQL"), data)).collect(
                            options(instances=[instance]), NOW)
                    self.assertEqual(caught.exception.code, failure)

    def test_pg_new_metric_invalid_values_fail_and_zero_is_valid(self):
        instance = {**INSTANCE, "engine": "PostgreSQL", "deadlock_rate": True, "long_query_seconds": 5}
        for key, position in (("PolarDBQPSTPS", 1), ("PolarDBLongSQL", 2)):
            for value in ("-1", "NaN", "inf", "secret", "0"):
                with self.subTest(key=key, value=value):
                    data = pg_optional_response()
                    for point in keyed(data, key)["Values"]["PerformanceValue"]:
                        values = point["Value"].split("&")
                        values[position] = value
                        point["Value"] = "&".join(values)
                    collector = RdsPerformanceCollector(FakeProvider(metadata(Engine="PostgreSQL"), data))
                    if value == "0":
                        metrics = collector.collect(options(instances=[instance]), NOW)
                        expected_metric = "db_deadlocks_per_second" if key == "PolarDBQPSTPS" else "db_long_running_queries"
                        self.assertEqual({m.value for m in metrics if m.metric == expected_metric}, {0})
                    else:
                        with self.assertRaises(RdsPerformanceError) as caught:
                            collector.collect(options(instances=[instance]), NOW)
                        self.assertEqual(caught.exception.code, "invalid_value")
                        self.assertNotIn("secret", str(caught.exception))

    def test_pg_optional_settings_reject_wrong_engine_types_and_unsupported_seconds(self):
        settings = [
            {"engine": "MySQL", "deadlock_rate": True},
            {"engine": "MySQL", "deadlock_rate": False},
            {"engine": "MySQL", "long_query_seconds": 5},
        ]
        settings.extend({"engine": "PostgreSQL", "deadlock_rate": value}
                        for value in ("true", 1, 0, None, [], {}))
        settings.extend({"engine": "PostgreSQL", "long_query_seconds": value}
                        for value in (True, False, 0, -1, 2, 4, 30, "5", 5.0, None, [], {}))
        for setting in settings:
            with self.subTest(setting=setting):
                provider = FakeProvider()
                with self.assertRaises(RdsPerformanceError):
                    RdsPerformanceCollector(provider).collect(options(instances=[{**INSTANCE, **setting}]), NOW)
                self.assertEqual(provider.calls, [])

    def test_pg_optional_field_mapping_requires_the_metric_to_be_enabled(self):
        for engine in ("MySQL", "PostgreSQL"):
            for metric in ("db_deadlocks_per_second", "db_long_running_queries"):
                with self.subTest(engine=engine, metric=metric):
                    with self.assertRaises(RdsPerformanceError) as caught:
                        validate_rds_performance_options(options(instances=[{
                            **INSTANCE, "engine": engine, "field_names": {metric: "example"}}]))
                    self.assertEqual(caught.exception.code, "invalid_fields")

    def assert_code(self, data, code):
        with self.assertRaises(RdsPerformanceError) as caught:
            RdsPerformanceCollector(FakeProvider(metadata(), data)).collect(options(), NOW)
        self.assertEqual(caught.exception.code, code)


class RdsRowLockWaitTests(unittest.TestCase):
    def collect(self, data, instance=None, **settings):
        provider = FakeProvider(metadata(), data)
        configured = {**INSTANCE, "row_lock_waits": True, **(instance or {})}
        metrics = RdsPerformanceCollector(provider).collect(options(instances=[configured], **settings), NOW)
        return metrics, provider

    def rates(self, data, **settings):
        metrics, _ = self.collect(data, **settings)
        return [metric for metric in metrics if metric.metric == "db_row_lock_waits_per_second"]

    def assert_error(self, data, expected, **settings):
        with self.assertRaises(RdsPerformanceError) as caught:
            self.collect(data, **settings)
        self.assertEqual(caught.exception.code, expected)

    def test_constant_historical_counter_is_zero_rate_not_455_waits_per_second(self):
        rates = self.rates(row_lock_response((455, "455.0", 455), (180, 120, 60)))
        self.assertEqual([metric.value for metric in rates], [0, 0])
        self.assertEqual([metric.observed_at for metric in rates],
                         [NOW - timedelta(seconds=120), NOW - timedelta(seconds=60)])
        self.assertTrue(all(metric.unit == "count/s" for metric in rates))
        self.assertTrue(all(metric.dimensions["source_counter"] == 455 for metric in rates))
        self.assertTrue(all(metric.dimensions["source_field"] == "innodb_row_lock_waits" for metric in rates))

    def test_sixty_new_waits_per_minute_is_one_per_second(self):
        rates = self.rates(row_lock_response((455, 515, 575), (180, 120, 60)))
        self.assertEqual([metric.value for metric in rates], [1, 1])
        self.assertTrue(all(metric.dimensions["sample_seconds"] == 60 for metric in rates))
        self.assertTrue(all(metric.dimensions["sample_max_gap_seconds"] == 90 for metric in rates))

    def test_incident_fractional_counter_preserves_full_history_across_collection_rounds(self):
        incident = datetime(2026, 10, 12, 6, 1, 22, tzinfo=timezone.utc)
        ages = tuple(range(922, 81, -60))
        responses = []
        for minute, counts in enumerate((
            ["824.0"] * 14 + ["826.9"],
            ["824.0"] * 13 + ["826.9", "827.0"],
        )):
            now = incident + timedelta(minutes=minute)
            data = response(ages=ages, now=now)
            lock = entry("MySQL_ROW_LOCK", "innodb_row_lock_waits&innodb_row_lock_time&innodb_row_lock_time_avg",
                         "0&111032&244", "int", ages, now)
            for row, count in zip(lock["Values"]["PerformanceValue"], counts):
                row["Value"] = f"{count}&111032&244"
            data["PerformanceKeys"]["PerformanceKey"].extend([
                lock, entry("MySQL_ReplicationDelay", "replication-lag", "0", "s", ages, now),
            ])
            responses.append(data)
        provider = FakeProvider(metadata(), *responses)
        collector = RdsPerformanceCollector(provider)
        configured = options(instances=[{**INSTANCE, "row_lock_waits": True, "replication_role": "primary"}])
        for minute, expected_rate in enumerate((2.9 / 60, 0.1 / 60)):
            with self.subTest(minute=minute):
                metrics = collector.collect(configured, incident + timedelta(minutes=minute))
                self.assertEqual(len(metrics), 89)
                rates = [metric for metric in metrics if metric.metric == "db_row_lock_waits_per_second"]
                self.assertEqual(len(rates), 14)
                self.assertEqual(rates[-1].value, expected_rate)
                self.assertEqual(rates[-1].dimensions["source_counter"], (826.9, 827)[minute])
                json.dumps([metric.to_dict() for metric in metrics], allow_nan=False)
        self.assertEqual([call[1] for call in provider.calls], [
            "DescribeDBInstanceAttribute", "DescribeDBInstancePerformance", "DescribeDBInstancePerformance",
        ])

    def test_fractional_counter_delta_is_calculated_before_float_conversion(self):
        counter = 2**60
        rates = self.rates(row_lock_response((str(counter) + ".1", str(counter) + ".2")))
        self.assertEqual(rates[0].value, 0.1 / 60)
        rates = self.rates(row_lock_response(("455.0000000000000001", "455.0000000000000002")))
        self.assertEqual(rates[0].value, 1e-16 / 60)

    def test_fractional_counter_reset_and_gap_still_restart_baseline(self):
        for counts, ages in (
            (("824.0", "826.9", "1.1", "1.2"), (240, 180, 120, 60)),
            (("824.0", "826.9", "827.0", "827.1"), (360, 300, 120, 60)),
        ):
            with self.subTest(counts=counts, ages=ages):
                rates = self.rates(row_lock_response(counts, ages))
                self.assertEqual([metric.value for metric in rates], [2.9 / 60, 0.1 / 60])
        self.assert_error(row_lock_response(("2.9", "1.1", "0.9"), (180, 120, 60)), "counter_baseline")
        self.assert_error(row_lock_response(("824.0", "826.9", "1.1"), (420, 360, 60)), "stale_series")

    def test_actual_elapsed_seconds_determine_rate_and_gap_tolerance(self):
        rates = self.rates(row_lock_response((100, 130, 190), (150, 120, 90)))
        self.assertEqual([metric.value for metric in rates], [1, 2])
        self.assertTrue(all(metric.dimensions["sample_seconds"] == 30 for metric in rates))
        self.assertTrue(all(metric.dimensions["sample_max_gap_seconds"] == 45 for metric in rates))

    def test_row_lock_key_shares_original_performance_request_and_preserves_other_identities(self):
        metrics, provider = self.collect(row_lock_response())
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual([call[1] for call in provider.calls],
                         ["DescribeDBInstanceAttribute", "DescribeDBInstancePerformance"])
        self.assertEqual(set(provider.calls[1][3]["Key"].split(",")), {
            "MySQL_Sessions", "MySQL_MemCpuUsage", "MySQL_DetailedSpaceUsage", "MySQL_ROW_LOCK"})
        baseline = RdsPerformanceCollector(FakeProvider(metadata(), response())).collect(options(), NOW)
        before = {metric.metric: metric.key() for metric in baseline}
        after = {metric.metric: metric.key() for metric in metrics if metric.metric in before}
        self.assertEqual(before, after)
        self.assertEqual(len(metrics), len(baseline) + 1)

    def test_false_or_absent_option_never_queries_row_lock_key(self):
        for configured in (dict(INSTANCE), {**INSTANCE, "row_lock_waits": False}):
            provider = FakeProvider(metadata(), response())
            metrics = RdsPerformanceCollector(provider).collect(options(instances=[configured]), NOW)
            self.assertNotIn("MySQL_ROW_LOCK", provider.calls[1][3]["Key"])
            self.assertNotIn("db_row_lock_waits_per_second", {metric.metric for metric in metrics})

    def test_unsorted_points_and_identical_duplicates_are_differenced_once(self):
        rates = self.rates(row_lock_response((575, 455, 515, 515), (60, 180, 120, 120)))
        self.assertEqual([metric.value for metric in rates], [1, 1])
        self.assertEqual([metric.observed_at for metric in rates], sorted(metric.observed_at for metric in rates))

    def test_conflicting_duplicate_counter_is_rejected(self):
        self.assert_error(row_lock_response((455, 456), (60, 60)), "duplicate_point")

    def test_reset_skips_crossing_interval_and_restarts_baseline(self):
        rates = self.rates(row_lock_response((400, 460, 1, 61), (240, 180, 120, 60)))
        self.assertEqual([metric.value for metric in rates], [1, 1])
        self.assertEqual([metric.observed_at for metric in rates],
                         [NOW - timedelta(seconds=180), NOW - timedelta(seconds=60)])
        self.assertGreater((rates[1].observed_at - rates[0].observed_at).total_seconds(),
                           rates[1].dimensions["sample_max_gap_seconds"])

    def test_gap_skips_crossing_interval_and_restarts_baseline(self):
        rates = self.rates(row_lock_response((400, 460, 520), (240, 60, 0)))
        self.assertEqual(len(rates), 1)
        self.assertEqual(rates[0].observed_at, NOW)
        self.assertEqual(rates[0].value, 1)
        self.assertEqual(rates[0].dimensions["sample_seconds"], 60)

    def test_counter_gap_tolerance_is_capped_even_if_configuration_allows_longer_gaps(self):
        self.assert_error(row_lock_response((0, 300), (360, 60)), "counter_baseline",
                          sample_max_gap_seconds=600)

    def test_one_missing_fast_cadence_sample_breaks_counter_continuity(self):
        rates = self.rates(row_lock_response((100, 105, 115, 120), (25, 20, 10, 5)))
        self.assertEqual([metric.value for metric in rates], [1, 1])
        self.assertEqual([metric.observed_at for metric in rates],
                         [NOW - timedelta(seconds=20), NOW - timedelta(seconds=5)])
        self.assertTrue(all(metric.dimensions["sample_max_gap_seconds"] == 7.5 for metric in rates))

    def test_single_point_only_resets_or_only_gaps_cannot_report_success(self):
        for counts, ages in (((455,), (60,)), ((20, 10, 5), (180, 120, 60)), ((20, 30), (240, 60))):
            with self.subTest(counts=counts, ages=ages):
                self.assert_error(row_lock_response(counts, ages), "counter_baseline")

    def test_fresh_raw_baseline_does_not_make_old_derived_rate_fresh(self):
        self.assert_error(row_lock_response((400, 460, 1), (420, 360, 60)), "stale_series")

    def test_recent_valid_rate_survives_latest_reset_without_fabricating_zero(self):
        rates = self.rates(row_lock_response((400, 460, 1), (180, 120, 60)))
        self.assertEqual(len(rates), 1)
        self.assertEqual(rates[0].value, 1)
        self.assertEqual(rates[0].observed_at, NOW - timedelta(seconds=120))

    def test_counter_units_accept_counts_but_not_duration_or_rates(self):
        for unit in ("int", "Integer", "Count", "counts", "Number"):
            self.assertEqual(self.rates(row_lock_response(unit=unit))[0].value, 0)
        for unit in ("seconds", "ms", "count/s", "%", "connections", "bytes", "", None):
            with self.subTest(unit=unit):
                self.assert_error(row_lock_response(unit=unit), "invalid_unit")

    def test_counter_requires_nonnegative_finite_number(self):
        for counter in ("-1", "-0.1", "nan", "Infinity", "secret-value", "True", ""):
            with self.subTest(counter=counter):
                self.assert_error(row_lock_response((counter, counter)), "invalid_value")
        for counter in ("455.0", "4.55e2", "0", "0.0", "455.5", "455.0000000000000001"):
            self.assertEqual(self.rates(row_lock_response((counter, counter)))[0].value, 0)

    def test_counter_delta_is_exact_above_float_integer_precision(self):
        counter = 2**60
        rates = self.rates(row_lock_response((counter, counter + 1), (120, 60)))
        self.assertEqual(rates[0].value, 1 / 60)
        self.assertEqual(rates[0].dimensions["source_counter"], counter + 1)

    def test_counter_field_is_identified_by_name_not_position(self):
        data = row_lock_response((455, 515))
        item = keyed(data, "MySQL_ROW_LOCK")
        item["ValueFormat"] = "innodb_row_lock_time_avg&innodb_row_lock_waits&innodb_row_lock_time"
        for row, count in zip(item["Values"]["PerformanceValue"], (455, 515)):
            row["Value"] = f"1.5&{count}&9000"
        self.assertEqual(self.rates(data)[0].value, 1)

    def test_counter_field_mapping_cannot_select_wait_duration_or_an_unknown_alias(self):
        metric_name = "db_row_lock_waits_per_second"
        for field in ("innodb_row_lock_time", "innodb_row_lock_time_avg", "row_lock_waits"):
            provider = FakeProvider()
            with self.subTest(field=field), self.assertRaises(RdsPerformanceError) as caught:
                RdsPerformanceCollector(provider).collect(options(instances=[{
                    **INSTANCE, "row_lock_waits": True, "field_names": {metric_name: field}}]), NOW)
            self.assertEqual(caught.exception.code, "invalid_fields")
            self.assertEqual(provider.calls, [])
        rates = self.rates(row_lock_response(), instance={"field_names": {metric_name: "INNODB_ROW_LOCK_WAITS"}})
        self.assertEqual(rates[0].value, 0)

    def test_unknown_missing_empty_and_stale_counter_series_fail(self):
        self.assert_error(response(), "missing_key")
        for failure in ("missing_field", "invalid_values", "stale_series"):
            data = row_lock_response()
            item = keyed(data, "MySQL_ROW_LOCK")
            if failure == "missing_field":
                item["ValueFormat"] = "unknown_waits&innodb_row_lock_time&innodb_row_lock_time_avg"
            elif failure == "invalid_values":
                item["Values"]["PerformanceValue"] = []
            else:
                item["Values"]["PerformanceValue"] = [
                    {"Date": (NOW - timedelta(seconds=400)).isoformat(), "Value": "455&9000&1.5"}]
            self.assert_error(data, failure)

    def test_counter_setting_requires_mysql_boolean_and_enabled_for_field_mapping(self):
        for engine, enabled in (("PostgreSQL", True), ("PostgreSQL", False),
                                *(("MySQL", value) for value in (1, 0, "true", None, [], {}))):
            provider = FakeProvider()
            with self.subTest(engine=engine, enabled=enabled), self.assertRaises(RdsPerformanceError) as caught:
                RdsPerformanceCollector(provider).collect(options(instances=[{
                    **INSTANCE, "engine": engine, "row_lock_waits": enabled}]), NOW)
            self.assertEqual(caught.exception.code, "invalid_row_lock_waits")
            self.assertEqual(provider.calls, [])
        for enabled in (False, None):
            instance = {**INSTANCE, "field_names": {"db_row_lock_waits_per_second": "innodb_row_lock_waits"}}
            if enabled is not None:
                instance["row_lock_waits"] = enabled
            with self.assertRaises(RdsPerformanceError) as caught:
                validate_rds_performance_options(options(instances=[instance]))
            self.assertEqual(caught.exception.code, "invalid_fields")


if __name__ == "__main__":
    unittest.main()
