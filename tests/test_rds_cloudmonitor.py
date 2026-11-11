import copy
from datetime import datetime, timedelta, timezone
import json
import unittest

from cloud_monitor.providers.aliyun import AliyunCollector
from cloud_monitor.providers.rds_cloudmonitor import (
    NAMESPACE,
    RdsCloudMonitorCollector,
    RdsCloudMonitorError,
    validate_rds_cloudmonitor_options,
)


NOW = datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc)
INSTANCE = {"resource_id": "rm-test", "engine": "MySQL", "region": "cn-hangzhou"}


class FakeProvider(AliyunCollector):
    def __init__(self, *responses):
        super().__init__({"id": "account-test"})
        self.responses = list(responses)
        self.calls = []

    def _rpc(self, service, action, version, params, region=None):
        self.calls.append((service, action, version, copy.deepcopy(params), region))
        if not self.responses:
            raise AssertionError("Unexpected cloud request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def options(**changes):
    return {"instances": [dict(INSTANCE)], **changes}


def metadata(engine="MySQL", **changes):
    row = {
        "Namespace": NAMESPACE,
        "MetricName": "MySQL_SlowQueries" if engine == "MySQL" else "PG_SharedBufferHitRate",
        "Unit": "count/s" if engine == "MySQL" else "%",
        "Dimensions": "userId,instanceId", "Periods": "5,60,300", "Statistics": "Average,Maximum,Minimum",
        **changes,
    }
    return {"Resources": {"Resource": [row]}, "TotalCount": "1", "Success": True, "Code": "200"}


def point(resource="rm-test", value=12.5, age=60, now=NOW):
    return {"instanceId": resource, "Average": value,
            "timestamp": int((now - timedelta(seconds=age)).timestamp() * 1000)}


def response(*points, token=None):
    result = {"Datapoints": json.dumps(list(points)), "Success": True, "Code": "200"}
    if token is not None:
        result["NextToken"] = token
    return result


class RdsCloudMonitorTests(unittest.TestCase):
    def test_two_instances_share_one_query_and_keep_all_points(self):
        instances = [INSTANCE, {**INSTANCE, "resource_id": "rm-other", "resource_name": "Orders database"}]
        provider = FakeProvider(metadata(), response(
            point(), point("rm-other", 0), point(age=120), point("rm-other", 1, age=120)))
        metrics = RdsCloudMonitorCollector(provider).collect(options(instances=instances), NOW)
        self.assertEqual([call[1] for call in provider.calls], ["DescribeMetricMetaList", "DescribeMetricList"])
        self.assertEqual(provider.calls[0][3], {
            "Namespace": NAMESPACE, "MetricName": "MySQL_SlowQueries", "PageNumber": 1, "PageSize": 100})
        query = provider.calls[1]
        self.assertEqual(query[:3], ("cms", "DescribeMetricList", "2019-01-01"))
        self.assertEqual(query[3]["Dimensions"], [{"instanceId": "rm-test"}, {"instanceId": "rm-other"}])
        self.assertEqual(query[3]["Length"], 1440)
        self.assertEqual(query[3]["Period"], 60)
        self.assertEqual(query[3]["StartTime"], int((NOW - timedelta(seconds=900)).timestamp() * 1000))
        self.assertEqual(len(metrics), 4)
        self.assertEqual([m.observed_at for m in metrics], sorted(m.observed_at for m in metrics))
        self.assertEqual({m.metric for m in metrics}, {"db_slow_queries_per_second"})
        self.assertEqual({m.unit for m in metrics}, {"count/s"})
        self.assertEqual({m.resource_name for m in metrics}, {"rm-test", "Orders database"})
        self.assertEqual({m.value for m in metrics}, {0, 1, 12.5})
        self.assertTrue(all(m.dimensions["aggregation"] == "Average" for m in metrics))

    def test_more_than_fifty_instances_split_queries(self):
        instances = [{**INSTANCE, "resource_id": f"rm-{number}"} for number in range(51)]
        provider = FakeProvider(metadata(), response(*(point(f"rm-{n}") for n in range(50))),
                                response(point("rm-50")))
        metrics = RdsCloudMonitorCollector(provider).collect(options(instances=instances), NOW)
        queries = [call for call in provider.calls if call[1] == "DescribeMetricList"]
        self.assertEqual([len(call[3]["Dimensions"]) for call in queries], [50, 1])
        self.assertEqual(len(metrics), 51)

    def test_regions_and_engines_use_separate_queries_and_metadata(self):
        instances = [INSTANCE, {**INSTANCE, "region": "cn-shanghai"},
                     {**INSTANCE, "resource_id": "pg-test", "engine": "PostgreSQL"}]
        provider = FakeProvider(metadata(), response(point()), metadata(), response(point()),
                                metadata("PostgreSQL"), response(point("pg-test", 94)))
        metrics = RdsCloudMonitorCollector(provider).collect(options(instances=instances), NOW)
        self.assertEqual(len(provider.calls), 6)
        self.assertEqual([call[4] for call in provider.calls[1::2]],
                         ["cn-hangzhou", "cn-shanghai", "cn-hangzhou"])
        pg = next(m for m in metrics if m.resource_id == "pg-test")
        self.assertEqual((pg.metric, pg.unit, pg.value), ("db_cache_hit_percent", "%", 94))
        self.assertEqual(len({m.key() for m in metrics}), 3)

    def test_pagination_keeps_history_and_deduplicates_identical_points(self):
        provider = FakeProvider(metadata(), response(point(age=120), token="page-two"),
                                response(point(age=120), point()))
        metrics = RdsCloudMonitorCollector(provider).collect(options(), NOW)
        self.assertEqual(len(metrics), 2)
        self.assertEqual(provider.calls[2][3]["NextToken"], "page-two")
        self.assertNotIn("NextToken", provider.calls[1][3])

    def test_repeated_or_invalid_page_token_fails(self):
        for token in ("same", 123, False, {}):
            with self.subTest(token=token):
                provider = FakeProvider(metadata(), response(point(), token=token),
                                        response(point(), token=token))
                self.assert_code(provider, "invalid_pagination")

    def test_pagination_has_page_limit(self):
        provider = FakeProvider(metadata(), *(response(point(), token=f"page-{i}") for i in range(100)))
        self.assert_code(provider, "invalid_pagination")
        self.assertEqual(len(provider.calls), 101)

    def test_no_data_or_one_missing_instance_never_becomes_zero(self):
        self.assert_code(FakeProvider(metadata(), response()), "no_data")
        instances = [INSTANCE, {**INSTANCE, "resource_id": "rm-missing"}]
        self.assert_code(FakeProvider(metadata(), response(point())), "no_data", options(instances=instances))

    def test_each_instance_must_have_fresh_data(self):
        instances = [INSTANCE, {**INSTANCE, "resource_id": "rm-old"}]
        self.assert_code(FakeProvider(metadata(), response(point(), point("rm-old", age=301))),
                         "stale_series", options(instances=instances))
        self.assert_code(FakeProvider(metadata(), response(point(age=1000))), "stale_series")
        metrics = RdsCloudMonitorCollector(FakeProvider(metadata(), response(point(age=300)))).collect(options(), NOW)
        self.assertEqual(len(metrics), 1)

    def test_unknown_or_missing_resource_fails_instead_of_guessing(self):
        for resource in ("rm-unrequested", None, ["rm-test"]):
            self.assert_code(FakeProvider(metadata(), response(point(resource))), "unknown_resource")

    def test_conflicting_duplicates_fail_even_for_zero(self):
        self.assert_code(FakeProvider(metadata(), response(point(value=0), point(value=1))), "duplicate_point")

    def test_invalid_values_and_missing_average_fail(self):
        for value in (-1, float("nan"), float("inf"), None, True, "secret-invalid-value", {}):
            self.assert_code(FakeProvider(metadata(), response(point(value=value))), "invalid_value")
        data = point()
        del data["Average"]
        data["Maximum"] = 20
        self.assert_code(FakeProvider(metadata(), response(data)), "invalid_value")

    def test_postgres_percentage_cannot_exceed_one_hundred(self):
        pg_options = options(instances=[{**INSTANCE, "engine": "PostgreSQL"}])
        self.assert_code(FakeProvider(metadata("PostgreSQL"), response(point(value=100.1))),
                         "invalid_value", pg_options)
        for value in (0, 100):
            metrics = RdsCloudMonitorCollector(FakeProvider(metadata("PostgreSQL"), response(point(value=value))))
            self.assertEqual(metrics.collect(pg_options, NOW)[0].value, value)

    def test_future_and_invalid_timestamps_fail(self):
        self.assert_code(FakeProvider(metadata(), response(point(age=-1))), "future_point")
        for timestamp in (None, True, "not-a-date", "2026-09-10T04:00:00", float("inf")):
            data = {**point(), "timestamp": timestamp}
            self.assert_code(FakeProvider(metadata(), response(data)), "invalid_time")

    def test_old_outside_window_points_are_not_emitted(self):
        metrics = RdsCloudMonitorCollector(FakeProvider(metadata(), response(point(age=1000), point())))
        self.assertEqual(len(metrics.collect(options(), NOW)), 1)

    def test_gap_tolerance_tightens_but_sparse_points_do_not_relax_it(self):
        for ages, configured, expected in (((60, 120), 90, 90), ((60, 65), 90, 7.5), ((60, 360), 600, 90)):
            provider = FakeProvider(metadata(), response(*(point(age=age) for age in ages)))
            metrics = RdsCloudMonitorCollector(provider).collect(options(sample_max_gap_seconds=configured), NOW)
            self.assertTrue(all(m.dimensions["sample_max_gap_seconds"] == expected for m in metrics))

    def test_metadata_cache_refreshes_daily_and_does_not_include_resource_name(self):
        later = NOW + timedelta(days=1)
        provider = FakeProvider(metadata(), response(point()), response(point(now=later - timedelta(seconds=1))),
                                metadata(), response(point(now=later)))
        collector = RdsCloudMonitorCollector(provider)
        first = collector.collect(options(), NOW)
        renamed = options(instances=[{**INSTANCE, "resource_name": "New name"}])
        collector.collect(renamed, later - timedelta(seconds=1))
        final = collector.collect(renamed, later)
        self.assertEqual(first[0].key(), final[0].key())
        self.assertEqual(final[0].resource_name, "New name")
        self.assertEqual([call[1] for call in provider.calls], [
            "DescribeMetricMetaList", "DescribeMetricList", "DescribeMetricList",
            "DescribeMetricMetaList", "DescribeMetricList"])
        self.assertEqual(final[0].dimensions["identity"]["source"], "rds_cloudmonitor")

    def test_failed_refresh_never_uses_expired_metadata(self):
        provider = FakeProvider(metadata(), response(point()), metadata(Unit="Count"))
        collector = RdsCloudMonitorCollector(provider)
        collector.collect(options(), NOW)
        with self.assertRaises(RdsCloudMonitorError) as caught:
            collector.collect(options(), NOW + timedelta(days=1))
        self.assertEqual(caught.exception.code, "invalid_unit")
        self.assertEqual(len(provider.calls), 3)

    def test_failed_metadata_is_not_cached(self):
        provider = FakeProvider(metadata(Unit="Count"), metadata(), response(point()))
        collector = RdsCloudMonitorCollector(provider)
        with self.assertRaises(RdsCloudMonitorError):
            collector.collect(options(), NOW)
        self.assertEqual(len(collector.collect(options(), NOW)), 1)
        self.assertEqual([call[1] for call in provider.calls[:2]], ["DescribeMetricMetaList"] * 2)

    def test_metadata_contract_checks_name_namespace_unit_period_dimensions_statistics(self):
        for changes, code in (
            ({"MetricName": "DifferentMetric"}, "invalid_metadata"),
            ({"Namespace": "other_namespace"}, "metadata_mismatch"),
            ({"Unit": "Count"}, "invalid_unit"),
            ({"Periods": "5,300"}, "metadata_mismatch"),
            ({"Dimensions": "userId,instanceId,nodeId"}, "metadata_mismatch"),
            ({"Statistics": "Maximum,Minimum"}, "metadata_mismatch"),
            ({"Periods": None}, "invalid_metadata"),
            ({"Dimensions": "userId,instanceId,instanceId"}, "invalid_metadata"),
        ):
            self.assert_code(FakeProvider(metadata(**changes)), code)

    def test_metadata_pagination_and_resource_envelope(self):
        first = metadata(MetricName="UnrelatedMetric")
        second = metadata()
        first["TotalCount"] = second["TotalCount"] = "2"
        provider = FakeProvider(first, second, response(point()))
        self.assertEqual(len(RdsCloudMonitorCollector(provider).collect(options(), NOW)), 1)
        self.assertEqual(provider.calls[1][3]["PageNumber"], 2)
        self.assert_code(FakeProvider({"Resources": [], "TotalCount": "0"}), "invalid_metadata")

    def test_metadata_incomplete_or_ambiguous_pages_fail(self):
        data = metadata()
        data["TotalCount"] = "2"
        empty = {"Resources": {"Resource": []}, "TotalCount": "2"}
        self.assert_code(FakeProvider(data, empty), "invalid_pagination")
        self.assert_code(FakeProvider(data, copy.deepcopy(data)), "invalid_metadata")
        bad_total = metadata()
        bad_total["TotalCount"] = True
        self.assert_code(FakeProvider(bad_total), "invalid_metadata")

    def test_malformed_data_fails_with_safe_error(self):
        for data in ({}, {"Datapoints": "not-json"}, {"Datapoints": {}}, {"Datapoints": [None]}):
            self.assert_code(FakeProvider(metadata(), data), "invalid_response")
        provider = FakeProvider(RuntimeError("secret-cloud-response-token"))
        with self.assertRaises(RdsCloudMonitorError) as caught:
            RdsCloudMonitorCollector(provider).collect(options(), NOW)
        self.assertEqual(caught.exception.code, "cloud_request_failed")
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_unsuccessful_response_is_not_parsed_as_data(self):
        for failure in ({"Success": False}, {"Success": "true"}, {"Code": "403"}):
            self.assert_code(FakeProvider(failure), "cloud_request_failed")

    def test_options_validate_before_any_api_request(self):
        invalid = [None, {}, options(instances=[]), options(instances=[None]),
                   options(period=5), options(period=True), options(lookback_seconds=60),
                   options(metadata_cache_seconds=0), options(max_age_seconds="300"),
                   options(instances=[{**INSTANCE, "engine": "MariaDB"}]),
                   options(instances=[{**INSTANCE, "resource_id": ""}]),
                   options(instances=[{**INSTANCE, "resource_name": ""}]),
                   options(instances=[INSTANCE, dict(INSTANCE)])]
        for item in invalid:
            provider = FakeProvider()
            with self.subTest(item=item), self.assertRaises(RdsCloudMonitorError):
                RdsCloudMonitorCollector(provider).collect(item, NOW)
            self.assertEqual(provider.calls, [])
        validate_rds_cloudmonitor_options(options())

    def test_unsupported_fields_cannot_silently_change_metric_semantics(self):
        for field in ("long_query_seconds", "metrics", "node_id", "aggregation", "multiplier"):
            self.assert_code(FakeProvider(), "unsupported_option", options(**{field: 30}))
            self.assert_code(FakeProvider(), "unsupported_option",
                             options(instances=[{**INSTANCE, field: 30}]))

    def test_identity_is_distinct_from_other_collectors(self):
        provider = FakeProvider(metadata(), response(point()))
        metric = RdsCloudMonitorCollector(provider).collect(options(), NOW)[0]
        other = provider._metric(INSTANCE["resource_id"], "same instance", metric.metric, metric.value,
                                 metric.unit, metric.observed_at,
                                 {"identity": {"region": INSTANCE["region"], "engine": INSTANCE["engine"]}})
        self.assertNotEqual(metric.key(), other.key())

    def test_metadata_refreshes_after_clock_moves_backwards(self):
        earlier = NOW - timedelta(seconds=1)
        provider = FakeProvider(metadata(), response(point()), metadata(), response(point(now=earlier)))
        collector = RdsCloudMonitorCollector(provider)
        collector.collect(options(), NOW)
        collector.collect(options(), earlier)
        self.assertEqual([call[1] for call in provider.calls],
                         ["DescribeMetricMetaList", "DescribeMetricList"] * 2)

    def assert_code(self, provider, code, settings=None):
        with self.assertRaises(RdsCloudMonitorError) as caught:
            RdsCloudMonitorCollector(provider).collect(settings or options(), NOW)
        self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
