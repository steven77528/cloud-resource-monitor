from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest
import yaml

from cloud_monitor import cli, diagnostics
from cloud_monitor.config import validate_config
from cloud_monitor.diagnostics import diagnose_jobs, safe_collection_error
from cloud_monitor.models import Metric
from cloud_monitor.providers.aliyun import AliyunCollector
from cloud_monitor.providers.common import ProviderError
from cloud_monitor.providers.huawei import HuaweiCollectionError
from cloud_monitor.providers.wechat import WechatAPIError
from cloud_monitor.service import Service


NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


@pytest.fixture
def config():
    return validate_config({
        "version": 1,
        "service": {"database": {}},
        "notifications": {"gateway": {"url": "http://notification-gateway:8080", "token": ""}},
        "accounts": [
            {"id": "primary", "provider": "aliyun", "enabled": True,
             "access_key_id": "offline-example-key", "access_key_secret": "offline-example-secret"},
            {"id": "disabled-account", "provider": "aliyun", "enabled": False},
        ],
        "jobs": [
            {"id": "balance", "account": "primary", "kind": "balance", "interval_seconds": 3600},
            {"id": "empty", "account": "primary", "kind": "certificates", "interval_seconds": 3600},
            {"id": "receipts", "account": "primary", "kind": "sms_receipts", "interval_seconds": 3600},
            {"id": "disabled", "account": "primary", "kind": "balance", "enabled": False,
             "interval_seconds": 3600},
            {"id": "account-disabled-job", "account": "disabled-account", "kind": "balance",
             "interval_seconds": 3600},
        ],
    })


@pytest.mark.parametrize("message", [
    "Huawei bss.list_pay_per_use_customer_resources failed (status=unknown, code=KeyError)",
    "Huawei scm.list_certificates failed (status=401, code=SCM.4002)",
    "Huawei ces.show_metric_data failed (status=403, code=CES.0015)",
    "Huawei bss endpoint region cn-east-3 is unsupported; set account.bss_region to cn-north-1",
    "Configured subscription IDs absent from BSS response; check billing mode and region",
    "Response has no valid datapoints list",
])
def test_known_huawei_details_are_preserved(message):
    assert safe_collection_error(HuaweiCollectionError(message)) == f"HuaweiCollectionError: {message}"


@pytest.mark.parametrize("error", [
    ValueError("Authorization: secret-token https://user:password@example.test?signature=signed-secret"),
    RuntimeError("Authorization: secret-token response={access_key_secret:signed-secret}"),
    HuaweiCollectionError("Huawei scm.list_certificates failed (status=401, code=secret-token)"),
    HuaweiCollectionError("Huawei scm.list_certificates failed (status=401, code=SCM.4002) secret-token"),
    HuaweiCollectionError("Huawei scm.list_certificates failed (status=401, code=SCM.4002\nsecret-token)"),
    HuaweiCollectionError("Huawei scm.secret_token failed (status=401, code=SCM.4002)"),
    HuaweiCollectionError("Huawei scm.list_certificates failed (status=secret-token, code=SCM.4002)"),
    HuaweiCollectionError("Huawei bss endpoint region secret-token is unsupported; set account.bss_region to cn-north-1"),
])
def test_untrusted_details_never_escape(error):
    assert safe_collection_error(error) == type(error).__name__


@pytest.mark.parametrize("code", [40001, 76022, -1, 0, -1_000_000_000, 1_000_000_000])
def test_wechat_numeric_error_code_survives_without_raw_provider_message(code):
    error = WechatAPIError(code)
    error.args = ("Authorization: secret-token https://api.example.test?access_token=signed-secret",)
    detail = safe_collection_error(error)
    assert detail == f"ProviderError: wechat_api_error field=response.errcode code={code}"
    assert "secret" not in detail
    assert "https" not in detail


@pytest.mark.parametrize("code", [True, False, 40001.0, "40001", "signed-secret", None,
                                   -1_000_000_001, 1_000_000_001])
def test_wechat_invalid_error_code_is_not_exposed(code):
    error = WechatAPIError(40001)
    error.api_code = code
    error.args = ("signed-secret raw SDK response",)
    assert safe_collection_error(error) == "ProviderError: wechat_api_error field=response.errcode"


def test_numeric_error_code_is_only_exposed_for_exact_wechat_error_type():
    class OtherWechatError(WechatAPIError):
        pass

    unrelated = ProviderError("api_business_error")
    unrelated.api_code = 40001
    assert safe_collection_error(unrelated) == "ProviderError: api_business_error"
    assert safe_collection_error(OtherWechatError(40001)) == "ProviderError: wechat_api_error field=response.errcode"


@pytest.mark.parametrize("error,expected", [
    (HuaweiCollectionError("CES has no datapoints for secret-token/private-resource"), "CES has no datapoints"),
    (HuaweiCollectionError("Missing time field: secret-token.expire_time"), "Missing time field"),
    (HuaweiCollectionError("Invalid numeric field: secret-token.amount"), "Invalid numeric field"),
    (ValueError("No CloudMonitor data for secret-token/private-resource"), "No CloudMonitor data"),
])
def test_resource_identifiers_and_response_derived_labels_are_removed(error, expected):
    result = safe_collection_error(error)
    assert expected in result
    assert "secret-token" not in result
    assert "private-resource" not in result


def test_known_local_sms_and_endpoint_validation_reasons_are_preserved():
    for message in (
        "SMS seven-day baseline is zero; percentage growth is undefined",
        "SMS daily statistics missing dates: 2026-09-01, 2026-09-02",
        "Huawei bss endpoint region is unsupported; set account.bss_region to cn-north-1 for mainland China",
    ):
        assert safe_collection_error(ValueError(message)) == f"ValueError: {message}"


def test_report_contains_counts_only_and_skips_disabled_and_database_jobs(config):
    collector = Mock()
    collector.collect.side_effect = [
        [Metric("aliyun", "primary", "private-resource", "private-name", "balance", 314159, "CNY", NOW)], []]
    factory = Mock(return_value=collector)
    reports = list(diagnose_jobs(config, collector_factory=factory, now=NOW))
    assert [(r["job"], r["status"], r["samples"]) for r in reports] == [
        ("balance", "ok", 1), ("empty", "no_data", 0), ("receipts", "no_data", 0)]
    assert reports[2]["skipped"] is True
    assert collector.collect.call_count == 2
    serialized = json.dumps(reports)
    assert "private-resource" not in serialized and "private-name" not in serialized
    assert "314159" not in serialized and "offline-example-secret" not in serialized


def test_report_continues_after_collection_error(config):
    collector = Mock()
    collector.collect.side_effect = [ValueError("secret-token response"), []]
    reports = list(diagnose_jobs(config, ["balance", "empty"], lambda account: collector, NOW))
    assert reports == [
        {"job": "balance", "account": "primary", "status": "error", "samples": 0,
         "error_type": "ValueError", "detail": "ValueError"},
        {"job": "empty", "account": "primary", "status": "no_data", "samples": 0},
    ]


@pytest.mark.parametrize("samples", [0, 2])
def test_network_unavailable_estimates_are_degraded_with_safe_counts(config, samples):
    config["jobs"][0].update(kind="network_traffic", options={"product": "ecs", "region": "cn-hangzhou"})
    collector = Mock(network_partial_failures=0, network_estimate_unavailable=[{
        "resource_id": "private-resource", "region": "private-region", "observed_at": NOW,
        "reason": "negative_difference"}])
    collector.collect.return_value = [
        Metric("aliyun", "primary", "valid-resource", "valid-name", "network_in_bps", 314159,
               "bit/s", NOW) for _ in range(samples)]

    report, = diagnose_jobs(config, ["balance"], lambda account: collector, NOW)

    assert report == {"job": "balance", "account": "primary", "status": "degraded", "samples": samples,
                      "unavailable_resources": 1,
                      "detail": "Network estimate unavailable; unavailable_resources=1"}
    serialized = json.dumps(report)
    assert "private-resource" not in serialized and "valid-resource" not in serialized
    assert "private-region" not in serialized and "314159" not in serialized


def test_network_partial_failure_takes_priority_over_unavailable_estimates(config):
    config["jobs"][0].update(kind="network_traffic", options={"product": "ecs", "region": "cn-hangzhou"})
    collector = Mock(network_partial_failures=2, network_estimate_unavailable=[{"resource_id": "private"}])
    collector.collect.return_value = [Metric("aliyun", "primary", "valid", "valid", "network_in_bps",
                                            1, "bit/s", NOW)]

    report, = diagnose_jobs(config, ["balance"], lambda account: collector, NOW)

    assert report["status"] == "error" and report["error_type"] == "NetworkPartialFailure"
    assert report["samples"] == 1 and report["unavailable_resources"] == 1
    assert report["detail"] == "Network collection partially failed; failed_resources=2"


def test_network_exception_takes_priority_over_unavailable_estimates(config):
    config["jobs"][0].update(kind="network_traffic", options={"product": "ecs", "region": "cn-hangzhou"})
    collector = Mock(network_partial_failures=1, network_estimate_unavailable=[{"resource_id": "private"}])
    collector.collect.side_effect = RuntimeError("signed-secret raw provider response")

    report, = diagnose_jobs(config, ["balance"], lambda account: collector, NOW)

    assert report == {"job": "balance", "account": "primary", "status": "error", "samples": 0,
                      "error_type": "RuntimeError", "detail": "RuntimeError", "unavailable_resources": 1}


def test_non_network_diagnostics_do_not_interpret_network_quality_attributes(config):
    collector = Mock(network_partial_failures=0, network_estimate_unavailable=[{"resource_id": "private"}])
    collector.collect.return_value = []
    report, = diagnose_jobs(config, ["balance"], lambda account: collector, NOW)
    assert report == {"job": "balance", "account": "primary", "status": "no_data", "samples": 0}


def test_cli_degraded_network_diagnosis_has_success_exit_without_database_or_notifications(
    config, tmp_path, monkeypatch, capsys,
):
    config["jobs"][0].update(kind="network_traffic", options={"product": "ecs", "region": "cn-hangzhou"})
    path = tmp_path / "offline.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    collector = Mock(network_partial_failures=0, network_estimate_unavailable=[{"resource_id": "private"}])
    collector.collect.return_value = []
    monkeypatch.setattr(diagnostics, "_collector", lambda account: collector)
    forbidden = Mock(side_effect=AssertionError("diagnose must not initialize service or storage"))
    monkeypatch.setattr(cli, "Store", forbidden)
    monkeypatch.setattr(cli, "Service", forbidden)
    monkeypatch.setattr(cli, "validate_database", forbidden)

    assert cli.main(["--config", str(path), "diagnose", "--job", "balance"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "degraded" and report["unavailable_resources"] == 1
    forbidden.assert_not_called()


def sms_collector(account, zero_baseline=False):
    end = NOW.date() - timedelta(days=1)
    rows = [{"SendDate": (end - timedelta(days=offset)).strftime("%Y%m%d"),
             "TotalCount": 2 if offset == 0 else 0} for offset in range(8)]
    if not zero_baseline:
        rows = rows[3:4]
    collector = AliyunCollector(account)
    collector._rpc = Mock(return_value={"Data": {"TargetList": rows, "TotalSize": len(rows)}})
    return collector


@pytest.mark.parametrize("zero_baseline,reason", [(False, "missing dates"), (True, "undefined")])
def test_sms_statistics_insufficiency_is_reported_without_a_collection_error(config, zero_baseline, reason):
    config["jobs"][0]["kind"] = "sms_statistics"
    reports = list(diagnose_jobs(config, ["balance"],
                                lambda account: sms_collector(account, zero_baseline), NOW))
    assert len(reports) == 1
    assert reports[0]["status"] == "insufficient_data"
    assert reports[0]["samples"] == 0
    assert reason in reports[0]["detail"]
    assert "error_type" not in reports[0]
    assert "offline-example-secret" not in json.dumps(reports)


def test_sms_insufficient_detail_still_rejects_untrusted_messages():
    from cloud_monitor.providers.aliyun import SmsStatisticsInsufficientData

    error = SmsStatisticsInsufficientData("Authorization: signed-secret raw SDK response")
    assert safe_collection_error(error) == "SmsStatisticsInsufficientData"


@pytest.mark.parametrize("job_id", ["missing", "disabled", "account-disabled-job"])
def test_invalid_later_selection_prevents_all_cloud_requests(config, job_id):
    factory = Mock()
    with pytest.raises(ValueError):
        list(diagnose_jobs(config, ["balance", job_id], factory))
    factory.assert_not_called()


def test_invalid_later_parameters_prevent_all_cloud_requests(config):
    config["jobs"][1].update(kind="packages", options={})
    factory = Mock()
    with pytest.raises(ValueError, match="product_code"):
        list(diagnose_jobs(config, ["balance", "empty"], factory))
    factory.assert_not_called()


def test_selected_jobs_only_and_duplicate_ids_are_collected_once(config):
    config["jobs"][1].update(kind="packages", options={})
    factory = Mock()
    factory.return_value.collect.return_value = []
    assert len(list(diagnose_jobs(config, ["balance", "balance"], factory))) == 1
    factory.assert_called_once()


def test_cli_diagnose_needs_no_database_or_notification_credentials(config, tmp_path, monkeypatch, capsys):
    path = tmp_path / "offline.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    forbidden = Mock(side_effect=AssertionError("diagnose must not initialize service or storage"))
    monkeypatch.setattr(cli, "Store", forbidden)
    monkeypatch.setattr(cli, "Service", forbidden)
    monkeypatch.setattr(cli, "validate_database", forbidden)
    factory = Mock()
    factory.return_value.collect.return_value = []
    monkeypatch.setattr(diagnostics, "_collector", factory)
    assert cli.main(["--config", str(path), "diagnose", "--job", "empty", "--job", "balance"]) == 0
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["job"] for r in reports] == ["empty", "balance"]
    forbidden.assert_not_called()


def test_cli_collection_error_has_failure_exit_and_safe_json(config, tmp_path, monkeypatch, capsys):
    path = tmp_path / "offline.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    factory = Mock()
    factory.return_value.collect.side_effect = RuntimeError("signed-secret raw SDK response")
    monkeypatch.setattr(diagnostics, "_collector", factory)
    assert cli.main(["--config", str(path), "diagnose", "--job", "balance"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["detail"] == "RuntimeError"
    assert "signed-secret" not in captured.out + captured.err


def test_cli_statistics_insufficiency_is_not_a_failed_cloud_request(config, tmp_path, monkeypatch, capsys):
    config["jobs"][0]["kind"] = "sms_statistics"
    path = tmp_path / "offline.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(diagnostics, "_collector", sms_collector)
    monkeypatch.setattr(diagnostics, "utcnow", lambda: NOW)
    forbidden = Mock(side_effect=AssertionError("diagnose must not initialize service or storage"))
    monkeypatch.setattr(cli, "Store", forbidden)
    monkeypatch.setattr(cli, "Service", forbidden)
    assert cli.main(["--config", str(path), "diagnose", "--job", "balance"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "insufficient_data" and "missing dates" in report["detail"]
    forbidden.assert_not_called()


def test_cli_invalid_job_is_configuration_error_before_any_request(config, tmp_path, monkeypatch, capsys):
    path = tmp_path / "offline.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    factory = Mock()
    monkeypatch.setattr(diagnostics, "_collector", factory)
    assert cli.main(["--config", str(path), "diagnose", "--job", "balance", "--job", "unknown"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Unknown diagnostic job" in captured.err
    factory.assert_not_called()


@pytest.mark.parametrize("error", [
    HuaweiCollectionError("Huawei scm.list_certificates failed (status=401, code=SCM.4002)"),
    ValueError("signed-secret raw SDK response"),
])
def test_service_persists_and_logs_identical_safe_detail(config, caplog, error):
    service = object.__new__(Service)
    service.accounts = {account["id"]: account for account in config["accounts"]}
    service.store = Mock()
    service.engine = Mock()
    service.collector_factory = Mock()
    service.collector_factory.return_value.collect.side_effect = error
    job = deepcopy(config["jobs"][0])
    assert service.collect(job, NOW) is False
    expected = safe_collection_error(error)
    service.store.job_status.assert_called_once_with(job["id"], "error", NOW, detail=expected)
    assert f"detail={expected}" in caplog.text
    assert "signed-secret" not in caplog.text
    service.store.save_samples.assert_not_called()
    assert service.engine.evaluate.call_args.args[1].metric == "collection_failed"
    assert service.engine.evaluate.call_args.args[1].value == 1
