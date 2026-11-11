from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
import yaml

from cloud_monitor.config import validate_config
from cloud_monitor.diagnostics import diagnose_jobs
from cloud_monitor.models import Metric
from cloud_monitor.providers import relay
from cloud_monitor.providers.common import ProviderError
from cloud_monitor.service import Service
from cloud_monitor.store import Store


NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)
TOKEN = "offline-relay-token-never-for-a-vendor"


def config(tmp_path, provider="huifu", kind="balance"):
    path = tmp_path / "relay.token"
    path.write_text(TOKEN, encoding="ascii")
    public = {"qiniu": {}, "huifu": {"huifu_id": "merchant-id"},
              "asign": {"api_mode": "openapi", "app_id": "offline-app"}, "wechat": {"appid": "offline-app"}}
    options = {"qiniu": {}, "huifu": {"amount_unit_confirmed": True, "currency": "CNY",
        "source_amount_unit": "yuan", "amount_multiplier": "1", "accounts": [{"acct_id": "main", "acct_type": "01"}]},
        "asign": {"resource_id": "company", "selector": {"account": "company"}},
        "wechat": {"cgi_paths": ["/cgi-bin/message/subscribe/send"]}}
    account = {"id": "business", "provider": provider, "enabled": True, **public[provider], "relay": {
        "url": "https://relay.example.invalid/v1/monitor/collect", "token_file": str(path)}}
    job = {"id": "query", "account": "business", "kind": kind, "interval_seconds": 60,
        "max_age_seconds": 120, "options": {"operation": "read", "collector": options[provider]}}
    return validate_config({"version": 1, "accounts": [account], "jobs": [job], "rules": [
        {"id": "balance-low", "metric": "huifu_available_balance", "levels": [
            {"severity": "P1", "op": "lt", "threshold": 100}]},
        {"id": "signature", "metric": "api_signature_failed", "levels": [
            {"severity": "P1", "op": "eq", "threshold": 1}]}], "notifications": {"sms": {"enabled": False}}})


def response(payload):
    identity = {key: value for key, value in payload.items() if key != "request"}
    provider = payload["provider"]
    if provider == "qiniu":
        raw = {"code": 0, "data": {"account_id": "finance", "currency": "CNY", "cash_balance": 20000000000,
            "present_balance": 0, "credit_line": 0, "unpaid_money": 0}}
    elif provider == "huifu":
        request = payload["request"]["body"]
        raw = {"data": {"req_date": request["req_date"], "req_seq_id": request["req_seq_id"], "resp_code": "00000000",
            "acctInfo_list": json.dumps([{"huifu_id": "merchant-id", "acct_id": "main", "acct_type": "01",
                "acct_stat": "N", "avl_bal": "200", "balance_amt": "210", "frz_bal": "10"}])}}
    elif provider == "asign":
        raw = {"code": 100000, "data": [{"account": "company", "userType": 1, "status": 1}]}
    else:
        raw = {"errcode": 0, "quota": {"daily_limit": 1000, "used": 250, "remain": 750}}
    return {**identity, "status": "ok", "signature_verified": True if provider == "huifu" else None, "response": raw}


class NoDelivery:
    def send(self, *_):
        raise AssertionError("Offline tests cannot send notifications")


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "monitor.db")
    yield result
    result.dispose()


@pytest.mark.parametrize("provider,kind", [("huifu", "balance"), ("qiniu", "balance"),
    ("asign", "company_verification"), ("wechat", "api_quota")])
def test_service_and_diagnostics_forward_then_parse_locally(tmp_path, monkeypatch, provider, kind, store):
    cfg = config(tmp_path, provider, kind)
    calls = []

    def transport(method, url, **kwargs):
        payload = json.loads(kwargs["body"])
        calls.append(payload)
        assert method == "POST" and url == cfg["accounts"][0]["relay"]["url"]
        assert payload["version"] == 2 and set(payload["request"]) == {"method", "url", "body"}
        assert TOKEN not in kwargs["body"].decode()
        return response(payload)

    monkeypatch.setattr(relay, "request_json", transport)
    service = Service(cfg, store, notifier=NoDelivery())
    assert service.collect(cfg["jobs"][0], NOW)
    assert store.status()["jobs"][0]["status"] == "ok"
    assert {row["payload"]["provider"] for row in store.latest_samples(["query"])} == {provider}
    assert list(diagnose_jobs(cfg, now=NOW))[0]["status"] == "ok"
    assert len(calls) == 2 and calls[0]["request_id"] != calls[1]["request_id"]


def test_invalid_trailing_local_metric_cannot_partially_alert(tmp_path, monkeypatch, store):
    from cloud_monitor.providers.huifu import HuifuCollector

    cfg = config(tmp_path)
    samples = [Metric("huifu", "business", resource, "Business", "huifu_available_balance", 5, "CNY",
        NOW - timedelta(seconds=age), {"source": "official_api"}) for resource, age in (("first", 0), ("second", 121))]
    monkeypatch.setattr(HuifuCollector, "collect", lambda *_: samples)
    service = Service(cfg, store, notifier=NoDelivery())
    assert not service.collect(cfg["jobs"][0], NOW)
    assert {row["rule_id"] for row in store.status()["active_alerts"]} == {"collection-error"}
    assert store.status()["jobs"][0]["detail"] == "ProviderError: source_data_stale"


def test_signature_recovery_requires_real_queries_not_empty_local_manifest(tmp_path, monkeypatch, store):
    cfg = config(tmp_path, kind="settlements")
    manifest = tmp_path / "orders.json"
    cfg["jobs"][0]["options"]["collector"] = {"orders_file": str(manifest)}
    current = {"mode": "signature"}
    calls = []

    def transport(_, __, **kwargs):
        payload = json.loads(kwargs["body"])
        identity = {key: value for key, value in payload.items() if key != "request"}
        calls.append(payload)
        if current["mode"] == "signature":
            return {**identity, "status": "error", "reason": "signature_invalid"}
        if current["mode"] == "network":
            raise RuntimeError(TOKEN)
        request = payload["request"]["body"]
        return {**identity, "status": "ok", "signature_verified": True, "response": {"data": {
            "resp_code": "00000000", "org_req_date": request["org_req_date"],
            "org_req_seq_id": request["org_req_seq_id"], "trans_status": "S", "re_exchange": "N"}}}

    monkeypatch.setattr(relay, "request_json", transport)
    service = Service(cfg, store, notifier=NoDelivery())
    for index, mode in enumerate(("signature", "empty", "network", "verified", "verified")):
        current["mode"] = mode
        now = NOW + timedelta(seconds=60 * index)
        manifest.write_text(json.dumps({"updated_at": now.isoformat(), "orders": [] if mode == "empty" else [
            {"resource_id": "order", "org_req_date": "20260915", "org_req_seq_id": "sequence", "created_at": NOW.isoformat()}]}))
        assert service.collect(cfg["jobs"][0], now) == (mode in {"empty", "verified"})
        assert ("signature" in {row["rule_id"] for row in store.status()["active_alerts"]}) == (index < 4)
    assert len(calls) == 4
    assert TOKEN not in json.dumps(store.status()) and TOKEN not in json.dumps(store.list_messages())


def test_shipped_configs_keep_credentials_out_and_business_options_local():
    root = Path(__file__).resolve().parents[1]
    forbidden = {"access_key_id", "access_key_secret", "private_key_file", "huifu_public_key_file", "token_file"}
    for provider in ("asign", "huifu", "qiniu", "wechat"):
        data = yaml.safe_load((root / f"config/integrations/{provider}.yaml").read_text(encoding="utf-8"))
        for account in data["accounts"]:
            assert not forbidden.intersection(account)
        for job in data["jobs"]:
            if job["kind"] not in relay.LOCAL_KINDS:
                assert "operation" in job["options"] and isinstance(job["options"].get("collector"), dict)


def test_preflight_rejects_vendor_secret_without_reading_it(tmp_path):
    cfg = config(tmp_path)
    cfg["accounts"][0]["access_key_secret"] = "synthetic-private-value"
    with pytest.raises(ProviderError, match="relay_account_fields_forbidden"):
        list(diagnose_jobs(cfg, now=NOW))
