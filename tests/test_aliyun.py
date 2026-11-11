import json
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cloud_monitor.providers.aliyun import AliyunCollector


NOW = datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc)
ACCOUNT = {"id": "ali-test", "regions": ["cn-hangzhou"]}


class FakeCollector(AliyunCollector):
    def __init__(self, responses):
        super().__init__(ACCOUNT)
        self.responses = list(responses)
        self.calls = []

    def _rpc(self, service, action, version, params, region=None):
        self.calls.append((service, action, version, dict(params), region))
        if not self.responses:
            raise AssertionError(f"Unexpected API request: {action}")
        return self.responses.pop(0)


def package_response(**changes):
    row = {"InstanceId": "pkg-1", "ExpiryTime": "2026-09-15T04:00:00Z",
           "TotalAmount": "1", "TotalAmountUnit": "GB",
           "RemainingAmount": "102.4", "RemainingAmountUnit": "MB", "Status": "Available"}
    row.update(changes)
    return {"Data": {"TotalCount": 1, "Instances": {"Instance": [row]}}}


def metric_options(**changes):
    entry = {"namespace": "acs_oss_dashboard", "metric_name": "InternetSend",
             "dimensions": {"BucketName": "bucket-1"}, "resource_id": "bucket-1",
             "output_metric": "bandwidth_used_percent", "period": 60,
             "aggregation": "Average", "limit": 100, "multiplier": 1}
    entry.update(changes)
    return {"metrics": [entry]}


def metric_response(age=30, **changes):
    point = {"timestamp": int((NOW - timedelta(seconds=age)).timestamp() * 1000), "Average": 80}
    point.update(changes)
    return {"Datapoints": json.dumps([point])}


def sms_response(current=300, baseline=100):
    end = NOW.date() - timedelta(days=1)
    rows = [{"SendDate": (end - timedelta(days=n)).strftime("%Y%m%d"),
             "TotalCount": current if n == 0 else baseline} for n in range(8)]
    return {"Data": {"TotalSize": 8, "TargetList": rows}}


class AliyunCollectorTests(unittest.TestCase):
    def test_ecs_paginates_and_skips_pay_as_you_go(self):
        collector = FakeCollector([
            {"TotalCount": 2, "Instances": {"Instance": [
                {"InstanceId": "i-1", "InstanceName": "server", "InstanceChargeType": "PrePaid",
                 "ExpiredTime": "2026-09-15T04:00:00Z"}]}},
            {"TotalCount": 2, "Instances": {"Instance": [
                {"InstanceId": "i-2", "InstanceChargeType": "PostPaid"}]}}
        ])
        metrics = collector.collect("ecs_expiry", {}, NOW)
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0].value, 7)
        self.assertEqual(collector.calls[1][3]["PageNumber"], 2)

    def test_prepaid_missing_expiry_is_not_zero(self):
        collector = FakeCollector([{"TotalCount": 1, "Instances": {"Instance": [
            {"InstanceId": "i-1", "InstanceChargeType": "PrePaid"}]}}])
        with self.assertRaisesRegex(ValueError, "ExpiredTime"):
            collector.collect("ecs_expiry", {}, NOW)

    def test_regional_collectors_require_regions(self):
        collector = AliyunCollector({"id": "ali"})
        with self.assertRaisesRegex(ValueError, "regions"):
            collector.collect("ecs_expiry", {}, NOW)

    def test_empty_resource_list_is_legitimate(self):
        collector = FakeCollector([{"TotalCount": 0, "Instances": {"Instance": []}}])
        self.assertEqual(collector.collect("ecs_expiry", {}, NOW), [])

    def test_incomplete_pagination_is_error(self):
        collector = FakeCollector([{"TotalCount": 1, "Instances": {"Instance": []}}])
        with self.assertRaisesRegex(ValueError, "pagination"):
            collector.collect("ecs_expiry", {}, NOW)

    def test_invalid_pagination_total_is_not_empty_inventory(self):
        collector = FakeCollector([{"TotalCount": False, "Instances": {"Instance": []}}])
        with self.assertRaisesRegex(ValueError, "invalid total"):
            collector.collect("ecs_expiry", {}, NOW)

    def test_rds_only_mysql_postgres_expiry(self):
        collector = FakeCollector([{"TotalRecordCount": 3, "Items": {"DBInstance": [
            {"DBInstanceId": "db1", "Engine": "PostgreSQL", "PayType": "Prepaid",
             "ExpireTime": "2026-09-09T04:00:00Z"},
            {"DBInstanceId": "db2", "Engine": "MySQL", "PayType": "Postpaid"},
            {"DBInstanceId": "db3", "Engine": "SQLServer", "PayType": "Prepaid"}]}}])
        metrics = collector.collect("rds_expiry", {}, NOW)
        self.assertEqual([(m.resource_id, m.metric, m.value) for m in metrics], [("db1", "expiry_days", 1)])

    def test_domain_uses_epoch_milliseconds(self):
        collector = FakeCollector([{"TotalItemNum": 1, "Data": {"Domain": [
            {"DomainName": "example.cn", "ExpirationDateLong": int((NOW + timedelta(days=30)).timestamp() * 1000)}]}}])
        self.assertEqual(collector.collect("domains", {}, NOW)[0].value, 30)

    def test_certificates_include_expired_and_filter_sensitive_content(self):
        collector = FakeCollector([
            {"TotalCount": 1, "CertificateOrderList": [{"CertificateId": 9, "Name": "cert"}]},
            {"NotAfter": int((NOW + timedelta(days=1)).timestamp())},
            {"TotalCount": 1, "CertificateOrderList": [{"CertificateId": 10}]},
            {"NotAfter": int((NOW - timedelta(days=1)).timestamp() * 1000)},
        ])
        self.assertEqual([m.value for m in collector.collect("certificates", {}, NOW)], [1, -1])
        self.assertTrue(collector.calls[1][3]["CertFilter"])
        self.assertEqual(collector.calls[2][3]["Status"], "EXPIRED")

    def test_certificate_display_uses_documented_domain_fields_without_more_requests(self):
        cases = [
            ({"CommonName": "listed.example.com", "Name": "cert-auto"},
             {"Common": "actual.example.com", "Name": "console-name"}, "actual.example.com"),
            ({"CommonName": "*.example.com", "Name": "cert-auto"}, {}, "*.example.com"),
            ({"Name": "cert-auto"}, {"Sans": "one.example.com,two.example.com"},
             "one.example.com,two.example.com"),
            ({"Sans": "listed.example.com"}, {}, "listed.example.com"),
            ({"Name": "cert-auto"}, {"Name": "Production certificate"}, "Production certificate"),
            ({"Name": "Console certificate"}, {}, "Console certificate"),
            ({"CommonName": " ", "Name": None}, {"Common": None, "Name": " "}, "9"),
        ]
        for row, detail, expected in cases:
            with self.subTest(expected=expected):
                collector = FakeCollector([
                    {"TotalCount": 1, "CertificateOrderList": [{"CertificateId": 9, **row}]},
                    {"NotAfter": int((NOW + timedelta(days=1)).timestamp()),
                     "Cert": "private-certificate-material", "Key": "private-key-material", **detail},
                ])
                metric = collector.collect("certificates", {"statuses": [None]}, NOW)[0]
                self.assertEqual(metric.resource_name, expected)
                self.assertEqual(metric.resource_id, "9")
                self.assertEqual(metric.value, 1)
                self.assertEqual(len(collector.calls), 2)
                self.assertTrue(collector.calls[1][3]["CertFilter"])
                self.assertNotIn("private-", json.dumps(metric.to_dict()))

    def test_certificate_name_and_domain_changes_preserve_alert_identity(self):
        collected = []
        for domain in ("old.example.com", "renewed.example.com"):
            collector = FakeCollector([
                {"TotalCount": 1, "CertificateOrderList": [{"CertificateId": 9, "Name": "cert-auto"}]},
                {"NotAfter": int((NOW + timedelta(days=1)).timestamp()), "Common": domain},
            ])
            collected.append(collector.collect("certificates", {"statuses": [None]}, NOW)[0])
        self.assertEqual(collected[0].dimensions["domain"], "old.example.com")
        self.assertEqual(collected[1].dimensions["domain"], "renewed.example.com")
        self.assertEqual(collected[0].key(), collected[1].key())

    def test_package_converts_total_and_remaining_units(self):
        collector = FakeCollector([package_response()])
        metrics = collector.collect("packages", {"product_code": "oss"}, NOW)
        values = {m.metric: m.value for m in metrics}
        self.assertEqual(values["expiry_days"], 7)
        self.assertAlmostEqual(values["package_used_percent"], 90)
        self.assertAlmostEqual(values["package_remaining_percent"], 10)

    def test_package_empty_is_zero_remaining(self):
        collector = FakeCollector([package_response(RemainingAmount="0")])
        values = {m.metric: m.value for m in collector.collect("packages", {"product_code": "dysms"}, NOW)}
        self.assertEqual(values["package_used_percent"], 100)
        self.assertEqual(values["package_remaining_percent"], 0)

    def test_package_incompatible_units_are_error(self):
        collector = FakeCollector([package_response(RemainingAmountUnit="requests")])
        with self.assertRaisesRegex(ValueError, "incompatible"):
            collector.collect("packages", {"product_code": "oss"}, NOW)

    def test_package_missing_amount_is_error(self):
        collector = FakeCollector([package_response(RemainingAmount=None)])
        with self.assertRaisesRegex(ValueError, "RemainingAmount"):
            collector.collect("packages", {"product_code": "oss"}, NOW)

    def test_slb_uses_billing_expiry_field_and_filter(self):
        collector = FakeCollector([{"Data": {"TotalCount": 1, "InstanceList": [
            {"InstanceID": "lb-1", "EndTime": "2026-09-15T04:00:00Z", "SubscriptionType": "Subscription"}]}}])
        self.assertEqual(collector.collect("slb_expiry", {}, NOW)[0].value, 7)
        self.assertEqual(collector.calls[0][1], "QueryAvailableInstances")
        self.assertEqual(collector.calls[0][3]["SubscriptionType"], "Subscription")

    def test_balance_accepts_thousands_separator(self):
        collector = FakeCollector([{"Data": {"AvailableAmount": "1,002.50", "Currency": "CNY"}}])
        self.assertEqual(collector.collect("balance", {}, NOW)[0].value, 1002.5)

    def test_balance_rejects_wrong_currency(self):
        collector = FakeCollector([{"Data": {"AvailableAmount": "1,002.50", "Currency": "USD"}}])
        with self.assertRaisesRegex(ValueError, "currency"):
            collector.collect("balance", {}, NOW)

    def test_metric_uses_actual_observation_time_and_limit(self):
        collector = FakeCollector([metric_response()])
        metric = collector.collect("metrics", metric_options(multiplier=2), NOW)[0]
        self.assertEqual(metric.value, 160)
        self.assertEqual(metric.observed_at, NOW - timedelta(seconds=30))
        self.assertEqual(metric.dimensions["metric_name"], "InternetSend")
        self.assertEqual(collector.calls[0][3]["Dimensions"], [{"BucketName": "bucket-1"}])

    def test_metric_latest_point_wins_across_pages(self):
        first = metric_response(age=90, Average=5)
        first["NextToken"] = "next"
        collector = FakeCollector([first, metric_response(age=30, Average=80)])
        self.assertEqual(collector.collect("metrics", metric_options(), NOW)[0].value, 80)
        self.assertEqual(collector.calls[1][3]["NextToken"], "next")

    def test_listener_dimensions_are_part_of_stable_identity(self):
        entries = [metric_options(namespace="acs_slb_dashboard", metric_name="TrafficRXNew",
                                  resource_id="lb-1", dimensions={"instanceId": "lb-1", "port": port})["metrics"][0]
                   for port in ("80", "443")]
        collector = FakeCollector([metric_response(), metric_response(), metric_response(Average=95)])
        metrics = collector.collect("metrics", {"metrics": entries}, NOW)
        repeated = collector.collect("metrics", {"metrics": entries[:1]}, NOW)[0]
        self.assertNotEqual(metrics[0].key(), metrics[1].key())
        self.assertEqual(metrics[0].key(), repeated.key())

    def test_boolean_limit_is_not_a_numeric_budget(self):
        collector = FakeCollector([])
        with self.assertRaisesRegex(ValueError, "Invalid numeric limit"):
            collector.collect("metrics", metric_options(limit=True), NOW)

    def test_stale_metric_is_not_a_fresh_measurement(self):
        collector = FakeCollector([metric_response(age=301)])
        with self.assertRaisesRegex(ValueError, "Stale"):
            collector.collect("metrics", metric_options(), NOW)

    def test_missing_metric_is_error_not_zero(self):
        collector = FakeCollector([{"Datapoints": "[]"}])
        with self.assertRaisesRegex(ValueError, "No CloudMonitor"):
            collector.collect("metrics", metric_options(), NOW)

    def test_metric_requires_budget(self):
        collector = FakeCollector([])
        with self.assertRaisesRegex(ValueError, "limit"):
            collector.collect("metrics", metric_options(limit=0), NOW)

    def test_qps_ratio_is_not_percent_growth(self):
        collector = FakeCollector([metric_response(Average=200)])
        self.assertEqual(collector.collect("metrics", metric_options(output_metric="qps_ratio"), NOW)[0].value, 2)

    def test_sms_compares_complete_day_with_seven_previous_days(self):
        collector = FakeCollector([sms_response()])
        metric = collector.collect("sms_statistics", {}, NOW)[0]
        self.assertEqual(metric.value, 200)
        self.assertEqual(metric.dimensions["statistics_date"], "2026-09-07")
        self.assertEqual(collector.calls[0][3]["StartDate"], "20260831")
        self.assertEqual(collector.calls[0][3]["EndDate"], "20260907")
        self.assertEqual(metric.metric, "sms_growth_percent")

    def test_sms_missing_date_is_not_assumed_zero(self):
        response = sms_response()
        response["Data"]["TargetList"].pop()
        response["Data"]["TotalSize"] = 7
        collector = FakeCollector([response])
        with self.assertRaisesRegex(ValueError, "missing dates") as caught:
            collector.collect("sms_statistics", {}, NOW)
        self.assertEqual(type(caught.exception).__name__, "SmsStatisticsInsufficientData")

    def test_sms_zero_baseline_is_not_fabricated(self):
        collector = FakeCollector([sms_response(baseline=0)])
        with self.assertRaisesRegex(ValueError, "undefined") as caught:
            collector.collect("sms_statistics", {}, NOW)
        self.assertEqual(type(caught.exception).__name__, "SmsStatisticsInsufficientData")

    def test_sms_no_activity_has_zero_growth(self):
        collector = FakeCollector([sms_response(current=0, baseline=0)])
        self.assertEqual(collector.collect("sms_statistics", {}, NOW)[0].value, 0)

    def test_sms_empty_successful_history_has_no_growth_sample(self):
        collector = FakeCollector([{"Data": {"TargetList": [], "TotalSize": 0}}])
        self.assertEqual(collector.collect("sms_statistics", {}, NOW), [])

    def test_sms_empty_incomplete_page_is_still_an_error(self):
        collector = FakeCollector([{"Data": {"TargetList": [], "TotalSize": 8}}])
        with self.assertRaises(ValueError):
            collector.collect("sms_statistics", {}, NOW)


@unittest.skipUnless(importlib.util.find_spec("aliyunsdkcore"), "Alibaba Cloud SDK is not installed")
class AliyunRpcTests(unittest.TestCase):
    def make_collector(self, response):
        class Client:
            request = None

            def do_action_with_exception(self, request):
                self.request = request
                return json.dumps(response).encode()

        collector = AliyunCollector(ACCOUNT)
        client = Client()
        collector._clients["cn-hangzhou"] = client
        return collector, client

    def test_sdk_request_uses_https_official_billing_endpoint(self):
        collector, client = self.make_collector({"Success": True, "Code": "Success", "Data": {}})
        collector._rpc("bssopenapi", "QueryAccountBalance", "2017-12-14", {})
        self.assertEqual(client.request.get_domain(), "business.aliyuncs.com")
        self.assertEqual(client.request.get_protocol_type(), "https")
        self.assertEqual(client.request.get_method(), "POST")
        self.assertEqual(client.request.get_action_name(), "QueryAccountBalance")

    def test_rpc_accepts_shared_timeout_configuration(self):
        for options, expected in (({"timeout_seconds": 12}, 12),
                                  ({"timeout_seconds": 12, "request_timeout_seconds": 9}, 9)):
            with self.subTest(options=options), patch("aliyunsdkcore.client.AcsClient") as client:
                client.return_value.do_action_with_exception.return_value = b'{"Success": true}'
                collector = AliyunCollector({**ACCOUNT, "access_key_id": "test-id",
                                             "access_key_secret": "test-secret", **options})
                collector._rpc("bssopenapi", "QueryAccountBalance", "2017-12-14", {})
                self.assertEqual(client.call_args.kwargs["timeout"], expected)

    def test_sdk_request_serializes_certificate_filter_as_json_boolean(self):
        collector, client = self.make_collector({"NotAfter": 123})
        collector._rpc("cas", "GetUserCertificateDetail", "2020-04-07", {"CertId": 9, "CertFilter": True})
        self.assertEqual(client.request.get_query_params()["CertFilter"], "true")

    def test_business_error_is_not_successful_collection(self):
        collector, _ = self.make_collector({"Success": False, "Code": "Forbidden", "Message": "hidden"})
        with self.assertRaisesRegex(RuntimeError, "Forbidden") as error:
            collector._rpc("bssopenapi", "QueryAccountBalance", "2017-12-14", {})
        self.assertNotIn("hidden", str(error.exception))

    def test_malformed_success_flag_is_error(self):
        collector, _ = self.make_collector({"Success": "false", "Data": {}})
        with self.assertRaisesRegex(ValueError, "invalid Success"):
            collector._rpc("bssopenapi", "QueryAccountBalance", "2017-12-14", {})

    def test_cloudmonitor_numeric_success_code_is_accepted(self):
        collector, client = self.make_collector({"Success": True, "Code": 200, "Datapoints": "[]"})
        collector._rpc("cms", "DescribeMetricList", "2019-01-01", {"Dimensions": [{"instanceId": "i-1"}]})
        self.assertEqual(client.request.get_domain(), "metrics.cn-hangzhou.aliyuncs.com")
        self.assertEqual(json.loads(client.request.get_query_params()["Dimensions"]), [{"instanceId": "i-1"}])


if __name__ == "__main__":
    unittest.main()
