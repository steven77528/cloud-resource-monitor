from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from prometheus_client.parser import text_string_to_metric_families
import pytest

from cloud_monitor.config import validate_config
from cloud_monitor.prometheus import render_metrics
from cloud_monitor.providers import relay
from cloud_monitor.providers.common import ProviderError
from cloud_monitor.service import Service
from cloud_monitor.store import Store


NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
KINDS = {"company_verification", "package_ledger"}
TOKEN = "offline-asign-relay-token-for-tests"
RELAY_URL = "https://relay.example.invalid/v1/monitor/collect"


class OpenAPIResponses:
    def __init__(self):
        self.fail = False
        self.verified = 1
        self.calls = []

    def __call__(self, method, url, **kwargs):
        assert method == "POST" and url == RELAY_URL
        assert kwargs["headers"]["Authorization"] == "Bearer " + TOKEN
        payload = json.loads(kwargs["body"])
        assert payload["provider"] == "asign" and payload["kind"] == "company_verification"
        assert payload["account"] == "asign-openapi-test"
        assert payload["request"] == {
            "method": "POST", "url": "https://oapi.asign.cn/user/getCompUser",
            "body": {"account": "test-company"},
        }
        assert TOKEN not in kwargs["body"].decode()
        self.calls.append(payload)
        if self.fail:
            raise ProviderError("transport_error")
        return {**{key: value for key, value in payload.items() if key != "request"},
                "status": "ok", "signature_verified": None, "response": {
                    "code": 100000, "data": [{"account": "test-company", "userType": 1,
                                               "status": self.verified}],
                }}


class NoDelivery:
    def send(self, *_):
        raise AssertionError("Offline workflow tests must not dispatch notifications")


def write_ledger(path, updated_at, remaining=50, expiry_days=90):
    path.write_text(json.dumps({
        "version": 1,
        "account_id": "asign-ledger-test",
        "updated_at": updated_at.isoformat(),
        "packages": [{
            "id": "signatures-main", "remaining": remaining, "total": 100,
            "unit": "signatures", "expires_at": (updated_at + timedelta(days=expiry_days)).isoformat(),
        }],
    }), encoding="utf-8")


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    token = tmp_path / "relay.token"
    token.write_text(TOKEN, encoding="ascii")
    ledger = tmp_path / "ledger.json"
    write_ledger(ledger, NOW)
    accounts = [
        {"id": "asign-openapi-test", "provider": "asign", "enabled": True,
         "api_mode": "openapi", "app_id": "offline-app",
         "relay": {"url": RELAY_URL, "token_file": str(token)}},
        {"id": "asign-ledger-test", "provider": "asign", "enabled": True,
         "api_mode": "ledger", "ledger_file": str(ledger)},
    ]
    jobs = [
        {"id": "asign-company-verification", "account": "asign-openapi-test",
         "kind": "company_verification", "interval_seconds": 300, "max_age_seconds": 7200,
         "rules": ["asign-company-unverified"], "options": {"operation": "company-verification",
             "collector": {"resource_id": "company-main", "selector": {"account": "test-company"}}}},
        {"id": "asign-package-ledger", "account": "asign-ledger-test", "kind": "package_ledger",
         "interval_seconds": 300, "max_age_seconds": 7200,
         "rules": ["asign-package-remaining", "asign-commercial-expiry"],
         "options": {"package_ids": ["signatures-main"]}},
    ]
    rules = [
        {"id": "asign-company-unverified", "metric": "company_verified",
         "levels": [{"severity": "P1", "op": "eq", "threshold": 0, "for_samples": 3}]},
        {"id": "asign-package-remaining", "metric": "package_remaining_percent",
         "levels": [{"severity": "P1", "op": "lt", "threshold": 10}]},
        {"id": "asign-commercial-expiry", "metric": "commercial_expiry_days",
         "levels": [{"severity": "P1", "op": "lt", "threshold": 7}]},
    ]
    cfg = validate_config({
        "version": 1, "accounts": accounts, "jobs": jobs, "rules": rules,
        "notifications": {"sms": {"enabled": False}},
    })
    responses = OpenAPIResponses()
    monkeypatch.setattr(relay, "request_json", responses)
    store = Store(tmp_path / "state.db")
    service = Service(cfg, store, notifier=NoDelivery())
    yield SimpleNamespace(service=service, store=store, responses=responses, ledger=ledger,
                          jobs={job["kind"]: job for job in service.jobs})
    store.dispose()


def poll(workflow, now):
    return {kind: workflow.service.collect(job, now) for kind, job in workflow.jobs.items()}


def exported(workflow, now, name="cloud_monitor_resource_value"):
    return [sample for family in text_string_to_metric_families(render_metrics(workflow.service, now).decode())
            for sample in family.samples if sample.name == name]


def active_rules(workflow):
    return {state["rule_id"] for state in workflow.store.status()["active_alerts"]}


def test_combined_asign_jobs_persist_and_export_distinct_sources(workflow):
    assert poll(workflow, NOW) == dict.fromkeys(KINDS, True)
    assert len(workflow.responses.calls) == 1
    stored = workflow.store.latest_samples([job["id"] for job in workflow.jobs.values()])
    keyed = {row["payload"]["metric"]: row["payload"] for row in stored}
    assert keyed["company_verified"]["value"] == 1
    assert keyed["package_remaining"]["value"] == 50
    assert keyed["commercial_expiry_days"]["value"] == 90

    series = {sample.labels["metric"]: sample for sample in exported(workflow, NOW)}
    assert series["company_verified"].labels["source"] == "official_api"
    assert series["company_verified"].labels["account"] == "asign-openapi-test"
    for metric in ("package_remaining", "package_remaining_percent", "commercial_expiry_days"):
        assert series[metric].labels["source"] == "ledger"
        assert series[metric].labels["account"] == "asign-ledger-test"
    assert series["package_remaining"].labels["unit"] == "signatures"
    api_rows = [row for row in stored if row["payload"]["metric"] == "api_query_success"]
    assert {row["job_id"] for row in api_rows} == {"asign-company-verification"}
    assert all(row["payload"]["value"] == 1 for row in api_rows)
    assert TOKEN not in json.dumps(stored)
    assert not workflow.store.list_messages()


def test_alerts_and_recoveries_do_not_cross_openapi_and_commercial_sources(workflow):
    workflow.responses.verified = 0
    write_ledger(workflow.ledger, NOW, remaining=5, expiry_days=3)
    for seconds in (0, 300):
        assert all(poll(workflow, NOW + timedelta(seconds=seconds)).values())
        assert "asign-company-unverified" not in active_rules(workflow)
    assert all(poll(workflow, NOW + timedelta(seconds=600)).values())
    ledger_rules = {"asign-package-remaining", "asign-commercial-expiry"}
    assert active_rules(workflow) == ledger_rules | {"asign-company-unverified"}

    workflow.responses.verified = 1
    assert all(poll(workflow, NOW + timedelta(seconds=900)).values())
    assert "asign-company-unverified" in active_rules(workflow)
    assert all(poll(workflow, NOW + timedelta(seconds=1200)).values())
    assert active_rules(workflow) == ledger_rules
    events = [json.loads(row["payload"]) for row in workflow.store.list_messages()]
    recoveries = [event for event in events if event["phase"] == "recovery"]
    assert {event["rule_id"] for event in recoveries} == {"asign-company-unverified"}
    assert all(event["dimensions"]["source"] == "official_api" for event in recoveries)

    write_ledger(workflow.ledger, NOW + timedelta(seconds=1500), remaining=80)
    assert all(poll(workflow, NOW + timedelta(seconds=1500)).values())
    assert active_rules(workflow) == ledger_rules
    assert all(poll(workflow, NOW + timedelta(seconds=1800)).values())
    assert not active_rules(workflow)
    events = [json.loads(row["payload"]) for row in workflow.store.list_messages()]
    assert {event["rule_id"] for event in events
            if event["phase"] == "recovery" and event["dimensions"]["source"] == "ledger"} == ledger_rules


def test_stale_ledger_preserves_alerts_and_does_not_write_zero_or_recovery(workflow):
    write_ledger(workflow.ledger, NOW, remaining=5, expiry_days=3)
    assert all(poll(workflow, NOW).values())
    ledger_job = workflow.jobs["package_ledger"]
    previous = workflow.store.latest_samples([ledger_job["id"]])
    # Even healthy-looking values must be rejected when their source is stale.
    write_ledger(workflow.ledger, NOW - timedelta(days=3), remaining=90, expiry_days=180)
    later = NOW + timedelta(seconds=7201)
    assert poll(workflow, later) == {"company_verification": True, "package_ledger": False}
    assert workflow.store.latest_samples([ledger_job["id"]]) == previous
    assert active_rules(workflow) == {"asign-package-remaining", "asign-commercial-expiry", "collection-error"}
    jobs = {job["id"]: job for job in workflow.store.status()["jobs"]}
    assert "source_data_stale" in jobs[ledger_job["id"]]["detail"]
    assert all(sample.labels["source"] != "ledger" for sample in exported(workflow, later))
    ledger_freshness = [sample for sample in exported(workflow, later, "cloud_monitor_resource_fresh")
                        if sample.labels["source"] == "ledger"]
    assert ledger_freshness and all(sample.value == 0 for sample in ledger_freshness)
    assert any(sample.labels["source"] == "official_api" for sample in exported(workflow, later))
    assert not any(json.loads(row["payload"])["phase"] == "recovery" for row in workflow.store.list_messages())


def test_openapi_request_failures_do_not_block_ledger_collection_or_alerts(workflow):
    assert all(poll(workflow, NOW).values())
    workflow.responses.fail = True
    later = NOW + timedelta(seconds=300)
    write_ledger(workflow.ledger, later, remaining=5, expiry_days=3)
    assert poll(workflow, later) == {"company_verification": False, "package_ledger": True}
    jobs = {job["id"]: job for job in workflow.store.status()["jobs"]}
    assert jobs["asign-package-ledger"]["status"] == "ok"
    alerts = workflow.store.status()["active_alerts"]
    assert {state["job_id"] for state in alerts if state["rule_id"] == "collection-error"} == {
        "asign-company-verification",
    }
    assert {state["rule_id"] for state in alerts if state["job_id"] == "asign-package-ledger"} == {
        "asign-package-remaining", "asign-commercial-expiry",
    }
    series = exported(workflow, later)
    assert {sample.labels["job_id"]: sample.value for sample in series
            if sample.labels["metric"] == "api_query_success"} == {"asign-company-verification": 0}
    assert [(sample.value, sample.labels["source"]) for sample in series
            if sample.labels["metric"] == "package_remaining"] == [(5, "ledger")]
