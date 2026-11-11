from datetime import datetime, timedelta, timezone

import pytest

from cloud_monitor.providers.huawei import HuaweiCollector, HuaweiCollectionError


NOW = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def collector():
    return HuaweiCollector({"id": "huawei-prod", "regions": ["cn-north-4"],
                            "access_key_id": "example", "access_key_secret": "example",
                            "project_ids": {"cn-north-4": "project-example"}})


def subscription(rid, expire="2026-09-09T00:00:00Z", service="hws.service.type.ec2"):
    return {"resource_id": rid, "resource_name": f"name-{rid}", "expire_time": expire,
            "region_code": "cn-north-4", "service_type_code": service}


def test_subscription_uses_prepaid_bss_operation_and_paginates(collector, monkeypatch):
    calls = []

    def call(service, region, operation, request):
        calls.append((service, region, operation, request))
        # A short page with a larger total must still be followed.
        rid = "first" if request.body.offset == 0 else "second"
        return {"data": [subscription(rid)], "total_count": 2}

    monkeypatch.setattr(collector, "_call", call)
    result = collector.collect("ecs_expiry", {}, NOW)
    assert [r.resource_id for r in result] == ["first", "second"]
    assert all(r.value == 1 for r in result)
    assert all(r.metric == "expiry_days" for r in result)
    assert [c[3].body.offset for c in calls] == [0, 1]
    assert calls[0][2] == "list_pay_per_use_customer_resources"
    assert calls[0][3].body.service_type_code == "hws.service.type.ec2"
    assert calls[0][3].body.only_main_resource == 1
    assert calls[0][3].body.status_list == [2, 4, 5]


@pytest.mark.parametrize("kind,service", [("slb_expiry", "hws.service.type.elb")])
def test_expiry_service_filters_and_resource_selection(collector, monkeypatch, kind, service):
    def call(api, region, operation, request):
        assert request.body.service_type_code == service
        assert request.body.resource_ids == ["selected"]
        return {"data": [subscription("other", service=service),
                         subscription("selected", "2026-09-07T12:00:00Z", service)], "total_count": 2}
    monkeypatch.setattr(collector, "_call", call)
    result = collector.collect(kind, {"resource_ids": ["selected"]}, NOW)
    assert len(result) == 1
    assert result[0].value == -0.5


def test_valid_empty_inventory_differs_from_missing_response_field(collector, monkeypatch):
    monkeypatch.setattr(collector, "_call", lambda *a: {"data": [], "total_count": 0})
    assert collector.collect("ecs_expiry", {}, NOW) == []
    with pytest.raises(HuaweiCollectionError, match="Configured subscription IDs"):
        collector.collect("ecs_expiry", {"resource_ids": ["expected"]}, NOW)
    monkeypatch.setattr(collector, "_call", lambda *a: {"total_count": 1})
    with pytest.raises(HuaweiCollectionError, match="data list"):
        collector.collect("ecs_expiry", {}, NOW)


def test_subscription_missing_expiry_fails_instead_of_fabricating_zero(collector, monkeypatch):
    monkeypatch.setattr(collector, "_call", lambda *a: {"data": [subscription("id", None)]})
    with pytest.raises(HuaweiCollectionError, match="Missing time"):
        collector.collect("ecs_expiry", {}, NOW)


def test_rds_discovers_only_mysql_postgres_and_uses_prepaid_expiry(collector, monkeypatch):
    calls = []

    def call(api, region, operation, request):
        assert api == "rds" and operation == "list_instances"
        calls.append(request.datastore_type)
        engine = request.datastore_type
        return {"instances": [
            {"id": f"{engine}-prepaid", "datastore": {"type": engine},
             "charge_info": {"charge_mode": "prePaid"}, "expiration_time": "2026-09-10T00:00:00Z"},
            {"id": f"{engine}-ondemand", "datastore": {"type": engine},
             "charge_info": {"charge_mode": "postPaid"}},
            {"id": "sqlserver", "datastore": {"type": "SQLServer"},
             "charge_info": {"charge_mode": "prePaid"}, "expiration_time": "2026-09-09T00:00:00Z"}],
            "total_count": 3}
    monkeypatch.setattr(collector, "_call", call)
    result = collector.collect("rds_expiry", {}, NOW)
    assert calls == ["MySQL", "PostgreSQL"]
    assert {m.resource_id for m in result} == {"MySQL-prepaid", "PostgreSQL-prepaid"}
    assert all(m.value == 2 and m.metric == "expiry_days" for m in result)


def test_rds_paginated_inventory_and_explicit_ids(collector, monkeypatch):
    offsets = []

    def call(api, region, operation, request):
        offsets.append(request.offset)
        return {"instances": [{"id": str(request.offset), "datastore": {"type": "MySQL"},
                               "charge_info": {"charge_mode": "prePaid"},
                               "expiration_time": "2026-09-08T12:00:00Z"}], "total_count": 2}
    monkeypatch.setattr(collector, "_call", call)
    result = collector.collect("rds_expiry", {"engines": ["MySQL"], "resource_ids": ["1"]}, NOW)
    assert offsets == [0, 1]
    assert len(result) == 1 and result[0].value == 0.5


def test_rds_missing_prepaid_expiry_is_collection_failure(collector, monkeypatch):
    monkeypatch.setattr(collector, "_call", lambda *a: {"instances": [
        {"id": "db", "datastore": {"type": "MySQL"}, "charge_info": {"charge_mode": "prePaid"}}]})
    with pytest.raises(HuaweiCollectionError, match="Missing time"):
        collector.collect("rds_expiry", {"engines": ["MySQL"]}, NOW)


def test_rds_scope_rejects_other_engines(collector):
    with pytest.raises(ValueError, match="MySQL and/or PostgreSQL"):
        collector.collect("rds_expiry", {"engines": ["SQLServer"]}, NOW)


def test_balance_uses_cash_account_only_and_retains_real_zero(collector, monkeypatch):
    monkeypatch.setattr(collector, "_call", lambda *a: {"account_balances": [
        {"account_id": "cash", "account_type": 1, "amount": "0", "currency": "CNY", "measure_id": 1},
        {"account_id": "credit", "account_type": 2, "amount": "100000", "currency": "CNY"},
        {"account_id": "reward", "account_type": 5, "amount": "9999", "currency": "CNY"}]})
    result = collector.collect("balance", {}, NOW)
    assert [(m.resource_id, m.value, m.unit) for m in result] == [("cash", 0, "CNY")]


def test_missing_balance_is_not_zero(collector, monkeypatch):
    monkeypatch.setattr(collector, "_call", lambda *a: {"account_balances": []})
    with pytest.raises(HuaweiCollectionError, match="absent"):
        collector.collect("balance", {}, NOW)


def package(remaining="25", total="100"):
    return {"order_instance_id": "package-1", "product_name": "OBS package", "status": 1,
            "expire_time": "2026-09-20T00:00:00Z", "quota_reuse_mode": 1,
            "region_code": "cn-north-4", "service_type_code": "hws.service.type.obs",
            "free_resources": [{"free_resource_id": "item-1", "usage_type_name": "egress",
                                "amount": remaining, "original_amount": total, "measure_id": 17}]}


def test_package_amount_means_remaining_and_keeps_each_quota_separate(collector, monkeypatch):
    first = package()
    first["free_resources"].append({"free_resource_id": "item-2", "amount": "0",
                                     "original_amount": "200", "usage_type_name": "storage"})
    def call(api, region, operation, request):
        assert operation == "list_free_resource_infos"
        assert request.body.service_type_code_list == ["hws.service.type.obs"]
        return {"free_resource_packages": [first], "total_count": 1}
    monkeypatch.setattr(collector, "_call", call)
    result = collector.collect("packages", {}, NOW)
    values = {(m.resource_id, m.metric): m.value for m in result}
    assert values[("package-1", "expiry_days")] == 12
    assert values[("item-1", "package_remaining_percent")] == 25
    assert values[("item-1", "package_used_percent")] == 75
    assert values[("item-2", "package_used_percent")] == 100


@pytest.mark.parametrize("remaining,total", [(None, "100"), ("25", None), ("0", "0"), ("NaN", "100")])
def test_unknown_or_invalid_package_quota_is_failure(collector, monkeypatch, remaining, total):
    monkeypatch.setattr(collector, "_call", lambda *a: {"free_resource_packages": [package(remaining, total)]})
    with pytest.raises(HuaweiCollectionError):
        collector.collect("packages", {}, NOW)


def test_certificates_ignore_pending_orders_but_include_uploaded_and_expired(collector, monkeypatch):
    def call(api, region, operation, request):
        assert api == "scm" and region == "cn-north-4"
        assert request.limit == 50 and request.status == "ALL"
        return {"certificates": [
            {"id": "pending", "status": "PAID", "expire_time": None},
            {"id": "uploaded", "status": "UPLOAD", "expire_time": "2026-10-08T00:00:00Z"},
            {"id": "old", "status": "EXPIRED", "expire_time": "2026-09-07T00:00:00Z"}], "total_count": 3}
    monkeypatch.setattr(collector, "_call", call)
    assert [(m.resource_id, m.value) for m in collector.collect("certificates", {}, NOW)] == [
        ("uploaded", 30), ("old", -1)]


def metric_spec(**extra):
    return {"region": "cn-north-4", "namespace": "SYS.ELB", "metric_name": "m7_in_Bps",
            "dimensions": {"lbaas_instance_id": "lb-1"}, "resource_id": "lb-1",
            "output_metric": "bandwidth_used_percent", "limit": 800, "multiplier": 8,
            "period": 300, "filter": "max", **extra}


def point(value, age=60):
    return {"max": value, "timestamp": int((NOW - timedelta(seconds=age)).timestamp() * 1000), "unit": "B/s"}


def test_metrics_use_newest_sample_and_explicit_units_normalization(collector, monkeypatch):
    def call(api, region, operation, request):
        assert request.dim_0 == "lbaas_instance_id,lb-1"
        assert request._from == int((NOW - timedelta(seconds=900)).timestamp() * 1000)
        return {"datapoints": [point(50), point(10, age=600)]}
    monkeypatch.setattr(collector, "_call", call)
    result = collector.collect("metrics", {"metrics": [metric_spec()]}, NOW)
    assert result[0].value == 50
    assert result[0].unit == "%"
    assert result[0].observed_at == NOW - timedelta(seconds=60)


@pytest.mark.parametrize("datapoints", [[], [point(None)], [point(0, age=3600)], [point(0, age=-600)]])
def test_empty_missing_or_stale_metrics_cannot_clear_an_alert(collector, monkeypatch, datapoints):
    monkeypatch.setattr(collector, "_call", lambda *a: {"datapoints": datapoints})
    with pytest.raises(HuaweiCollectionError):
        collector.collect("metrics", {"metrics": [metric_spec()]}, NOW)


@pytest.mark.parametrize("limit", [None, 0, -1, float("nan")])
def test_normalization_needs_real_positive_limit(collector, monkeypatch, limit):
    monkeypatch.setattr(collector, "_call", lambda *a: {"datapoints": [point(0)]})
    with pytest.raises((ValueError, HuaweiCollectionError)):
        collector.collect("metrics", {"metrics": [metric_spec(limit=limit)]}, NOW)


def test_api_error_message_does_not_echo_sensitive_request(collector, monkeypatch):
    class Client:
        def operation(self, request):
            raise RuntimeError("Authorization secret and request body")
    monkeypatch.setattr(collector, "_client", lambda *a: Client())
    with pytest.raises(HuaweiCollectionError) as error:
        collector._call("bss", "cn-north-1", "operation", None)
    assert "secret" not in str(error.value)
    assert "RuntimeError" in str(error.value)


def test_ces_rejects_fractional_period_instead_of_truncating(collector):
    with pytest.raises(ValueError, match="integer"):
        collector.collect("metrics", {"metrics": [metric_spec(period=300.5)]}, NOW)


def test_ces_listener_dimensions_are_part_of_stable_identity(collector, monkeypatch):
    monkeypatch.setattr(collector, "_call", lambda *a: {"datapoints": [point(20)]})
    specs = [metric_spec(dimensions={"lbaas_instance_id": "lb-1", "lbaas_listener_id": listener})
             for listener in ("listener-a", "listener-b")]
    result = collector.collect("metrics", {"metrics": specs}, NOW)
    assert result[0].key() != result[1].key()


def test_domain_registration_expiry_is_explicitly_unsupported(collector):
    with pytest.raises(ValueError, match="Unsupported"):
        collector.collect("domains", {}, NOW)


def test_sdk_clients_build_offline_with_explicit_tenant_ids(collector):
    collector.account["domain_id"] = "domain-example"
    assert collector._client("bss", "cn-north-1") is collector._client("bss", "cn-north-1")
    assert collector._client("scm", "cn-north-4") is not None
    assert collector._client("ces", "cn-north-4") is not None
    assert collector._client("rds", "cn-north-4") is not None
    assert collector._client("ecs", "cn-north-4") is collector._client("ecs", "cn-north-4")


def test_ecs_client_requires_explicit_project_id(collector):
    collector.account["project_ids"] = {}
    with pytest.raises(ValueError, match="project_ids.*required for ECS"):
        collector._client("ecs", "cn-north-4")


@pytest.mark.parametrize("service,expected", [("bss", "cn-north-1"), ("scm", "cn-north-4")])
def test_global_client_rejects_resource_region_before_authentication(collector, service, expected):
    collector.account.pop("domain_id", None)
    with pytest.raises(ValueError, match=f"account.{service}_region to {expected}"):
        collector._client(service, "cn-east-3")


@pytest.mark.parametrize("kind,service", [
    ("ecs_expiry", "bss"), ("slb_expiry", "bss"), ("balance", "bss"),
    ("packages", "bss"), ("certificates", "scm"),
])
def test_live_job_validation_checks_global_endpoint_without_requests(collector, kind, service):
    from cloud_monitor.config import _validate_job
    account = dict(collector.account, provider="huawei", **{f"{service}_region": "cn-east-3"})
    with pytest.raises(ValueError, match="endpoint region is unsupported"):
        _validate_job({"id": "job", "kind": kind}, account)


def test_certificate_validation_uses_task_region_override(collector):
    from cloud_monitor.config import _validate_job
    account = dict(collector.account, provider="huawei", scm_region="cn-east-3")
    _validate_job({"id": "job", "kind": "certificates", "options": {"region": "cn-north-4"}}, account)


def test_certificate_validation_rejects_invalid_task_region_override(collector):
    from cloud_monitor.config import _validate_job
    account = dict(collector.account, provider="huawei", scm_region="cn-north-4")
    with pytest.raises(ValueError, match="endpoint region is unsupported"):
        _validate_job({"id": "job", "kind": "certificates", "options": {"region": "cn-east-3"}}, account)
