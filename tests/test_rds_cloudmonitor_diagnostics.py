from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

from aliyunsdkcore.acs_exception.exceptions import ClientException, ServerException
import pytest

from cloud_monitor.config import validate_config
from cloud_monitor.diagnostics import diagnose_jobs, safe_collection_error
from cloud_monitor.providers.aliyun import AliyunCollector
from cloud_monitor.providers.rds_cloudmonitor import (
    NAMESPACE,
    RdsCloudMonitorCollector,
    RdsCloudMonitorError,
)
from cloud_monitor.service import Service
from cloud_monitor.store import Store


NOW = datetime(2026, 10, 12, 17, 49, 32, tzinfo=timezone.utc)
BASE_MESSAGE = "RDS CloudMonitor request failed; check cloud permissions and connectivity"
RAW_SECRET = "Authorization: diagnostic-secret https://user:password@example.test?signature=signed-secret"
OPTIONS = {"instances": [{"resource_id": "rm-test", "engine": "MySQL", "region": "cn-hangzhou"}]}
ACTIONS = ("DescribeMetricMetaList", "DescribeMetricList")


class FakeProvider(AliyunCollector):
    def __init__(self, *responses):
        super().__init__({"id": "primary"})
        self.responses = list(responses)

    def _rpc(self, service, action, version, params, region=None):
        assert service == "cms"
        if not self.responses:
            raise AssertionError("Unexpected cloud request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def metadata():
    return {"Resources": {"Resource": [{
        "Namespace": NAMESPACE, "MetricName": "MySQL_SlowQueries", "Unit": "count/s",
        "Dimensions": "userId,instanceId", "Periods": "60", "Statistics": "Average",
    }]}, "TotalCount": "1", "Success": True, "Code": "200"}


def response(now):
    return {"Datapoints": [{"instanceId": "rm-test", "Average": 0,
                            "timestamp": int(now.timestamp() * 1000)}], "Success": True, "Code": "200"}


def collector_error(error, action):
    provider = FakeProvider(*([metadata(), error] if action == "DescribeMetricList" else [error]))
    with pytest.raises(RdsCloudMonitorError) as caught:
        RdsCloudMonitorCollector(provider).collect(deepcopy(OPTIONS), NOW)
    assert caught.value.code == "cloud_request_failed"
    if isinstance(error, Exception):
        assert caught.value.__suppress_context__
    return caught.value


def assert_safe(detail):
    for value in ("diagnostic-secret", "signed-secret", "Authorization", "user:password", "example.test",
                  "secret-request-id", "secret-class-name"):
        assert value not in detail


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("error,error_type,api_code,http_status", [
    (ClientException("SDK.HttpError", RAW_SECRET), "ClientException", "SDK.HttpError", None),
    (ServerException("Throttling.User", RAW_SECRET, 429, "secret-request-id"),
     "ServerException", "Throttling.User", 429),
    (ServerException("ServiceUnavailable", RAW_SECRET, 503, "secret-request-id"),
     "ServerException", "ServiceUnavailable", 503),
    (TimeoutError(RAW_SECRET), "TimeoutError", None, None),
    (RuntimeError(RAW_SECRET), "RuntimeError", None, None),
])
def test_request_failure_preserves_safe_diagnostics_for_each_cms_action(
    action, error, error_type, api_code, http_status,
):
    detail = safe_collection_error(collector_error(error, action))
    assert BASE_MESSAGE in detail
    assert f"action={action}" in detail
    assert f"error_type={error_type}" in detail
    if api_code is not None:
        assert f"api_code={api_code}" in detail
    if http_status is not None:
        assert f"http_status={http_status}" in detail
    assert_safe(detail)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("code", ["403", 403, "Throttling"])
def test_unsuccessful_response_has_safe_code_without_exposing_response_fields(action, code):
    failure = {"Success": False, "Code": code, "Message": RAW_SECRET, "RequestId": "secret-request-id"}
    detail = safe_collection_error(collector_error(failure, action))
    assert BASE_MESSAGE in detail
    assert f"action={action}" in detail
    assert "error_type=APIResponseError" in detail
    assert f"api_code={code}" in detail
    assert "http_status=" not in detail
    assert_safe(detail)


@pytest.mark.parametrize("action", ACTIONS)
def test_builtin_runtime_failure_from_aliyun_preserves_allowlisted_code(action):
    error = RuntimeError(f"Alibaba Cloud cms.{action} failed: code=Throttling")
    detail = safe_collection_error(collector_error(error, action))
    assert f"action={action}" in detail
    assert "error_type=RuntimeError" in detail
    assert "api_code=Throttling" in detail


@pytest.mark.parametrize("message", [
    "Alibaba Cloud cms.DescribeMetricList failed: code=diagnostic-secret",
    "Alibaba Cloud cms.DescribeMetricList failed: code=Throttling diagnostic-secret",
    "Alibaba Cloud cms.DescribeMetricList failed: code=Throttling\ndiagnostic-secret",
    "Alibaba Cloud cms.DescribeMetricMetaList failed: code=Throttling",
    "Alibaba Cloud cms.diagnostic-secret failed: code=Throttling",
    "Alibaba Cloud rds.DescribeMetricList failed: code=Throttling",
])
def test_runtime_message_only_accepts_exact_known_service_action_and_code(message):
    detail = safe_collection_error(collector_error(RuntimeError(message), "DescribeMetricList"))
    assert "error_type=RuntimeError" in detail
    assert "api_code=" not in detail
    assert_safe(detail)


@pytest.mark.parametrize("value", ["diagnostic-secret", "Throttling\ndiagnostic-secret", {}, [], True, None])
def test_unknown_sdk_error_codes_never_escape(value):
    detail = safe_collection_error(collector_error(
        ServerException(value, RAW_SECRET, 429, "secret-request-id"), "DescribeMetricList"))
    assert "error_type=ServerException" in detail
    assert "api_code=" not in detail
    assert "http_status=429" in detail
    assert_safe(detail)


@pytest.mark.parametrize("status", ["diagnostic-secret", "429\ndiagnostic-secret", {}, [], True, 99, 600, 429.5])
def test_invalid_sdk_http_status_never_escapes(status):
    detail = safe_collection_error(collector_error(
        ServerException("Throttling.User", RAW_SECRET, status, "secret-request-id"), "DescribeMetricList"))
    assert "api_code=Throttling.User" in detail
    assert "http_status=" not in detail
    assert_safe(detail)


def test_untrusted_exception_class_names_and_sdk_subclasses_are_not_diagnostics():
    unknown = type("secret-class-name", (Exception,), {})
    sdk_subclass = type("secret-class-name", (ServerException,), {})
    for error in (unknown(RAW_SECRET), sdk_subclass("Throttling", RAW_SECRET, 429)):
        detail = safe_collection_error(collector_error(error, "DescribeMetricList"))
        assert BASE_MESSAGE in detail
        assert "action=DescribeMetricList" in detail
        assert "api_code=" not in detail
        assert "http_status=" not in detail
        assert_safe(detail)


@pytest.mark.parametrize("field", ["action", "error_type", "api_code", "http_status"])
@pytest.mark.parametrize("value", ["diagnostic-secret", "DescribeMetricList\ndiagnostic-secret", {}, [], True])
def test_direct_error_diagnostics_are_validated_at_construction(field, value):
    error = RdsCloudMonitorError("cloud_request_failed", **{field: value})
    detail = safe_collection_error(error)
    assert BASE_MESSAGE in detail
    assert f"{field}=" not in detail
    assert_safe(detail)


def test_error_without_diagnostics_retains_existing_fixed_message():
    assert str(RdsCloudMonitorError("cloud_request_failed")) == BASE_MESSAGE
    assert "no data" in safe_collection_error(RdsCloudMonitorError("no_data"))


def config():
    return validate_config({
        "version": 1,
        "accounts": [{"id": "primary", "provider": "aliyun", "enabled": True,
                      "access_key_id": "offline-key", "access_key_secret": "offline-secret"}],
        "jobs": [{"id": "rds-cms", "account": "primary", "kind": "rds_cloudmonitor",
                  "interval_seconds": 60, "options": deepcopy(OPTIONS)}],
        "notifications": {"sms": {"enabled": False}},
    })


def test_real_cloudmonitor_failure_survives_diagnostic_report_without_credentials():
    cfg = config()
    provider = FakeProvider(metadata(), ClientException("SDK.HttpError", RAW_SECRET))
    reports = list(diagnose_jobs(cfg, collector_factory=lambda _: provider, now=NOW))
    assert len(reports) == 1
    assert reports[0]["status"] == "error"
    assert reports[0]["error_type"] == "RdsCloudMonitorError"
    assert "action=DescribeMetricList" in reports[0]["detail"]
    assert "api_code=SDK.HttpError" in reports[0]["detail"]
    serialized = json.dumps(reports)
    assert "offline-secret" not in serialized
    assert_safe(serialized)


def test_service_logs_safe_failure_and_recovers_only_after_two_successes(tmp_path, caplog, monkeypatch):
    cfg = config()
    job = cfg["jobs"][0]
    first_success = NOW + timedelta(seconds=60)
    second_success = NOW + timedelta(seconds=120)
    provider = FakeProvider(metadata(), TimeoutError(RAW_SECRET), response(first_success), response(second_success))
    store = Store(tmp_path / "state.db")
    status = Mock(wraps=store.job_status)
    monkeypatch.setattr(store, "job_status", status)
    notifier = Mock()
    service = Service(cfg, store, collector_factory=lambda _: provider, notifier=notifier)

    assert service.collect(job, NOW) is False
    failure_detail = status.call_args.kwargs["detail"]
    assert "action=DescribeMetricList" in failure_detail
    assert "error_type=TimeoutError" in failure_detail
    assert f"detail={failure_detail}" in caplog.text
    assert_safe(caplog.text)
    assert [json.loads(row["payload"])["phase"] for row in store.list_messages()] == ["firing"]

    assert service.collect(job, first_success) is True
    assert len(store.list_messages()) == 1
    assert service.collect(job, second_success) is True
    messages = [json.loads(row["payload"]) for row in store.list_messages()]
    assert [message["phase"] for message in messages] == ["firing", "recovery"]
    assert messages[1]["observed_at"] == second_success.isoformat()
    notifier.send.assert_not_called()
    assert service.started is False
