"""Offline contracts against official Qiniu response shapes and real SDK signing."""

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from cloud_monitor.providers.common import ProviderError
from cloud_monitor.providers.qiniu import QiniuCollector, validate_options


NOW = datetime(2026, 9, 11, 2, 17, tzinfo=timezone.utc)
ACCOUNT = {"id": "qiniu-test", "access_key_id": "offline-ak", "access_key_secret": "offline-sk"}


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert self.responses, "Unexpected extra API request"
        return deepcopy(self.responses.pop(0))


def balance(**overrides):
    return {"code": 0, "data": {
        "account_id": "finance-account-1", "currency": "CNY", "cash_balance": "12345678901",
        "present_balance": 0, "credit_line": 20000000000, "unpaid_money": "-12345678", **overrides,
    }}


def indexed(metrics):
    return {metric.metric: metric for metric in metrics}


def test_balance_uses_eight_decimal_money_and_actual_qiniu_sdk_signing():
    transport = Transport(balance())
    values = indexed(QiniuCollector(ACCOUNT, transport).collect("balance", {}, NOW))
    assert values["qiniu_cash_balance"].value == 123.45678901
    assert values["qiniu_unpaid_money"].value == -0.12345678
    assert values["qiniu_cash_balance"].dimensions["raw_value"] == "12345678901"
    assert "qiniu_available_balance" not in values
    method, url, options = transport.calls[0]
    assert method == "GET" and options["body"] is None
    signed = ("GET /billing-api/v1/account/balance-overview\nHost: api.qiniu.com\n"
              "Content-Type: application/x-www-form-urlencoded\nX-Qiniu-Date: 20260911T021700Z\n\n")
    digest = base64.urlsafe_b64encode(hmac.new(b"offline-sk", signed.encode(), hashlib.sha1).digest()).decode()
    assert options["headers"]["Authorization"] == "Qiniu offline-ak:" + digest


@pytest.mark.parametrize("options", [
    {"available_balance_field": "balance"}, {"available_balance_confirmed": True},
    {"available_balance_field": "unpaid_money", "available_balance_confirmed": True},
    {"available_balance_confirmed": "true"},
])
def test_available_balance_requires_explicit_field_confirmation(options):
    with pytest.raises(ProviderError):
        validate_options("balance", options)


@pytest.mark.parametrize("field", ["balance", "available_balance"])
def test_available_balance_never_falls_back_to_other_documented_field(field):
    options = {"available_balance_field": field, "available_balance_confirmed": True}
    other = "balance" if field == "available_balance" else "available_balance"
    with pytest.raises(ProviderError):
        QiniuCollector(ACCOUNT, Transport(balance(**{other: 1}))).collect("balance", options, NOW)
    values = indexed(QiniuCollector(ACCOUNT, Transport(balance(**{field: 500000000}))).collect(
        "balance", options, NOW))
    assert values["qiniu_available_balance"].value == 5


def test_shipped_balance_rules_alert_only_on_available_balance_and_keep_cash_samples(tmp_path):
    from pathlib import Path

    import yaml

    from cloud_monitor.config import validate_config
    from cloud_monitor.engine import Engine
    from cloud_monitor.store import Store

    root = Path(__file__).resolve().parents[1]
    integration = yaml.safe_load((root / "config/integrations/qiniu.yaml").read_text(encoding="utf-8"))
    job = next(job for job in integration["jobs"] if job["id"] == "qiniu-balance")
    cfg = validate_config({"version": 1, "accounts": [{"id": job["account"], "provider": "qiniu"}],
                           "jobs": [job], "rules": integration["rules"]})
    transport = Transport(balance(cash_balance=41708000000, available_balance=40774000000))
    account = {**ACCOUNT, "id": job["account"]}
    metrics = QiniuCollector(account, transport).collect("balance", job["options"]["collector"], NOW)
    store = Store(tmp_path / "state.db")
    try:
        engine = Engine(store, cfg)
        for metric in metrics:
            engine.evaluate(job, metric, NOW)
        store.save_samples(job["id"], metrics)
        alerts = store.status()["active_alerts"]
        assert len(alerts) == 1
        assert alerts[0]["rule_id"] == "qiniu-available-low"
        assert alerts[0]["active"] == "P1"
        assert alerts[0]["metric"]["value"] == 407.74
        assert len(store.list_messages()) == 1
        samples = {row["payload"]["metric"]: row["payload"]["value"]
                   for row in store.latest_samples([job["id"]])}
        assert samples["qiniu_cash_balance"] == 417.08
        assert samples["qiniu_available_balance"] == 407.74
        assert len(transport.calls) == 1
    finally:
        store.dispose()


@pytest.mark.parametrize("value", [None, True, "NaN", "Infinity", "0.1"])
def test_invalid_money_is_not_replaced_with_zero(value):
    with pytest.raises(ProviderError):
        QiniuCollector(ACCOUNT, Transport(balance(cash_balance=value))).collect("balance", {}, NOW)


def test_money_identity_includes_currency_and_ignores_untrusted_response_fields():
    cny = QiniuCollector(ACCOUNT, Transport(balance(email="DO-NOT-STORE", message="secret")))
    usd = QiniuCollector(ACCOUNT, Transport(balance(currency="USD")))
    first = cny.collect("balance", {}, NOW)[0]
    second = usd.collect("balance", {}, NOW)[0]
    assert first.key() != second.key()
    assert "DO-NOT-STORE" not in json.dumps(first.to_dict())


@pytest.mark.parametrize("now,cutoff,month,budget_metric", [
    (NOW, "2026-09-11T00:00:00", "2026-09", True),
    (datetime(2026, 9, 10, 23, tzinfo=timezone.utc), "2026-09-10T00:00:00", "2026-09", True),
    (datetime(2026, 10, 1, 1, tzinfo=timezone.utc), "2026-10-01T00:00:00", "2026-09", False),
    (datetime(2026, 10, 1, 23, tzinfo=timezone.utc), "2026-10-01T00:00:00", "2026-09", False),
])
def test_snapshot_cutoff_and_budget_follow_actual_billing_month(now, cutoff, month, budget_metric):
    transport = Transport({"code": 0, "data": {"currency": "CNY", "total_money": 85000000000}})
    values = indexed(QiniuCollector(ACCOUNT, transport).collect(
        "billing_snapshot", {"monthly_budget": 1000, "budget_currency": "CNY"}, now))
    assert parse_qs(urlsplit(transport.calls[0][1]).query)["date"] == [cutoff]
    assert values["qiniu_estimated_cost"].value == 850
    assert values["qiniu_estimated_cost"].dimensions["billing_period"] == month
    assert ("qiniu_budget_used_percent" in values) is budget_metric
    if budget_metric:
        assert values["qiniu_budget_used_percent"].value == 85


def test_first_day_before_eight_does_not_query_forbidden_previous_month():
    transport = Transport()
    with pytest.raises(ProviderError, match="source_not_ready"):
        QiniuCollector(ACCOUNT, transport).collect(
            "billing_snapshot", {}, datetime(2026, 9, 30, 23, tzinfo=timezone.utc))
    assert not transport.calls


def test_snapshot_budget_currency_cannot_mix():
    transport = Transport({"code": 0, "data": {"currency": "USD", "total_money": 100000000}})
    with pytest.raises(ProviderError, match="currency_mismatch"):
        QiniuCollector(ACCOUNT, transport).collect(
            "billing_snapshot", {"monthly_budget": 100, "budget_currency": "CNY"}, NOW)


def test_snapshot_month_rollover_replaces_current_gauges_and_recovers_budget_alert(tmp_path):
    from cloud_monitor.config import validate_config
    from cloud_monitor.engine import Engine
    from cloud_monitor.store import Store

    cfg = validate_config({"version": 1, "accounts": [{"id": "qiniu-test", "provider": "qiniu"}],
                           "jobs": [{"id": "snapshot", "account": "qiniu-test", "kind": "billing_snapshot",
                                     "interval_seconds": 86400, "max_age_seconds": 172800}],
                           "rules": [{"id": "budget", "metric": "qiniu_budget_used_percent",
                                      "levels": [{"severity": "P1", "op": "ge", "threshold": 80}]}]})
    store = Store(tmp_path / "state.db")
    try:
        engine = Engine(store, cfg)
        job = cfg["jobs"][0]
        snapshots = []
        for now, amount in ((datetime(2026, 9, 30, 2, tzinfo=timezone.utc), 90000000000),
                            (datetime(2026, 10, 2, 2, tzinfo=timezone.utc), 1000000000),
                            (datetime(2026, 10, 3, 2, tzinfo=timezone.utc), 2000000000)):
            response = {"code": 0, "data": {"currency": "CNY", "total_money": amount}}
            metrics = QiniuCollector(ACCOUNT, Transport(response)).collect(
                "billing_snapshot", {"monthly_budget": 1000, "budget_currency": "CNY"}, now)
            snapshots.append(indexed(metrics))
            for metric in metrics:
                engine.evaluate(job, metric, now)
            store.save_samples(job["id"], metrics)
        assert snapshots[0]["qiniu_estimated_cost"].dimensions["billing_period"] == "2026-09"
        assert snapshots[-1]["qiniu_estimated_cost"].dimensions["billing_period"] == "2026-10"
        for name in ("qiniu_estimated_cost", "qiniu_budget_used_percent"):
            assert len({snapshot[name].key() for snapshot in snapshots}) == 1
        latest = store.latest_samples([job["id"]])
        assert len(latest) == 2
        assert all(row["payload"]["dimensions"]["billing_period"] == "2026-10" for row in latest)
        assert not store.status()["active_alerts"]
        phases = [json.loads(message["payload"])["phase"] for message in store.list_messages()]
        assert "recovery" in phases
    finally:
        store.dispose()


def month_row(**overrides):
    return {"item_name": "cdn-flow", "zone_name": "china", "available_time": "all-day",
            "respack_unit": "GB", "month_remain": "100", "month_used": "900", "total_surplus": "1000",
            **overrides}


def test_month_packages_reads_all_pages_and_preserves_incompatible_dimensions():
    transport = Transport({"code": 0, "data": [month_row()]},
                          {"code": 0, "data": [month_row(respack_unit="TB")]},
                          {"code": 0, "data": []})
    metrics = QiniuCollector(ACCOUNT, transport).collect("resource_packages_month", {"page_size": 1}, NOW)
    assert len(transport.calls) == 3
    assert len(metrics) == 8
    remaining = [m for m in metrics if m.metric == "qiniu_package_remaining_percent"]
    assert [m.value for m in remaining] == [10, 10]
    assert remaining[0].key() != remaining[1].key()
    assert [m.resource_name for m in remaining] == ["cdn-flow / china / all-day / GB",
                                                   "cdn-flow / china / all-day / TB"]
    assert all(m.dimensions["source"] == "official_api" for m in metrics)
    assert remaining[0].dimensions["identity"] == {
        "item_name": "cdn-flow", "zone_name": "china", "available_time": "all-day", "respack_unit": "GB",
    }


def test_zero_total_has_no_percentage_and_big_integer_does_not_round_before_ratio():
    transport = Transport({"code": 0, "data": [month_row(total_surplus="0", month_used="0", month_remain="0"),
                                                month_row(zone_name="oversea", total_surplus="9007199254740993",
                                                          month_used="9007199254740992", month_remain="1")]})
    metrics = QiniuCollector(ACCOUNT, transport).collect("resource_packages_month", {}, NOW)
    ratios = [m for m in metrics if m.metric == "qiniu_package_remaining_percent"]
    assert len(ratios) == 1 and ratios[0].value > 0
    assert next(m for m in metrics if m.dimensions["zone_name"] == "oversea").dimensions[
        "raw_value"] == "9007199254740993"


@pytest.mark.parametrize("responses,options,reason", [
    ([{"code": 0, "data": [month_row()]}, {"code": 0, "data": [month_row()]}], {"page_size": 1},
     "pagination_repeated"),
    ([{"code": 0, "data": [month_row()]}], {"page_size": 1, "max_pages": 1}, "pagination_incomplete"),
    ([{"code": 0, "data": [month_row(), month_row()]}], {}, "duplicate_resource"),
])
def test_incomplete_or_duplicated_package_pages_fail_atomically(responses, options, reason):
    with pytest.raises(ProviderError, match=reason):
        QiniuCollector(ACCOUNT, Transport(*responses)).collect("resource_packages_month", options, NOW)


def package(**overrides):
    return {"order_hash": "selected-order", "po_id": 9, "respack_name": "production CDN package",
            "effective_start": "2026-09-01T00:00:00+08:00", "effective_end": "2026-09-16T00:00:00+08:00",
            "data_update_time": "2026-09-11T09:00:00+08:00", "status": 2, **overrides}


PACKAGE_OPTIONS = {"packages": [{"order_hash": "selected-order", "po_id": 9}]}


def test_expiry_monitors_selected_package_only_and_can_detect_its_expiration():
    transport = Transport({"code": 0, "data": [package(order_hash="historical", status=4),
                                                package(status=4, effective_end="2026-09-10T00:00:00+08:00")]})
    values = indexed(QiniuCollector(ACCOUNT, transport).collect("resource_packages", PACKAGE_OPTIONS, NOW))
    assert values["qiniu_package_expiry_days"].value < 0
    assert values["qiniu_package_unavailable"].value == 1
    assert all(m.resource_id == "package:selected-order:9" for m in values.values())


@pytest.mark.parametrize("rows,reason", [
    ([], "target_not_found"),
    ([package(data_update_time="2026-08-01T00:00:00+08:00")], "source_data_stale"),
    ([package(status=5)], "response_schema_invalid"),
    ([package(), package()], "duplicate_resource"),
])
def test_missing_stale_or_invalid_selected_package_is_collection_failure(rows, reason):
    with pytest.raises(ProviderError, match=reason):
        QiniuCollector(ACCOUNT, Transport({"code": 0, "data": rows})).collect(
            "resource_packages", PACKAGE_OPTIONS, NOW)


def test_kodo_uses_latest_complete_delayed_point_not_sum_or_future_zero():
    times = [int((NOW - timedelta(minutes=offset)).timestamp()) for offset in (17, 12, 7)]
    response = {"times": times, "datas": [100, 200, 0]}
    transport = Transport(response, response)
    metrics = QiniuCollector(ACCOUNT, transport).collect("kodo_usage", {"buckets": ["production"]}, NOW)
    assert [m.value for m in metrics] == [200, 200]
    assert all(m.observed_at == NOW - timedelta(minutes=12) for m in metrics)
    assert all("Authorization" in call[2]["headers"] for call in transport.calls)
    assert parse_qs(urlsplit(transport.calls[0][1]).query)["g"] == ["5min"]


@pytest.mark.parametrize("response,reason", [
    ({"times": [], "datas": []}, "source_not_ready"),
    ({"times": [int((NOW - timedelta(hours=1)).timestamp())], "datas": [100]}, "source_data_stale"),
    ({"times": [int((NOW - timedelta(minutes=12)).timestamp())], "datas": []}, "response_schema_invalid"),
])
def test_kodo_empty_stale_and_misaligned_data_fail(response, reason):
    with pytest.raises(ProviderError, match=reason):
        QiniuCollector(ACCOUNT, Transport(response)).collect("kodo_usage", {"buckets": ["production"]}, NOW)


def test_cdn_qbox_signing_payload_and_separate_region_metrics():
    response = {"code": 200, "time": ["2026-09-11 10:00:00", "2026-09-11 10:05:00"],
                "data": {"cdn.example.com": {"china": [1, 2], "oversea": [3, 4]}}}
    transport = Transport(response, response)
    metrics = QiniuCollector(ACCOUNT, transport).collect("cdn_usage", {"domains": ["cdn.example.com"]}, NOW)
    assert [m.value for m in metrics] == [2, 4, 2, 4]
    assert metrics[0].key() != metrics[1].key()
    method, url, options = transport.calls[0]
    assert json.loads(options["body"])["domains"] == "cdn.example.com"
    signed = "/v2/tune/monitoring/flow\n"
    digest = base64.urlsafe_b64encode(hmac.new(b"offline-sk", signed.encode(), hashlib.sha1).digest()).decode()
    assert options["headers"]["Authorization"] == "QBox offline-ak:" + digest


@pytest.mark.parametrize("total,error,ratio_count", [(1000, 20, 1), (20, 2, 0), (0, 0, 0)])
def test_cdn_status_counts_same_complete_window_and_requires_minimum_requests(total, error, ratio_count):
    response = {"code": 200, "data": {"points": ["2026-09-11-10-05"],
                                       "codes": {"200": [total - error], "503": [error]}}}
    transport = Transport(response)
    metrics = QiniuCollector(ACCOUNT, transport).collect("cdn_status", {"domains": ["cdn.example.com"]}, NOW)
    values = indexed(metrics)
    assert values["qiniu_cdn_requests"].value == total
    assert values["qiniu_cdn_5xx_requests"].value == error
    assert len(metrics) == 2 + ratio_count
    if ratio_count:
        assert values["qiniu_cdn_5xx_percent"].value == 2
    payload = json.loads(transport.calls[0][2]["body"])
    assert payload["domains"] == ["cdn.example.com"] and payload["freq"] == "5min"


def test_error_messages_do_not_contain_provider_response_or_credentials():
    transport = Transport({"code": 401, "message": "credential=DO-NOT-LOG", "data": {}})
    with pytest.raises(ProviderError) as error:
        QiniuCollector(ACCOUNT, transport).collect("balance", {}, NOW)
    assert str(error.value) == "provider_query_failed field=response.code"


@pytest.mark.parametrize("kind,options", [
    ("resource_packages", {}), ("resource_packages_month", {"page_size": 201}),
    ("kodo_usage", {"buckets": []}), ("cdn_usage", {"domains": ["https://invalid/path"]}),
    ("cdn_status", {"domains": ["cdn.example.com"], "minimum_requests": 0}),
    ("billing_snapshot", {"monthly_budget": 0, "budget_currency": "CNY"}),
    ("balance", {"guessed_balance_field": "balance"}),
])
def test_unsafe_or_incomplete_options_rejected_before_request(kind, options):
    with pytest.raises(ProviderError):
        validate_options(kind, options)
