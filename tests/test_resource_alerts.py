from dataclasses import replace
from datetime import datetime, timezone

import pytest

from cloud_monitor.alerting import resource_alerts_enabled
from cloud_monitor.config import validate_config
from cloud_monitor.models import Metric
from cloud_monitor.providers.aliyun import AliyunCollector
from cloud_monitor.providers.huawei import HuaweiCollector


NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
CERT = Metric("aliyun", "test", "cert-1", "Certificate", "expiry_days", 3, "days", NOW,
              {"domain": "primary.example.com", "domains": ["primary.example.com", "api.example.com"]})


def job(policy=None, kind="certificates"):
    result = {"id": "cert-job", "account": "test", "kind": kind, "interval_seconds": 60}
    if policy is not None:
        result["alerting"] = policy
    return result


def configuration(policy):
    return {"version": 1, "accounts": [{"id": "test", "provider": "aliyun", "enabled": False}],
            "jobs": [{**job(), "alerting": policy}]}


def test_omitted_and_empty_policy_preserve_existing_alerts():
    assert resource_alerts_enabled(job(), CERT)
    assert resource_alerts_enabled(job({}), CERT)
    assert resource_alerts_enabled(job({"resources": []}), CERT)
    assert not resource_alerts_enabled(job({"default_enabled": False}), CERT)


@pytest.mark.parametrize("domain", ["api.example.com", "API.EXAMPLE.COM.", "primary.example.com"])
def test_certificate_domain_selection_includes_sans(domain):
    selected = job({"default_enabled": False, "resources": [{"domain": domain, "enabled": True}]})
    assert resource_alerts_enabled(selected, CERT)
    assert resource_alerts_enabled(selected, CERT.to_dict())
    assert not resource_alerts_enabled(selected, replace(CERT, dimensions={"domain": "other.example.com"}))


def test_certificate_domain_selection_supports_legacy_comma_separated_domain():
    selected = job({"default_enabled": False,
                    "resources": [{"domain": "two.example.com", "enabled": True}]})
    assert resource_alerts_enabled(selected, replace(CERT, dimensions={
        "domain": "one.example.com,two.example.com"}))


def test_wildcard_certificate_name_is_literal_and_does_not_match_other_certificates():
    selected = job({"default_enabled": False,
                    "resources": [{"domain": "*.example.com", "enabled": True}]})
    assert not resource_alerts_enabled(selected, CERT)
    assert resource_alerts_enabled(selected, replace(CERT, dimensions={"domain": "*.example.com"}))


def test_registered_domain_selection_uses_resource_id_with_domain_normalization():
    selected = job({"default_enabled": False,
                    "resources": [{"domain": "EXAMPLE.COM.", "enabled": True}]}, kind="domains")
    assert resource_alerts_enabled(selected, replace(CERT, resource_id="example.com", dimensions={}))


@pytest.mark.parametrize("reverse", [False, True])
def test_disabled_certificate_id_overrides_enabled_domain_in_any_order(reverse):
    resources = [{"domain": "api.example.com", "enabled": True},
                 {"resource_id": "cert-1", "enabled": False}]
    selected = job({"default_enabled": False, "resources": resources[::-1] if reverse else resources})
    assert not resource_alerts_enabled(selected, CERT)
    assert resource_alerts_enabled(selected, replace(CERT, resource_id="renewed-cert"))


def test_resource_id_is_exact_and_case_sensitive():
    selected = job({"resources": [{"resource_id": "cert-1", "enabled": False}]})
    assert not resource_alerts_enabled(selected, CERT)
    assert resource_alerts_enabled(selected, replace(CERT, resource_id="cert-10"))
    assert resource_alerts_enabled(selected, replace(CERT, resource_id="CERT-1"))


@pytest.mark.parametrize("enabled", [False, True])
def test_package_selection_controls_parent_expiry_and_child_quota(enabled):
    selected = job({"default_enabled": not enabled,
                    "resources": [{"package_id": "package-1", "enabled": enabled}]}, kind="packages")
    parent = replace(CERT, resource_id="package-1", dimensions={})
    quota = replace(parent, resource_id="quota-1", metric="package_used_percent",
                    dimensions={"package_id": "package-1"})
    assert resource_alerts_enabled(selected, parent) == enabled
    assert resource_alerts_enabled(selected, quota) == enabled
    assert resource_alerts_enabled(selected, replace(parent, resource_id="package-2")) != enabled


def test_quota_item_can_be_excluded_from_enabled_package():
    selected = job({"default_enabled": False, "resources": [
        {"package_id": "package-1", "enabled": True}, {"resource_id": "quota-1", "enabled": False},
    ]}, kind="packages")
    quota = replace(CERT, resource_id="quota-1", metric="package_used_percent",
                    dimensions={"package_id": "package-1"})
    assert not resource_alerts_enabled(selected, quota)
    assert resource_alerts_enabled(selected, replace(quota, resource_id="quota-2"))


@pytest.mark.parametrize("policy", [
    None, [], False, {"default_enabled": "false"}, {"default_enabled": 0},
    {"enabled": False}, {"resource": []}, {"resources": None}, {"resources": {}},
    {"resources": [None]}, {"resources": [{}]},
    {"resources": [{"domain": "api.example.com"}]},
    {"resources": [{"domain": "api.example.com", "enabled": "false"}]},
    {"resources": [{"domain": "api.example.com", "enabled": 0}]},
    {"resources": [{"domain": "api.example.com", "resource_id": "cert-1", "enabled": True}]},
    {"resources": [{"resource_ids": ["cert-1"], "enabled": True}]},
    {"resources": [{"resource_id": 123, "enabled": True}]},
    {"resources": [{"resource_id": "", "enabled": True}]},
    {"resources": [{"package_id": " pkg-1 ", "enabled": True}]},
    {"resources": [{"domain": "https://api.example.com", "enabled": True}]},
    {"resources": [{"domain": "api.example.com,other.example.com", "enabled": True}]},
    {"resources": [{"domain": "*", "enabled": True}]},
    {"resources": [{"domain": "api.*.com", "enabled": True}]},
    {"resources": [{"domain": "API.EXAMPLE.COM.", "enabled": True},
                   {"domain": "api.example.com", "enabled": False}]},
    {"resources": [{"package_id": "pkg-1", "enabled": True},
                   {"package_id": "pkg-1", "enabled": False}]},
])
def test_offline_check_rejects_invalid_policy_even_for_disabled_accounts(policy):
    with pytest.raises(ValueError, match="cert-job.alerting"):
        validate_config(configuration(policy))


def test_valid_policy_is_not_mutated_by_validation():
    policy = {"default_enabled": False, "resources": [
        {"domain": "*.example.com", "enabled": True},
        {"resource_id": "001234567890123456789", "enabled": False},
        {"package_id": "pkg-1", "enabled": True},
    ]}
    assert validate_config(configuration(policy))["jobs"][0]["alerting"] == policy


def test_aliyun_collects_all_certificate_domains_without_extra_requests(monkeypatch):
    collector = AliyunCollector({"id": "test"})
    replies = iter([
        {"TotalCount": 1, "CertificateOrderList": [{
            "CertificateId": 9, "CommonName": "listed.example.com", "Sans": "list-san.example.com"}]},
        {"NotAfter": "2026-10-01T00:00:00Z", "Common": "primary.example.com",
         "Sans": "one.example.com,two.example.com", "Key": "do-not-persist"},
    ])
    calls = []

    def rpc(*args):
        calls.append(args)
        return next(replies)

    monkeypatch.setattr(collector, "_rpc", rpc)
    metric = collector.collect("certificates", {"statuses": [None]}, NOW)[0]
    assert len(calls) == 2
    assert metric.resource_name == "primary.example.com"
    assert metric.dimensions["domains"] == [
        "list-san.example.com", "listed.example.com", "one.example.com",
        "primary.example.com", "two.example.com",
    ]
    selected = job({"default_enabled": False,
                    "resources": [{"domain": "two.example.com", "enabled": True}]})
    assert resource_alerts_enabled(selected, metric)
    assert "do-not-persist" not in str(metric.to_dict())
    legacy = replace(metric, dimensions={"product": "ssl", "domain": "primary.example.com"})
    assert metric.key() == legacy.key()


def test_huawei_certificate_sans_participate_in_selection_without_changing_identity(monkeypatch):
    collector = HuaweiCollector({"id": "test"})
    monkeypatch.setattr(collector, "_call", lambda *args: {"certificates": [{
        "id": "cert-1", "name": "Console name", "domain": "primary.example.com", "status": "ISSUED",
        "sans": "api.example.com,other.example.com", "expire_time": "2026-10-01T00:00:00Z",
    }], "total_count": 1})
    metric = collector.collect("certificates", {}, NOW)[0]
    assert metric.dimensions["domains"] == ["api.example.com", "other.example.com", "primary.example.com"]
    selected = job({"default_enabled": False,
                    "resources": [{"domain": "api.example.com", "enabled": True}]})
    assert resource_alerts_enabled(selected, metric)
    assert metric.resource_name == "Console name"
    legacy = replace(metric, dimensions={k: v for k, v in metric.dimensions.items() if k != "domains"})
    assert metric.key() == legacy.key()
