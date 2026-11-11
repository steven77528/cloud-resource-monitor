from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from cloud_monitor import providers
from cloud_monitor.models import Metric
from cloud_monitor.providers import relay
from cloud_monitor.providers.common import ProviderError
from cloud_monitor.providers.relay import RelayCollector, validate_account, validate_job


NOW = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)
TOKEN = "offline-relay-token-for-tests-001"
OPTIONS = {"operation": "balance-main", "source_max_age_seconds": 120, "collector": {}}


@pytest.fixture(autouse=True)
def forbid_default_transport(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("Inject an offline relay transport"))
    monkeypatch.setattr(relay, "request_json", forbidden)
    yield
    forbidden.assert_not_called()


@pytest.fixture
def account(tmp_path):
    path = tmp_path / "relay.token"
    path.write_text(TOKEN, encoding="ascii")
    return {"id": "merchant", "provider": "qiniu", "relay": {
        "url": "https://relay.example.test/v1/monitor/collect", "token_file": str(path)}}


def balance(**changes):
    return {"code": 0, "data": {"account_id": "finance-main", "currency": "CNY",
        "cash_balance": "12345678901", "present_balance": 0, "credit_line": 20000000000,
        "unpaid_money": "-12345678", **changes}}


class Responses:
    def __init__(self, raw=None, *, verified=None, update=None):
        self.raw = raw if raw is not None else balance()
        self.verified, self.update, self.calls = verified, update, []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        identity = json.loads(kwargs["body"])
        request = identity.pop("request")
        raw = self.raw(request) if callable(self.raw) else deepcopy(self.raw)
        result = {**identity, "status": "ok", "signature_verified": self.verified, "response": raw}
        if self.update:
            self.update(result)
        return result


def collect(account, responses, kind="balance", options=None):
    return RelayCollector(account, transport=responses).collect(kind, options or OPTIONS, NOW)


def test_unsigned_request_and_local_calculation(account, monkeypatch):
    from cloud_monitor.providers.qiniu import QiniuCollector

    forbidden = Mock(side_effect=AssertionError("Monitor cannot sign vendor requests"))
    monkeypatch.setattr(QiniuCollector, "_signed_request", forbidden)
    responses = Responses()
    metrics = collect(account, responses)
    method, url, kwargs = responses.calls[0]
    payload = json.loads(kwargs["body"])
    assert method == "POST" and url == account["relay"]["url"]
    assert set(payload) == {"version", "request_id", "provider", "account", "operation", "kind", "request"}
    assert payload["version"] == 2
    assert payload["request"] == {"method": "GET", "body": None,
        "url": "https://api.qiniu.com/billing-api/v1/account/balance-overview"}
    assert kwargs["headers"]["Authorization"] == "Bearer " + TOKEN
    assert TOKEN not in kwargs["body"].decode()
    assert account["relay"]["token_file"] not in kwargs["body"].decode()
    values = {metric.metric: metric.value for metric in metrics}
    assert values["qiniu_cash_balance"] == 123.45678901
    assert values["qiniu_unpaid_money"] == -0.12345678
    forbidden.assert_not_called()


def test_pagination_and_percentage_are_local(account):
    pages = []

    def raw(request):
        page = int(parse_qs(urlsplit(request["url"]).query)["page"][0])
        pages.append(page)
        return {"code": 0, "data": [] if page == 2 else [{"item_name": "storage", "zone_name": "z0",
            "available_time": "2026-09", "respack_unit": "GB", "total_surplus": 100,
            "month_used": 25, "month_remain": 75}]}

    responses = Responses(raw)
    metrics = collect(account, responses, "resource_packages_month",
                      {**OPTIONS, "collector": {"page_size": 1, "max_pages": 2}})
    assert pages == [1, 2]
    assert {m.metric: m.value for m in metrics}["qiniu_package_remaining_percent"] == 75
    assert len({json.loads(call[2]["body"])["request_id"] for call in responses.calls}) == 2


def test_token_rotation_and_immutable_account_binding(account):
    responses = Responses()
    collector = RelayCollector(account, transport=responses)
    account["id"] = "changed-after-construction"
    collector.collect("balance", OPTIONS, NOW)
    rotated = "offline-relay-token-for-tests-002"
    Path(account["relay"]["token_file"]).write_text(rotated, encoding="ascii")
    collector.collect("balance", OPTIONS, NOW)
    assert [c[2]["headers"]["Authorization"] for c in responses.calls] == ["Bearer " + TOKEN, "Bearer " + rotated]
    assert all(json.loads(c[2]["body"])["account"] == "merchant" for c in responses.calls)


@pytest.mark.parametrize("url", ["http://relay.test/", "https://user:password@relay.test/",
    "https://relay.test/?token=hidden", "https://relay.test/#fragment", "https://relay.test:0/",
    "https://relay.test:65536/", "https://relay.test:bad/", "https://relay.test\\@other.test/",
    "https://relay.test/\ncollect", "https:///collect", "file:///tmp/collect", "https://relay.test/collect ",
    "http://relay.test:18443/v1/monitor/collect", "http://127.0.0.1/v1/monitor/collect",
    "http://127.0.0.1:0/v1/monitor/collect", "http://127.0.0.1:65536/v1/monitor/collect",
    "http://127.0.0.1:18443/", "http://127.0.0.1:18443/v1/monitor/collect?",
    "http://user@127.0.0.1:18443/v1/monitor/collect", "http://127.0.0.1:18443/v1/monitor/collect#",
    "http://127.0.0.1:18443/v1/monitor/collect\n", "http://[fe80::1%25eth0]:18443/v1/monitor/collect",
    "http://2130706433:18443/v1/monitor/collect", "http://127.1:18443/v1/monitor/collect"])
def test_invalid_relay_urls(account, url):
    account["relay"]["url"] = url
    with pytest.raises(ProviderError, match="invalid_endpoint"):
        validate_account(account)


@pytest.mark.parametrize("url", ["http://192.0.2.10:18443/v1/monitor/collect",
    "http://[2001:db8::10]:18443/v1/monitor/collect", "https://relay.example.test/v1/monitor/collect"])
def test_valid_relay_urls(account, url):
    account["relay"]["url"] = url
    validate_account(account)
    responses = Responses()
    assert collect(account, responses)
    assert responses.calls[0][1] == url


def test_http_relay_still_forbids_http_vendor_url(account):
    account["relay"]["url"] = "http://192.0.2.10:18443/v1/monitor/collect"
    responses = Responses()
    collector = RelayCollector(account, transport=responses)
    collector._kind, collector._operation = "balance", "balance-main"
    with pytest.raises(ProviderError, match="invalid_configuration"):
        collector.request("GET", "http://api.qiniu.com/billing-api/v1/account/balance-overview")
    assert responses.calls == []


@pytest.mark.parametrize("provider", ["qiniu", "huifu", "asign", "wechat"])
@pytest.mark.parametrize("field", ["access_key_id", "access_key_secret", "access_key_secret_file",
    "private_key_file", "huifu_public_key_file", "token_file", "app_secret", "access_token", "_relay_client"])
def test_vendor_credentials_and_internal_bypass_forbidden_in_config(account, provider, field):
    account.update(provider=provider, **{field: "sensitive-placeholder"})
    with pytest.raises(ProviderError, match="relay_account_fields_forbidden") as error:
        validate_account(account)
    assert "sensitive-placeholder" not in str(error.value)


@pytest.mark.parametrize("provider", ["qiniu", "huifu", "asign", "wechat"])
@pytest.mark.parametrize("transport", [None, "direct", "local"])
def test_missing_relay_never_falls_back(monkeypatch, provider, transport):
    account = {"id": "merchant", "provider": provider}
    if transport is not None:
        account["transport"] = transport
    direct = Mock(side_effect=AssertionError("Direct dispatch forbidden"))
    monkeypatch.setattr(providers, "provider_module", direct)
    with pytest.raises(ProviderError, match="relay_(?:required|not_configured)"):
        providers.make_collector(account)
    with pytest.raises(ProviderError, match="relay_(?:required|not_configured)"):
        providers.validate_account_credentials(account)
    direct.assert_not_called()


@pytest.mark.parametrize("options", [{}, {"operation": "../admin"}, {"operation": "balance-main", "request": {}},
    {"operation": "balance-main", "access_token": "hidden"}, {"operation": "balance-main", "source_max_age_seconds": 0},
    {"operation": "balance-main", "collector": {"access_key_secret": "hidden"}}])
def test_invalid_options(account, options):
    with pytest.raises(ProviderError):
        validate_job(account, "balance", options)


@pytest.mark.parametrize("field,value", [("version", True), ("version", 1), ("request_id", "different"),
    ("provider", "huifu"), ("account", "another"), ("operation", "another"), ("kind", "cdn_usage")])
def test_response_identity(account, field, value):
    with pytest.raises(ProviderError, match="relay_identity_mismatch"):
        collect(account, Responses(update=lambda r: r.update({field: value})))


@pytest.mark.parametrize("changes", [{"metrics": []}, {"query_performed": True}, {"response": []},
    {"signature_verified": True}, {"message": "raw secret error"}])
def test_remote_cannot_return_metrics_or_unknown_envelope_fields(account, changes):
    with pytest.raises(ProviderError, match="relay_invalid_response"):
        collect(account, Responses(update=lambda r: r.update(changes)))


@pytest.mark.parametrize("raw", [{"code": 99, "message": "private error", "data": {}}, {"code": 0, "data": []},
    {"metrics": [{"metric": "qiniu_cash_balance", "value": 0}]}, balance(cash_balance="NaN"), balance(cash_balance=True)])
def test_monitor_validates_business_response(account, raw):
    with pytest.raises(ProviderError):
        collect(account, Responses(raw))


@pytest.mark.parametrize("token", [TOKEN, "offline-token-with-quote\"and\\slash"])
def test_token_reflection_is_rejected(account, token):
    Path(account["relay"]["token_file"]).write_text(token, encoding="ascii")
    with pytest.raises(ProviderError, match="relay_invalid_response"):
        collect(account, Responses({**balance(), "unused": [{"echo": token}]}))


def test_transport_failure_is_sanitized(account):
    with pytest.raises(ProviderError, match="^relay_request_failed$"):
        collect(account, Mock(side_effect=RuntimeError(TOKEN)))


def huifu_options():
    return {**OPTIONS, "collector": {"amount_unit_confirmed": True, "currency": "CNY", "source_amount_unit": "yuan",
        "amount_multiplier": "1", "accounts": [{"acct_id": "main", "acct_type": "01"}]}}


def huifu_raw(request):
    data = request["body"]
    return {"data": {"resp_code": "00000000", "req_date": data["req_date"], "req_seq_id": data["req_seq_id"],
        "acctInfo_list": json.dumps([{"huifu_id": "merchant-id", "acct_id": "main", "acct_type": "01",
            "acct_stat": "N", "avl_bal": "1234.56", "balance_amt": "1250.00", "frz_bal": "15.44"}])},
        "sign": "verified-by-relay"}


@pytest.mark.parametrize("verified,reason", [(False, "signature_invalid"), (None, "relay_invalid_response"),
                                           (1, "relay_invalid_response")])
def test_huifu_requires_verified_signature(account, verified, reason):
    account.update(provider="huifu", huifu_id="merchant-id")
    with pytest.raises(ProviderError, match=reason):
        collect(account, Responses(huifu_raw, verified=verified), options=huifu_options())


@pytest.mark.parametrize("string_data", [False, True])
def test_huifu_parses_locally_without_keys(account, monkeypatch, string_data):
    from cloud_monitor.providers import huifu

    account.update(provider="huifu", huifu_id="merchant-id")
    forbidden = Mock(side_effect=AssertionError("No Huifu keys in monitor"))
    monkeypatch.setattr(huifu, "_key", forbidden)

    def raw(request):
        result = huifu_raw(request)
        if string_data:
            result["data"] = json.dumps(result["data"])
        return result

    responses = Responses(raw, verified=True)
    values = {m.metric: m.value for m in collect(account, responses, options=huifu_options())}
    assert values["huifu_available_balance"] == 1234.56
    request = json.loads(responses.calls[0][2]["body"])["request"]
    assert "sign" not in request["body"] and "sys_id" not in request["body"]
    forbidden.assert_not_called()


def test_asign_without_private_key(account, monkeypatch):
    from cloud_monitor.providers import asign

    account.update(provider="asign", api_mode="openapi", app_id="offline-app")
    forbidden = Mock(side_effect=AssertionError("No Asign keys in monitor"))
    monkeypatch.setattr(asign, "_private_key", forbidden)
    responses = Responses({"code": 100000, "data": [{"account": "company", "userType": 1, "status": 1}]})
    metrics = collect(account, responses, "company_verification", {**OPTIONS,
        "collector": {"resource_id": "company-main", "selector": {"account": "company"}}})
    assert metrics[0].metric == "company_verified" and metrics[0].value == 1
    assert json.loads(responses.calls[0][2]["body"])["request"]["body"] == {"account": "company"}
    forbidden.assert_not_called()


def test_wechat_without_business_token(account, monkeypatch):
    from cloud_monitor.providers.wechat import WechatCollector

    account.update(provider="wechat", appid="offline-app")
    forbidden = Mock(side_effect=AssertionError("No Wechat token cache in monitor"))
    monkeypatch.setattr(WechatCollector, "_token", forbidden)
    responses = Responses({"errcode": 0, "quota": {"daily_limit": 1000, "used": 250, "remain": 750}})
    options = {**OPTIONS, "collector": {"cgi_paths": ["/cgi-bin/message/subscribe/send"]}}
    assert {m.metric: m.value for m in collect(account, responses, "api_quota", options)}["api_quota_remaining_percent"] == 75
    assert "access_token" not in json.loads(responses.calls[0][2]["body"])["request"]["url"]
    responses.raw = {"errcode": 40001, "errmsg": "not propagated"}
    with pytest.raises(ProviderError, match="wechat_api_error"):
        collect(account, responses, "api_quota", options)
    forbidden.assert_not_called()


def test_empty_local_manifest_does_not_contact_relay(account, tmp_path):
    account.update(provider="huifu", huifu_id="merchant-id")
    manifest = tmp_path / "orders.json"
    manifest.write_text(json.dumps({"updated_at": NOW.isoformat(), "orders": []}))
    account["relay"]["token_file"] = str(tmp_path / "absent.token")
    responses = Mock(side_effect=AssertionError("Empty manifest cannot query relay"))
    collector = RelayCollector(account, transport=responses)
    metrics = collector.collect("settlements", {**OPTIONS, "collector": {"orders_file": str(manifest)}}, NOW)
    assert len(metrics) == 1 and metrics[0].metric == "huifu_tracked_settlements" and metrics[0].value == 0
    assert collector.query_performed is False
    responses.assert_not_called()


def test_complete_local_batch_is_validated(account, monkeypatch):
    from cloud_monitor.providers.qiniu import QiniuCollector

    valid = Metric("qiniu", "merchant", "r1", "Resource", "qiniu_cash_balance", 1, "CNY", NOW,
                   {"source": "official_api"})
    stale = Metric("qiniu", "merchant", "r2", "Resource", "qiniu_cash_balance", 1, "CNY",
                   NOW - timedelta(seconds=121), {"source": "official_api"})
    monkeypatch.setattr(QiniuCollector, "collect", lambda *_: [valid, stale])
    with pytest.raises(ProviderError, match="source_data_stale"):
        collect(account, Responses())
    monkeypatch.setattr(QiniuCollector, "collect", lambda *_: [valid, valid])
    with pytest.raises(ProviderError, match="relay_duplicate_metric"):
        collect(account, Responses())
