from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from prometheus_client.parser import text_string_to_metric_families
import pytest
import yaml

from cloud_monitor.config import _validate_job, validate_config
from cloud_monitor.models import Metric
from cloud_monitor.prometheus import render_metrics
from cloud_monitor.providers.aliyun import AliyunCollector
from cloud_monitor.providers.huawei import HuaweiCollector
from cloud_monitor.service import Service, _collection_name
from cloud_monitor.store import Store


NOW = datetime(2026, 10, 20, 2, tzinfo=timezone.utc)
SUPPLEMENT = yaml.safe_load((Path(__file__).parents[1] / "config/network-traffic.yaml").read_text(encoding="utf-8"))


def configuration(rule, *, job_id="network"):
    provider, product = rule["match"]["provider"], rule["match"]["product"]
    period = 300 if (provider, product) == ("huawei", "ecs") else 60
    return validate_config({
        "version": 1,
        "accounts": [{"id": "example", "provider": provider, "enabled": True}],
        "jobs": [{"id": job_id, "account": "example", "kind": "network_traffic",
                  "interval_seconds": period, "max_age_seconds": 900, "rules": [rule["id"]],
                  "options": {"product": product}}],
        "rules": [deepcopy(rule)],
        "notifications": {"sms": {"enabled": True}},
    })


class OfflineNotifier:
    def send(self, *args):
        raise AssertionError("Network workflow verification must never send notifications")


@contextmanager
def offline_store():
    store = Store(":memory:")
    try:
        yield store
    finally:
        store.dispose()


def reading(rule, value, second, period):
    return Metric(rule["match"]["provider"], "example", "test-instance", "Example instance",
                  rule["metric"], value, "bit/s", NOW + timedelta(seconds=second),
                  {**rule["match"], "region": "cn-example", "sample_max_gap_seconds": period * 1.5,
                   "identity": {"direction": rule["metric"], "scope": rule["match"]["scope"]}})


CASES = [(rule, level) for rule in SUPPLEMENT["rules"] for level in rule["levels"]]


@pytest.mark.parametrize("rule,level", CASES,
                         ids=[f"{rule['id']}-{level['severity']}" for rule, level in CASES])
def test_continuous_source_samples_trigger_selected_level_and_channel(rule, level):
    cfg = configuration(rule)
    job = cfg["jobs"][0]
    period = job["interval_seconds"]
    samples = []
    factories = []

    class Collector:
        def collect(self, kind, options, now):
            assert kind == "network_traffic"
            return list(samples)

    def factory(account):
        factories.append(account["id"])
        return Collector()

    with offline_store() as store:
        service = Service(cfg, store, collector_factory=factory, notifier=OfflineNotifier())
        for second in range(0, level["for_seconds"], period):
            samples[:] = [reading(rule, level["threshold"], second, period)]
            assert service.collect(job, NOW + timedelta(seconds=second))
        assert store.list_messages() == []
        second = level["for_seconds"]
        samples[:] = [reading(rule, level["threshold"], second, period)]
        assert service.collect(job, NOW + timedelta(seconds=second))
        messages = store.list_messages()
        assert len(messages) == 1
        assert messages[0]["channel"] == {"P0": "sms", "P1": "dingtalk", "P2": "email"}[level["severity"]]
        assert factories == ["example"]

        families = list(text_string_to_metric_families(render_metrics(service, NOW + timedelta(seconds=second)).decode()))
        values = next(family.samples for family in families if family.name == "cloud_monitor_resource_value")
        assert len(values) == 1
        assert values[0].labels["unit"] == "bit/s"
        assert values[0].labels["scope"] == rule["match"]["scope"]
        assert values[0].value == level["threshold"]


def test_repeated_and_missing_source_samples_cannot_prove_continuous_high_traffic():
    rule = next(rule for rule in SUPPLEMENT["rules"] if rule["id"] == "huawei-ecs-network-in")
    cfg = configuration(rule)
    job = cfg["jobs"][0]
    period = job["interval_seconds"]
    with offline_store() as store:
        from cloud_monitor.engine import Engine
        engine = Engine(store, cfg)
        point = reading(rule, 900_000_000, 0, period)
        for second in (0, 300, 600):
            engine.evaluate(job, point, NOW + timedelta(seconds=second))
        assert store.list_messages() == []
        for second in (600, 900):
            engine.evaluate(job, reading(rule, 900_000_000, second, period), NOW + timedelta(seconds=second))
        assert store.list_messages() == []
        engine.evaluate(job, reading(rule, 900_000_000, 1200, period), NOW + timedelta(seconds=1200))
        assert len(store.list_messages()) == 1


def test_supplement_validates_against_existing_account_shapes_without_credentials():
    accounts = {
        "aliyun-hycen": {"provider": "aliyun", "regions": ["cn-hangzhou"]},
        "aliyun-yuanzhu": {"provider": "aliyun", "regions": ["cn-hangzhou"]},
        "huawei-hycen": {"provider": "huawei", "regions": ["cn-east-3"],
                         "project_ids": {"cn-east-3": "offline-project"}},
    }
    for job in SUPPLEMENT["jobs"]:
        _validate_job(job, accounts[job["account"]])
        expected = "ECS 网络速率采集" if job["options"]["product"] == "ecs" else "RDS 网络吞吐采集"
        assert _collection_name(job) == expected


def test_huawei_project_validation_follows_explicit_region_selection():
    account = {"provider": "huawei", "regions": ["cn-east-3"], "project_ids": {"cn-north-4": "offline-project"}}
    job = {"id": "network", "kind": "network_traffic", "options": {
        "product": "rds", "regions": ["cn-north-4"]}}
    _validate_job(job, account)
    job["options"]["regions"] = ["cn-east-3"]
    with pytest.raises(ValueError, match="project_ids.cn-east-3"):
        _validate_job(job, account)


@pytest.mark.parametrize("selection", [
    {"region": "cn-north-4"}, {"regions": ["cn-north-4"]},
    {"instances": [{"resource_id": "ecs-test", "region": "cn-north-4"}]},
])
def test_explicit_network_region_does_not_require_account_region_defaults(selection):
    account = {"provider": "huawei", "project_ids": {"cn-north-4": "offline-project"}}
    job = {"id": "network", "kind": "network_traffic", "options": {"product": "ecs", **selection}}
    _validate_job(job, account)


@pytest.mark.parametrize("provider", ["aliyun", "huawei"])
@pytest.mark.parametrize("account_regions", [{}, {"regions": ["cn-east-3"]}])
def test_empty_ecs_selection_does_not_require_regions_or_huawei_projects(provider, account_regions):
    account = {"provider": provider, **account_regions}
    job = {"id": "network", "kind": "network_traffic", "options": {"product": "ecs", "instances": []}}
    _validate_job(job, account)


@pytest.mark.parametrize("instances", [
    None, "i-test", {}, [{"resource_id": "i-test"}],
    [{"resource_id": "i-test", "region": "cn-hangzhou"}] * 2,
])
def test_offline_check_rejects_invalid_ecs_selection_without_credentials(tmp_path, capsys, instances):
    from cloud_monitor.cli import main

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "version": 1,
        "accounts": [{"id": "example", "provider": "aliyun", "enabled": False}],
        "jobs": [{"id": "network", "account": "example", "kind": "network_traffic", "interval_seconds": 60,
                  "options": {"product": "ecs", "instances": instances}}],
    }), encoding="utf-8")
    assert main(["--config", str(config_path), "check"]) == 2
    assert "network traffic" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("provider", ["aliyun", "huawei"])
def test_offline_check_accepts_empty_ecs_selection_without_credentials(tmp_path, capsys, provider):
    from cloud_monitor.cli import main

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "version": 1,
        "accounts": [{"id": "example", "provider": provider, "enabled": False}],
        "jobs": [{"id": "network", "account": "example", "kind": "network_traffic", "interval_seconds": 300,
                  "options": {"product": "ecs", "instances": []}}],
    }), encoding="utf-8")
    assert main(["--config", str(config_path), "check"]) == 0
    assert '"valid": true' in capsys.readouterr().out


@pytest.mark.parametrize("provider,collector_class", [("aliyun", AliyunCollector), ("huawei", HuaweiCollector)])
def test_empty_ecs_selection_is_no_data_without_cloud_requests_or_failure_alerts(provider, collector_class, monkeypatch):
    rule = next(rule for rule in SUPPLEMENT["rules"]
                if rule["match"]["provider"] == provider and rule["match"]["product"] == "ecs")
    cfg = configuration(rule)
    job = cfg["jobs"][0]
    job["options"]["instances"] = []

    def reject_cloud_request(*args, **kwargs):
        pytest.fail("Empty ECS selection must not query cloud APIs")

    monkeypatch.setattr(collector_class, "_rpc" if provider == "aliyun" else "_call", reject_cloud_request)
    with offline_store() as store:
        service = Service(cfg, store, collector_factory=collector_class, notifier=OfflineNotifier())
        assert service.collect(job, NOW)
        state = store.status()["jobs"][0]
        assert state["status"] == "no_data"
        assert state["sample_count"] == 0
        assert store.latest_samples([job["id"]]) == []
        assert store.list_messages() == []
        families = list(text_string_to_metric_families(render_metrics(service, NOW).decode()))
        assert not any(family.name == "cloud_monitor_resource_value" and family.samples for family in families)


def test_partial_collection_keeps_healthy_metrics_and_reports_error_without_dispatch():
    rule = SUPPLEMENT["rules"][0]
    cfg = configuration(rule)
    job = cfg["jobs"][0]

    class Collector:
        network_partial_failures = 1

        def collect(self, kind, options, now):
            return [reading(rule, 1234, 0, 60)]

    with offline_store() as store:
        service = Service(cfg, store, collector_factory=lambda _: Collector(), notifier=OfflineNotifier())
        assert not service.collect(job, NOW)
        state = store.status()["jobs"][0]
        assert state["status"] == "error"
        assert state["sample_count"] == 1
        assert "failed_resources=1" in state["detail"]
        assert len(store.latest_samples([job["id"]])) == 1
        families = list(text_string_to_metric_families(render_metrics(service, NOW).decode()))
        values = next(family.samples for family in families if family.name == "cloud_monitor_resource_value")
        assert [sample.value for sample in values] == [1234]
        assert store.list_messages()[0]["channel"] == "email"


def test_diagnostic_partial_failure_retains_count_but_does_not_disclose_resource_ids(monkeypatch):
    from cloud_monitor import diagnostics
    rule = SUPPLEMENT["rules"][0]
    cfg = configuration(rule)
    monkeypatch.setattr(diagnostics, "validate_account_credentials", lambda _: None)
    monkeypatch.setattr(diagnostics, "_validate_job", lambda *args: None)

    class Collector:
        network_partial_failures = 2

        def collect(self, kind, options, now):
            return [reading(rule, 1234, 0, 60)]

    report, = diagnostics.diagnose_jobs(cfg, collector_factory=lambda _: Collector(), now=NOW)
    assert report["status"] == "error"
    assert report["samples"] == 1
    assert report["detail"] == "Network collection partially failed; failed_resources=2"
    assert "test-instance" not in str(report)


@pytest.mark.parametrize("message", [
    "CMS network metric metadata has an unsupported rate unit",
    "CMS network metric metadata does not match dimensions, period or statistic",
    "CMS network metric metadata is missing or ambiguous",
])
def test_network_metadata_failures_have_safe_actionable_diagnostic_reasons(message):
    from cloud_monitor.diagnostics import safe_collection_error
    assert safe_collection_error(ValueError(message)) == "ValueError: " + message


def test_ces_discovery_permission_failure_exposes_only_allowlisted_status_and_code():
    from cloud_monitor.diagnostics import safe_collection_error
    from cloud_monitor.providers.huawei import HuaweiCollectionError
    message = "Huawei ces.list_metrics failed (status=403, code=CES.0015)"
    assert safe_collection_error(HuaweiCollectionError(message)) == "HuaweiCollectionError: " + message
