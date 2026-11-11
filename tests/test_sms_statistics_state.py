from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
import json
import threading
from unittest.mock import Mock

import pytest
from prometheus_client.parser import text_string_to_metric_families

from cloud_monitor.api import make_server
from cloud_monitor.config import validate_config
from cloud_monitor.prometheus import render_metrics
from cloud_monitor.providers import aliyun
from cloud_monitor.service import Service
from cloud_monitor.store import Store


NOW = datetime(2026, 10, 19, 1, 3, 51, tzinfo=timezone.utc)
ACCOUNT = {"id": "aliyun-hycen", "provider": "aliyun", "enabled": True}
JOB = {"id": "sms-statistics", "account": ACCOUNT["id"], "kind": "sms_statistics",
       "interval_seconds": 3600}
ADMIN_TOKEN = "sms-statistics-admin-token-only-for-tests"


def response(current=300, baseline=100):
    end = NOW.astimezone(timezone(timedelta(hours=8))).date() - timedelta(days=1)
    rows = [{"SendDate": (end - timedelta(days=offset)).strftime("%Y%m%d"),
             "TotalCount": current if offset == 0 else baseline} for offset in range(8)]
    return {"Code": "OK", "Data": {"TotalSize": len(rows), "TargetList": rows}}


def sparse_response():
    return {"Code": "OK", "Data": {"TotalSize": 1, "TargetList": [
        {"SendDate": "20261015", "TotalCount": 2},
    ]}}


class FakeCollector(aliyun.AliyunCollector):
    def __init__(self, responses):
        super().__init__(ACCOUNT)
        self.responses = list(responses)

    def _rpc(self, service, action, version, params, region=None):
        assert (service, action) == ("dysmsapi", "QuerySendStatistics")
        if not self.responses:
            raise AssertionError("Unexpected SMS statistics request")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


@pytest.fixture
def store():
    instance = Store(":memory:")
    try:
        yield instance
    finally:
        instance.dispose()


def service_for(store, responses):
    config = validate_config({
        "version": 1,
        "accounts": [ACCOUNT],
        "jobs": [JOB],
        "rules": [{"id": "sms-growth", "metric": "sms_growth_percent", "levels": [
            {"severity": "P1", "op": "ge", "threshold": 50},
        ]}],
        "service": {"recovery_samples": 2},
        "api": {"admin_token": ADMIN_TOKEN},
    })
    collector = FakeCollector(responses)
    return Service(config, store, collector_factory=lambda _: collector, notifier=Mock())


def metric_samples(service, now):
    families = list(text_string_to_metric_families(render_metrics(service, now).decode()))
    return {family.name: family.samples for family in families}


def active_rules(store):
    return {state["rule_id"] for state in store.status()["active_alerts"]}


@pytest.mark.parametrize("result,reason", [
    (sparse_response(), "missing dates"),
    (response(current=2, baseline=0), "undefined"),
])
def test_sms_missing_history_and_zero_baseline_have_a_typed_insufficient_result(result, reason):
    with pytest.raises(aliyun.SmsStatisticsInsufficientData, match=reason):
        FakeCollector([result]).collect("sms_statistics", {}, NOW)


@pytest.mark.parametrize("result,reason", [
    (sparse_response(), "missing dates"),
    (response(current=2, baseline=0), "undefined"),
])
def test_successful_but_insufficient_sms_statistics_do_not_trigger_collection_failure(store, result, reason):
    service = service_for(store, [result])
    assert service.collect(JOB, NOW) is True
    status = store.status()["jobs"][0]
    assert status["status"] == "insufficient_data"
    assert status["sample_count"] == 0
    assert reason in status["detail"]
    assert not store.latest_samples([JOB["id"]])
    assert not store.list_messages()
    assert not active_rules(store)
    exported = metric_samples(service, NOW)
    assert not exported["cloud_monitor_resource_value"]
    assert [(sample.labels["status"], sample.value)
            for sample in exported["cloud_monitor_job_status"] if sample.value] == [("insufficient_data", 1)]


def test_successful_empty_sms_response_remains_no_data_without_zero_growth(store):
    service = service_for(store, [{"Code": "OK", "Data": {"TotalSize": 0, "TargetList": []}}])
    assert service.collect(JOB, NOW) is True
    assert store.status()["jobs"][0]["status"] == "no_data"
    assert not store.latest_samples([JOB["id"]])
    assert not store.list_messages()


def test_successful_insufficient_sms_statistics_keep_service_ready(store, monkeypatch):
    service = service_for(store, [sparse_response()])
    assert service.collect(JOB, NOW)
    service.started = True
    service.last_tick = NOW
    monkeypatch.setattr("cloud_monitor.service.utcnow", lambda: NOW)
    server = make_server(service, "127.0.0.1", 0)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request("GET", "/readyz", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        result = connection.getresponse()
        state = json.loads(result.read())
        assert result.status == 200
        assert state["scheduler_alive"] is True
        assert state["jobs"][0]["status"] == "insufficient_data"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()


def invalid_response(case):
    result = response()
    rows = result["Data"]["TargetList"]
    if case == "pagination":
        return {"Code": "OK", "Data": {"TotalSize": 1, "TargetList": []}}
    if case == "negative":
        rows[0]["TotalCount"] = -1
    elif case == "duplicate":
        rows.append(deepcopy(rows[0]))
        result["Data"]["TotalSize"] = len(rows)
    elif case == "invalid-date":
        rows[0]["SendDate"] = "20261301"
    elif case == "out-of-window-date":
        rows[0]["SendDate"] = "20261019"
    return result


@pytest.mark.parametrize("failure", [
    PermissionError("AccessDenied"),
    TimeoutError("request timed out"),
    RuntimeError("Alibaba Cloud dysmsapi.QuerySendStatistics failed: code=Forbidden"),
    *[invalid_response(case) for case in (
        "pagination", "negative", "duplicate", "invalid-date", "out-of-window-date",
    )],
], ids=["permissions", "transport", "api-business", "pagination", "negative", "duplicate",
        "invalid-date", "out-of-window-date"])
def test_actual_sms_collection_errors_still_trigger_failure(store, failure):
    service = service_for(store, [failure])
    assert service.collect(JOB, NOW) is False
    assert store.status()["jobs"][0]["status"] == "error"
    assert active_rules(store) == {"collection-error"}
    assert not store.latest_samples([JOB["id"]])
    event = json.loads(store.list_messages()[0]["payload"])
    assert (event["metric"], event["phase"], event["value"]) == ("collection_failed", "firing", 1)


def test_insufficient_statistics_hide_previous_growth_without_resolving_business_alert(store):
    service = service_for(store, [response(), sparse_response(), response(current=400)])
    assert service.collect(JOB, NOW)
    assert active_rules(store) == {"sms-growth"}
    assert metric_samples(service, NOW)["cloud_monitor_resource_value"][0].value == 200
    insufficient_at = NOW + timedelta(minutes=1)
    assert service.collect(JOB, insufficient_at)
    assert store.status()["jobs"][0]["status"] == "insufficient_data"
    assert active_rules(store) == {"sms-growth"}
    assert len(store.list_messages()) == 1
    assert len(store.latest_samples([JOB["id"]])) == 1
    exported = metric_samples(service, insufficient_at)
    assert not exported["cloud_monitor_resource_value"]
    assert exported["cloud_monitor_resource_fresh"][0].value == 0
    assert len(exported["cloud_monitor_active_alert"]) == 1
    complete_at = NOW + timedelta(minutes=2)
    assert service.collect(JOB, complete_at)
    assert store.status()["jobs"][0]["status"] == "ok"
    assert store.status()["jobs"][0]["detail"] == ""
    assert metric_samples(service, complete_at)["cloud_monitor_resource_value"][0].value == 300


def test_insufficient_sms_growth_stays_hidden_across_restart_and_transport_error(store):
    service = service_for(store, [response(), sparse_response()])
    assert service.collect(JOB, NOW)
    assert metric_samples(service, NOW)["cloud_monitor_resource_value"][0].value == 200
    insufficient_at = NOW + timedelta(minutes=1)
    assert service.collect(JOB, insufficient_at)
    assert not metric_samples(service, insufficient_at)["cloud_monitor_resource_value"]

    restarted = service_for(store, [TimeoutError("temporary failure"), response(current=500)])
    assert not metric_samples(restarted, insufficient_at)["cloud_monitor_resource_value"]
    failed_at = NOW + timedelta(minutes=2)
    assert restarted.collect(JOB, failed_at) is False
    assert store.status()["jobs"][0]["status"] == "error"
    assert not metric_samples(restarted, failed_at)["cloud_monitor_resource_value"]
    assert active_rules(store) == {"sms-growth", "collection-error"}

    complete_at = NOW + timedelta(minutes=3)
    assert restarted.collect(JOB, complete_at)
    assert store.status()["jobs"][0]["status"] == "ok"
    assert metric_samples(restarted, complete_at)["cloud_monitor_resource_value"][0].value == 400


def test_collection_failure_recovers_after_two_successful_insufficient_queries(store):
    service = service_for(store, [
        response(), TimeoutError("temporary failure"), sparse_response(), sparse_response(),
    ])
    assert service.collect(JOB, NOW)
    assert not service.collect(JOB, NOW + timedelta(minutes=1))
    assert active_rules(store) == {"sms-growth", "collection-error"}
    for row in store.list_messages():
        store.delivered(row["id"])
    assert service.collect(JOB, NOW + timedelta(minutes=2))
    assert active_rules(store) == {"sms-growth", "collection-error"}
    assert service.collect(JOB, NOW + timedelta(minutes=3))
    assert active_rules(store) == {"sms-growth"}
    assert store.status()["jobs"][0]["status"] == "insufficient_data"
    events = [json.loads(row["payload"]) for row in store.list_messages()]
    assert [(event["metric"], event["phase"]) for event in events] == [
        ("sms_growth_percent", "firing"),
        ("collection_failed", "firing"),
        ("collection_failed", "recovery"),
    ]
