from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from cloud_monitor.providers.common import ProviderError
from cloud_monitor.providers.wechat import WechatAPIError, WechatCollector, validate_account, validate_job, validate_options


NOW = datetime(2026, 9, 11, 2, 0, tzinfo=timezone.utc)
TOKEN = "offline-secret-token-never-print"
PRODUCT = {"spu_id": "100001", "product_type": "resource_pack", "unit": "calls", "resource_name": "Paid API"}


@pytest.fixture
def account(tmp_path):
    cache = tmp_path / "token.json"
    cache.write_text(json.dumps({"access_token": TOKEN, "expires_at": int(NOW.timestamp()) + 7200,
                                "appid": "wx-test"}),
                     encoding="utf-8")
    return {"id": "wechat-test", "appid": "wx-test", "token_file": str(cache)}


def collector(account, responses):
    calls = []
    pending = iter(responses)

    def transport(method, url, **kwargs):
        calls.append({"method": method, "path": urlsplit(url).path,
                      "query": parse_qs(urlsplit(url).query),
                      "body": json.loads(kwargs["body"]) if kwargs["body"] else None})
        return next(pending)

    return WechatCollector(account, transport=transport), calls


def package(pkg_id="package-one", *, status=1, capacity="100", used="25", start=None, end=None, spu="100001"):
    return {"pkgId": pkg_id, "spuId": spu, "status": status, "all": capacity, "used": used,
            "startTime": int((start or NOW - timedelta(days=10)).timestamp()),
            "endTime": int((end or NOW + timedelta(days=20)).timestamp())}


def package_page(rows, total=None, **kwargs):
    return {"errcode": 0, "total": total if total is not None else len(rows), "detailList": rows, **kwargs}


def find(metrics, name, resource=None):
    matches = [metric for metric in metrics if metric.metric == name
               and (resource is None or metric.resource_id == resource)]
    assert len(matches) == 1
    return matches[0]


def authorize(account, permission_sets=(18, 30)):
    account = {**account, "token_type": "authorizer_access_token", "third_party_authorized": True,
               "permission_sets": list(permission_sets)}
    from pathlib import Path
    Path(account["token_file"]).write_text(json.dumps({
        "authorizer_access_token": TOKEN, "expires_at": int(NOW.timestamp()) + 7200, "appid": "wx-test",
    }), encoding="utf-8")
    return account


def test_quota_request_and_zero_denominator(account):
    instance, calls = collector(account, [
        {"errcode": 0, "quota": {"daily_limit": 100, "used": 85, "remain": 15}},
        {"errcode": 0, "quota": {"daily_limit": 0, "used": 0, "remain": 0}},
    ])
    result = instance.collect("api_quota", {"cgi_paths": ["/cgi-bin/token", "/wxa/getwxacode"]}, NOW)
    assert find(result, "api_quota_remaining_percent").value == 15
    assert len(result) == 7
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/cgi-bin/openapi/quota/get"
    assert calls[0]["body"] == {"cgi_path": "/cgi-bin/token"}
    assert calls[0]["query"]["access_token"] == [TOKEN]
    assert TOKEN not in repr([metric.to_dict() for metric in result])


@pytest.mark.parametrize("bad", [None, True, "NaN", "Infinity", -1, "1.5"])
def test_quota_invalid_number_never_becomes_zero(account, bad):
    instance, _ = collector(account, [{"errcode": 0, "quota": {"daily_limit": 100, "used": 2, "remain": bad}}])
    with pytest.raises(ProviderError):
        instance.collect("api_quota", {"cgi_paths": ["/cgi-bin/token"]}, NOW)


def test_quota_identity_survives_date_change(account):
    payload = {"errcode": 0, "quota": {"daily_limit": 100, "used": 20, "remain": 80}}
    first, _ = collector(account, [payload])
    second, _ = collector(account, [payload])
    one = first.collect("api_quota", {"cgi_paths": ["/cgi-bin/token"]}, NOW)
    two = second.collect("api_quota", {"cgi_paths": ["/cgi-bin/token"]}, NOW - timedelta(days=1))
    assert [metric.key() for metric in one] == [metric.key() for metric in two]
    assert one[0].dimensions["data_date"] != two[0].dimensions["data_date"]


def test_packages_paginate_short_pages_and_keep_bigint_subtraction_exact(account):
    capacity = 2**63 - 1
    instance, calls = collector(account, [
        package_page([package("one", capacity=str(capacity), used=str(capacity - 1))], total=2),
        package_page([package("two", capacity="100", used="90")], total=2),
    ])
    result = instance.collect("charge_packages", {"products": [PRODUCT]}, NOW)
    assert [call["query"]["offset"] for call in calls] == [["0"], ["1"]]
    assert all(call["method"] == "GET" and call["query"]["limit"] == ["20"] for call in calls)
    assert find(result, "package_remaining", "spu:100001:package:one").value == 1
    aggregate = find(result, "charge_active_remaining")
    assert aggregate.value == 11
    assert aggregate.dimensions["raw_total"] == str(capacity + 100)
    assert aggregate.dimensions["raw_remaining"] == "11"
    assert find(result, "charge_active_package_count").value == 2


def test_packages_active_only_aggregate_and_expired_opt_in(account):
    rows = [package("active"), package("next", status=2, start=NOW + timedelta(days=1)),
            package("old", status=3, end=NOW - timedelta(days=1))]
    instance, _ = collector(account, [package_page(rows)])
    result = instance.collect("charge_packages", {"products": [PRODUCT]}, NOW)
    assert find(result, "charge_active_remaining").value == 75
    assert find(result, "charge_active_package_count").value == 1
    assert not any("old" in metric.resource_id for metric in result)
    assert not any(metric.metric == "package_remaining_percent" and "next" in metric.resource_id for metric in result)
    instance, _ = collector(account, [package_page(rows)])
    result = instance.collect("charge_packages", {"products": [{**PRODUCT, "track_expired_package_ids": ["old"]}]}, NOW)
    assert find(result, "package_expiry_days", "spu:100001:package:old").value == -1


def test_packages_zero_capacity_does_not_emit_ratio(account):
    instance, _ = collector(account, [package_page([package(capacity="0", used="0")])])
    result = instance.collect("charge_packages", {"products": [PRODUCT]}, NOW)
    assert find(result, "charge_active_remaining").value == 0
    assert not any(metric.metric.endswith("percent") for metric in result)


def test_packages_empty_response_only_emits_real_active_count(account):
    instance, _ = collector(account, [package_page([])])
    result = instance.collect("charge_packages", {"products": [PRODUCT]}, NOW)
    assert len(result) == 1
    assert result[0].metric == "charge_active_package_count"
    assert result[0].value == 0


@pytest.mark.parametrize("responses,reason", [
    ([package_page([package()], total=2), package_page([], total=2)], "pagination_incomplete"),
    ([package_page([package()], total=2), package_page([package()], total=2)], "pagination_duplicate"),
    ([package_page([package()], total=2), package_page([], total=3)], "pagination_changed"),
    ([package_page([package()], total=0)], "pagination_invalid"),
    ([package_page([package(spu="other")])], "product_mismatch"),
    ([{"errcode": 0, "total": 0}], "invalid_list"),
])
def test_packages_reject_partial_or_mismatched_inventory(account, responses, reason):
    instance, _ = collector(account, responses)
    with pytest.raises(ProviderError, match=reason):
        instance.collect("charge_packages", {"products": [PRODUCT]}, NOW)


def test_packages_max_pages_never_returns_partial_metrics(account):
    instance, _ = collector(account, [package_page([package()], total=2)])
    with pytest.raises(ProviderError, match="pagination_limit"):
        instance.collect("charge_packages", {"products": [PRODUCT], "max_pages": 1}, NOW)


@pytest.mark.parametrize("field,bad", [("all", None), ("used", True), ("used", "101"),
                                      ("status", 4), ("endTime", None)])
def test_package_invalid_fields_fail_collection(account, field, bad):
    row = package()
    row[field] = bad
    instance, _ = collector(account, [package_page([row])])
    with pytest.raises(ProviderError):
        instance.collect("charge_packages", {"products": [PRODUCT]}, NOW)


def test_unit_participates_in_package_identity(account):
    instance, _ = collector(account, [package_page([package()]), package_page([package()])])
    one = instance.collect("charge_packages", {"products": [PRODUCT]}, NOW)
    two = instance.collect("charge_packages", {"products": [{**PRODUCT, "unit": "bytes"}]}, NOW)
    assert find(one, "package_remaining_percent").key() != find(two, "package_remaining_percent").key()


def test_subscription_has_only_expiry_and_active_no_usage_ratio(account):
    instance, _ = collector(account, [{"errcode": 0,
                                      "startServiceTime": int((NOW - timedelta(days=1)).timestamp()),
                                      "endServiceTime": int((NOW + timedelta(days=7)).timestamp())}])
    result = instance.collect("charge_packages", {"products": [{"spu_id": "100001", "product_type": "subscription"}]}, NOW)
    assert {metric.metric for metric in result} == {"subscription_expiry_days", "subscription_active"}
    assert find(result, "subscription_expiry_days").value == 7


def test_subscription_missing_expiry_is_error(account):
    instance, _ = collector(account, [package_page([], startServiceTime=1)])
    with pytest.raises(ProviderError, match="endServiceTime"):
        instance.collect("charge_packages", {"products": [{"spu_id": "100001", "product_type": "subscription"}]}, NOW)


def test_expired_cache_and_reload_each_collect(account):
    from pathlib import Path
    instance, calls = collector(account, [{"errcode": 0, "quota": {"daily_limit": 1, "used": 0, "remain": 1}}])
    instance.collect("api_quota", {"cgi_paths": ["/cgi-bin/token"]}, NOW)
    Path(account["token_file"]).write_text(json.dumps({"access_token": TOKEN, "expires_at": int(NOW.timestamp())}),
                                            encoding="utf-8")
    with pytest.raises(ProviderError, match="token_cache_expired"):
        instance.collect("api_quota", {"cgi_paths": ["/cgi-bin/token"]}, NOW)
    assert len(calls) == 1


@pytest.mark.parametrize("content", ["not-json-secret", "[]", '{"access_token":"secret"}',
                                     '{"access_token":"secret\\nsecret","expires_at":9999999999}'])
def test_token_cache_invalid_never_leaks(account, content):
    from pathlib import Path
    Path(account["token_file"]).write_text(content, encoding="utf-8")
    instance, calls = collector(account, [])
    with pytest.raises(ProviderError) as caught:
        instance.collect("api_quota", {"cgi_paths": ["/cgi-bin/token"]}, NOW)
    assert "secret" not in str(caught.value)
    assert not calls


def test_token_cache_wrong_appid_rejected(account):
    from pathlib import Path
    Path(account["token_file"]).write_text(json.dumps({"access_token": TOKEN, "appid": "other",
                                                        "expires_at": int(NOW.timestamp()) + 3600}), encoding="utf-8")
    instance, calls = collector(account, [])
    with pytest.raises(ProviderError, match="token_account_mismatch"):
        instance.collect("api_quota", {"cgi_paths": ["/cgi-bin/token"]}, NOW)
    assert not calls


def test_token_cache_requires_explicit_appid_binding(account):
    from pathlib import Path
    Path(account["token_file"]).write_text(json.dumps({"access_token": TOKEN,
                                                        "expires_at": int(NOW.timestamp()) + 3600}), encoding="utf-8")
    instance, calls = collector(account, [])
    with pytest.raises(ProviderError, match="token_account_mismatch"):
        instance.collect("api_quota", {"cgi_paths": ["/cgi-bin/token"]}, NOW)
    assert not calls


def test_api_error_preserves_code_without_message_or_token(account):
    instance, _ = collector(account, [{"errcode": 40001, "errmsg": TOKEN}])
    with pytest.raises(WechatAPIError) as caught:
        instance.collect("api_quota", {"cgi_paths": ["/cgi-bin/token"]}, NOW)
    assert caught.value.api_code == 40001
    assert "40001" in str(caught.value)
    assert TOKEN not in str(caught.value)


def test_transport_error_does_not_leak_url(account):
    def transport(method, url, **kwargs):
        raise RuntimeError(url)
    instance = WechatCollector(account, transport=transport)
    with pytest.raises(ProviderError) as caught:
        instance.collect("api_quota", {"cgi_paths": ["/cgi-bin/token"]}, NOW)
    assert caught.value.__suppress_context__
    assert TOKEN not in str(caught.value)
    assert "https" not in str(caught.value)


def basic_info(**verify):
    return {"errcode": 0, "appid": "wx-test", "account_type": 3, "wx_verify_info": verify}


@pytest.mark.parametrize("account_override", [
    {}, {"token_type": "authorizer_access_token"},
    {"token_type": "authorizer_access_token", "third_party_authorized": True, "permission_sets": [18]},
])
def test_annual_review_requires_third_party_and_permission_thirty(account, account_override):
    instance, calls = collector({**account, **account_override}, [])
    with pytest.raises(ProviderError):
        instance.collect("annual_review", {}, NOW)
    assert not calls


def test_charge_delegation_requires_permission_eighteen(account):
    instance, calls = collector(authorize(account, (30,)), [])
    with pytest.raises(ProviderError, match="permission_set_required"):
        instance.collect("charge_packages", {"products": [PRODUCT]}, NOW)
    assert not calls


def test_unverified_has_no_fabricated_annual_deadline(account):
    instance, calls = collector(authorize(account), [basic_info(qualification_verify=False)])
    result = instance.collect("annual_review", {}, NOW)
    assert len(result) == 1 and result[0].metric == "qualification_verified" and result[0].value == 0
    assert calls[0]["method"] == "POST"


def test_annual_review_fields_and_explicit_get_mode(account):
    response = basic_info(qualification_verify=True, annual_review=True,
                          annual_review_begin_time=int((NOW - timedelta(days=20)).timestamp()),
                          annual_review_end_time=int((NOW + timedelta(days=6)).timestamp()))
    instance, calls = collector(authorize(account), [response])
    result = instance.collect("annual_review", {"method": "GET"}, NOW)
    assert find(result, "annual_review_expiry_days").value == 6
    assert find(result, "annual_review_required").value == 1
    assert calls[0]["method"] == "GET"


def test_verified_but_missing_deadline_fails(account):
    instance, _ = collector(authorize(account), [basic_info(qualification_verify=True, annual_review=False)])
    with pytest.raises(ProviderError, match="annual_review_begin_time"):
        instance.collect("annual_review", {}, NOW)


@pytest.mark.parametrize("field,value", [("appid", "other"), ("account_type", 2)])
def test_basic_info_wrong_account_fails(account, field, value):
    response = basic_info(qualification_verify=False)
    response[field] = value
    instance, _ = collector(authorize(account), [response])
    with pytest.raises(ProviderError):
        instance.collect("annual_review", {}, NOW)


def js_error(index=1):
    return {"errorMsgMd5": f"{index:032x}", "errorStackMd5": f"{index + 10:032x}",
            "pv": 20, "uv": 5, "errorMsg": TOKEN, "errorStack": TOKEN}


def test_js_error_complete_window_pagination_and_no_user_sum(account):
    instance, calls = collector(account, [
        {"errcode": 0, "data": [js_error(1)], "totalCount": 2},
        {"errcode": 0, "data": [js_error(2)], "totalCount": 2},
    ])
    result = instance.collect("js_errors", {}, NOW)
    assert calls[0]["body"]["startTime"] == calls[0]["body"]["endTime"] == "2026-09-10"
    assert [call["body"]["offset"] for call in calls] == [0, 1]
    assert calls[0]["body"]["limit"] == 30
    assert [metric.value for metric in result if metric.metric == "js_error_affected_users"] == [5, 5]
    assert find(result, "js_error_group_count").value == 2
    assert find(result, "js_error_total_occurrences").value == 40
    assert all(metric.observed_at == datetime(2026, 9, 10, 16, tzinfo=timezone.utc) for metric in result)
    assert TOKEN not in repr([metric.to_dict() for metric in result])
    assert all("collected_at" in metric.dimensions for metric in result)


def test_js_errors_zero_groups_is_valid_complete_inventory(account):
    instance, _ = collector(account, [{"errcode": 0, "data": [], "totalCount": 0}])
    result = instance.collect("js_errors", {}, NOW)
    assert len(result) == 2 and all(metric.value == 0 for metric in result)


def test_js_errors_duplicate_page_fails(account):
    instance, _ = collector(account, [{"errcode": 0, "data": [js_error()], "totalCount": 2}] * 2)
    with pytest.raises(ProviderError, match="pagination_duplicate"):
        instance.collect("js_errors", {}, NOW)


def test_performance_parses_json_and_uses_latest_complete_day(account):
    response = {"errcode": 0, "default_time_data": json.dumps({"list": [
        {"ref_date": "20260909", "cost_time_type": 1, "cost_time": 900},
        {"ref_date": "20260910", "cost_time_type": 1, "cost_time": 1100},
        {"ref_date": "20260911", "cost_time_type": 1, "cost_time": 100},
    ]})}
    instance, calls = collector(account, [response])
    result = instance.collect("performance", {"cost_time_types": [1]}, NOW)
    assert len(result) == 1 and result[0].metric == "performance_cost_milliseconds"
    assert result[0].value == 1100
    assert result[0].dimensions["data_date"] == "2026-09-10"
    assert result[0].observed_at == datetime(2026, 9, 10, 16, tzinfo=timezone.utc)
    assert calls[0]["body"]["default_end_time"] == int(datetime(2026, 9, 10, 16, tzinfo=timezone.utc).timestamp()) - 1
    assert "p95" not in repr(result)


@pytest.mark.parametrize("payload,reason", [
    ("not-json-secret", "invalid_json_field"), ("[]", "invalid_object"),
    ('{"list":[]}', "no_complete_data"),
    ('{"list":[{"ref_date":"20260901","cost_time_type":1,"cost_time":10}]}', "no_complete_data"),
    ('{"list":[{"ref_date":"20260908","cost_time_type":1,"cost_time":10}]}', "stale_data"),
    ('{"list":[{"ref_date":"20260910","cost_time_type":2,"cost_time":10}]}', "metric_type_mismatch"),
    ('{"list":[{"ref_date":"20260910","cost_time_type":1,"cost_time":null}]}', "invalid_number"),
])
def test_performance_rejects_invalid_or_stale_data(account, payload, reason):
    instance, _ = collector(account, [{"errcode": 0, "default_time_data": payload}])
    with pytest.raises(ProviderError, match=reason):
        instance.collect("performance", {"cost_time_types": [1]}, NOW)


@pytest.mark.parametrize("kind,options", [
    ("api_quota", {"cgi_paths": ["https://api.weixin.qq.com/a"]}),
    ("api_quota", {"cgi_paths": ["/sns/jscode2session"]}),
    ("api_quota", {"cgi_paths": ["/a?access_token=secret"]}),
    ("api_quota", {"cgi_paths": ["/../token"]}),
    ("api_quota", {"cgi_paths": ["/a", "/a"]}),
    ("charge_packages", {"products": [{"spu_id": "1", "product_type": "resource_pack"}]}),
    ("charge_packages", {"products": [PRODUCT, deepcopy(PRODUCT)]}),
    ("charge_packages", {"products": [PRODUCT], "max_pages": "2"}),
    ("js_errors", {"day_offset": 0}), ("js_errors", {"day_offset": "1"}),
    ("js_errors", {"error_type": 1}), ("performance", {"cost_time_types": [True]}),
    ("performance", {"cost_time_types": [1, 1]}), ("performance", {"lookback_days": "8"}),
    ("annual_review", {"method": "DELETE"}),
])
def test_invalid_options_fail_safely(kind, options):
    with pytest.raises(ProviderError):
        validate_options(kind, options)


def test_account_validation_does_not_read_secret_file():
    validate_account({"id": "wechat-test", "appid": "wx-test", "token_file": "/not-mounted/offline-token.json"})


def test_job_validation_checks_permissions_without_reading_token_file():
    account = {"id": "wechat-test", "appid": "wx-test", "token_file": "/not-mounted/offline-token.json"}
    validate_job(account, "api_quota", {"cgi_paths": ["/cgi-bin/token"]})
    with pytest.raises(ProviderError, match="third_party_authorization_required"):
        validate_job(account, "annual_review", {})
    account.update({"token_type": "authorizer_access_token", "third_party_authorized": True,
                    "permission_sets": [18]})
    with pytest.raises(ProviderError, match="permission_set_required"):
        validate_job(account, "annual_review", {})
    validate_job(account, "charge_packages", {"products": [PRODUCT]})
    account["permission_sets"].append(30)
    validate_job(account, "annual_review", {})
