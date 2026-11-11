"""Opt-in integration tests against an actual MySQL server.

MYSQL_TEST_DSN must point at an isolated test server using an account permitted
to create/drop databases. Every test creates a random database and drops only
that database during cleanup. No tables in the DSN's original database change.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.engine import make_url

from cloud_monitor.config import validate_config
from cloud_monitor.engine import Engine
from cloud_monitor.models import Metric
from cloud_monitor.providers.common import ProviderError
from cloud_monitor.service import Service
from cloud_monitor.store import Store


MYSQL_TEST_DSN = os.environ.get("MYSQL_TEST_DSN")
pytestmark = pytest.mark.skipif(not MYSQL_TEST_DSN, reason="MYSQL_TEST_DSN not configured")
NOW = datetime(2026, 9, 8, 0, 0, 0, 123456, tzinfo=timezone.utc)
JOB = {"id": "mysql-expiry-job", "account": "test", "kind": "ecs_expiry", "interval_seconds": 60}
RULE = {"id": "expiry", "metric": "expiry_days", "levels": [
    {"severity": "P2", "op": "le", "threshold": 60},
    {"severity": "P1", "op": "le", "threshold": 30},
    {"severity": "P0", "op": "le", "threshold": 7},
]}


def config():
    return validate_config({"version": 1, "rules": [deepcopy(RULE)], "jobs": [JOB],
                            "accounts": [{"id": "test", "provider": "aliyun", "enabled": True}]})


def sample(value, seconds=0):
    return Metric("aliyun", "test", "ecs-1", "\u751f\u4ea7\u4e91\u4e3b\u673a", "expiry_days", value, "days",
                  NOW + timedelta(seconds=seconds), {"region": "cn-hangzhou"})


@pytest.fixture
def mysql_store():
    admin_url = make_url(MYSQL_TEST_DSN)
    if admin_url.get_backend_name() != "mysql":
        pytest.fail("MYSQL_TEST_DSN must use a MySQL SQLAlchemy URL")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    database_name = f"cloud_monitor_test_{uuid.uuid4().hex}"
    identifier = admin.dialect.identifier_preparer.quote_identifier(database_name)
    created = False
    store = None
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f"CREATE DATABASE {identifier} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        created = True
        database_url = admin_url.set(database=database_name).render_as_string(hide_password=False)
        store = Store(database_url)
        yield store, database_url
    finally:
        if store is not None:
            store.dispose()
        if created:
            with admin.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE {identifier}")
        admin.dispose()


def test_mysql_restart_persists_state_pending_outbox_and_utf8_resource(mysql_store):
    store, database_url = mysql_store
    Engine(store, config()).evaluate(JOB, sample(25), NOW)
    original = store.due_messages(NOW)
    assert len(original) == 1 and original[0]["channel"] == "dingtalk"
    store.job_status(JOB["id"], "ok", NOW, count=1)
    store.dispose()

    restarted = Store(database_url)
    try:
        assert restarted.due_messages(NOW)[0]["id"] == original[0]["id"]
        active = restarted.status()["active_alerts"]
        assert len(active) == 1 and active[0]["active"] == "P1"
        assert active[0]["metric"]["resource_name"] == "\u751f\u4ea7\u4e91\u4e3b\u673a"
        assert restarted.status()["jobs"][0]["sample_count"] == 1
        Engine(restarted, config()).evaluate(JOB, sample(24, 60), NOW + timedelta(seconds=60))
        assert len(restarted.list_messages()) == 1
    finally:
        restarted.dispose()


def test_mysql_highest_severity_escalation_cancels_obsolete_outbox(mysql_store):
    store, _ = mysql_store
    engine = Engine(store, config())
    engine.evaluate(JOB, sample(20), NOW)
    previous_id = store.due_messages(NOW)[0]["id"]
    engine.evaluate(JOB, sample(0, 60), NOW + timedelta(seconds=60))
    due = store.due_messages(NOW + timedelta(seconds=60))
    assert len(due) == 1 and due[0]["channel"] == "sms"
    assert json.loads(due[0]["payload"])["severity"] == "P0"
    assert not store.still_pending(previous_id)
    assert store.status()["outbox"] == {"cancelled": 1, "pending": 1}
    assert len(store.status()["active_alerts"]) == 1
    assert store.status()["active_alerts"][0]["active"] == "P0"


def test_mysql_receipt_retries_are_idempotent_and_account_windows_are_isolated(mysql_store):
    store, database_url = mysql_store
    rows = [("message-1", "test", NOW.timestamp(), 1),
            ("message-2", "test", NOW.timestamp(), 0),
            ("message-3", "other-account", NOW.timestamp(), 1)]
    assert store.add_receipts(rows) == 3
    assert store.add_receipts(rows) == 0
    restarted = Store(database_url)
    try:
        assert restarted.add_receipts([rows[0]]) == 0
        summary = restarted.receipt_summary("test", NOW - timedelta(seconds=1), NOW + timedelta(seconds=1))
        assert summary["total"] == 2 and summary["success"] == 1
        assert summary["latest"] == pytest.approx(NOW.timestamp(), abs=0.000001)
        assert restarted.receipt_summary("absent", NOW, NOW + timedelta(seconds=1))["total"] == 0
    finally:
        restarted.dispose()


def test_mysql_sample_dedup_preserves_microseconds_and_job_upsert(mysql_store):
    from cloud_monitor.store import JOBS, SAMPLES

    store, _ = mysql_store
    first = sample(25)
    second = sample(25, seconds=0.0001)
    store.save_samples(JOB["id"], [first, first])
    store.save_samples(JOB["id"], [first, second])
    store.job_status(JOB["id"], "error", NOW, count=0, detail="temporary failure")
    store.job_status(JOB["id"], "ok", NOW + timedelta(seconds=60), count=2, detail="")
    with store.connect() as connection:
        assert connection.execute(select(func.count()).select_from(SAMPLES)).scalar_one() == 2
        jobs = connection.execute(select(JOBS)).mappings().all()
        assert len(jobs) == 1
        assert jobs[0]["status"] == "ok" and jobs[0]["sample_count"] == 2
        assert jobs[0]["detail"] == ""


def test_mysql_latest_samples_preserve_newest_payload_across_overlap_restart_and_retention(mysql_store):
    store, database_url = mysql_store
    newest = sample(24, seconds=0.0001)
    store.save_samples(JOB["id"], [newest, sample(25)])
    store.save_samples(JOB["id"], [sample(999, seconds=0.0001), sample(26, seconds=-60)])
    store.save_samples("other-job", [sample(40)])
    store.save_samples("expired-job", [sample(40, seconds=-31 * 86400)])
    restarted = Store(database_url)
    try:
        latest = restarted.latest_samples([JOB["id"]])
        assert len(latest) == 1 and latest[0]["payload"] == newest.to_dict()
        assert latest[0]["observed_at"] == pytest.approx(newest.observed_at.timestamp(), abs=0.000001)
        assert restarted.latest_samples(["other-job"])[0]["payload"]["value"] == 40
        assert restarted.latest_samples([JOB["id"]], newest.observed_at) == latest
        restarted.prune(NOW, retention_days=30)
        assert restarted.latest_samples(["expired-job"]) == []
        assert restarted.latest_samples([JOB["id"]]) == latest
    finally:
        restarted.dispose()


def test_mysql_sample_history_and_latest_snapshot_rollback_together(mysql_store, monkeypatch):
    from cloud_monitor.store import SAMPLES

    store, _ = mysql_store
    store.save_samples(JOB["id"], [sample(25)])
    original_upsert = store._upsert_latest_samples

    def fail_after_snapshot_write(connection, rows):
        original_upsert(connection, rows)
        raise RuntimeError("injected snapshot write failure")

    monkeypatch.setattr(store, "_upsert_latest_samples", fail_after_snapshot_write)
    with pytest.raises(RuntimeError, match="injected snapshot write failure"):
        store.save_samples(JOB["id"], [sample(24, 60)])
    assert store.latest_samples([JOB["id"]])[0]["payload"] == sample(25).to_dict()
    with store.connect() as connection:
        assert connection.execute(select(func.count()).select_from(SAMPLES)).scalar_one() == 1


def test_mysql_delivery_backoff_survives_restart_and_recovers(mysql_store):
    store, database_url = mysql_store
    Engine(store, config()).evaluate(JOB, sample(0), NOW)
    event_id = store.due_messages(NOW)[0]["id"]
    store.delivery_failed(event_id, NOW, 2, "Timeout")
    restarted = Store(database_url)
    try:
        assert not restarted.due_messages(NOW + timedelta(seconds=29))
        assert restarted.due_messages(NOW + timedelta(seconds=30))[0]["attempts"] == 1
        restarted.delivery_failed(event_id, NOW + timedelta(seconds=30), 2, "Timeout")
        assert restarted.status()["outbox"] == {"failed": 1}
        assert restarted.retry_failed(NOW + timedelta(seconds=90)) == 1
        restarted.delivered(event_id)
        assert not restarted.still_pending(event_id)
        assert restarted.status()["outbox"] == {"sent": 1}
    finally:
        restarted.dispose()


def test_mysql_skipped_delivery_persists_without_migration_retry_or_retention_leak(mysql_store):
    store, database_url = mysql_store
    old = NOW - timedelta(days=31)
    before_columns = [(column["name"], str(column["type"]), column["nullable"])
                      for column in inspect(store.engine).get_columns("outbox")]
    event_ids = {name: str(uuid.uuid4()) for name in ("pending_sms", "failed_sms", "recent_sms", "failed_email")}
    with store.connect() as connection:
        for name, event_id in event_ids.items():
            store.enqueue(connection, name, "email" if name == "failed_email" else "sms",
                          {"id": event_id, "summary": "\u4e91\u8d44\u6e90\u544a\u8b66"},
                          NOW if name == "recent_sms" else old,
                          status="skipped" if name == "recent_sms" else "pending")
    store.delivery_failed(event_ids["failed_sms"], old, 1, "Timeout")
    store.delivery_failed(event_ids["failed_email"], old, 1, "Timeout")
    assert store.skip_channel("sms") == 2
    assert store.skip_channel("sms") == 0
    store.dispose()

    restarted = Store(database_url)
    try:
        assert restarted.status()["outbox"] == {"skipped": 3, "failed": 1}
        rows = {row["id"]: row for row in restarted.list_messages()}
        assert rows[event_ids["failed_sms"]]["last_error"] == "channel_disabled"
        assert rows[event_ids["failed_sms"]]["attempts"] == 1
        assert restarted.retry_failed(NOW) == 1
        assert [row["id"] for row in restarted.due_messages(NOW)] == [event_ids["failed_email"]]
        restarted.delivery_failed(event_ids["failed_sms"], NOW, 3, "Timeout")
        assert not restarted.still_pending(event_ids["failed_sms"])
        restarted.prune(NOW, retention_days=30)
        remaining = {row["id"] for row in restarted.list_messages()}
        assert remaining == {event_ids["recent_sms"], event_ids["failed_email"]}
        assert restarted.status()["outbox"] == {"skipped": 1, "pending": 1}
        after_columns = [(column["name"], str(column["type"]), column["nullable"])
                         for column in inspect(restarted.engine).get_columns("outbox")]
        assert after_columns == before_columns
    finally:
        restarted.dispose()


def test_mysql_database_lock_allows_one_scheduler_and_releases_on_exit(mysql_store):
    store, database_url = mysql_store
    contender = Store(database_url)
    try:
        with store.acquire_lock():
            assert store.check_lock()
            with pytest.raises(RuntimeError):
                with contender.acquire_lock():
                    pytest.fail("A second scheduler acquired the same MySQL advisory lock")
        with contender.acquire_lock():
            assert contender.check_lock()
        with pytest.raises(LookupError):
            with store.acquire_lock():
                raise LookupError("simulate scheduler failure")
        with contender.acquire_lock():
            assert contender.check_lock()
    finally:
        contender.dispose()


def test_mysql_alert_state_and_outbox_rollback_together(mysql_store, monkeypatch):
    from cloud_monitor.store import OUTBOX, STATES

    store, _ = mysql_store
    original_save = store.save_state

    def fail_save(*args):
        raise RuntimeError("injected state write failure")

    monkeypatch.setattr(store, "save_state", fail_save)
    with pytest.raises(RuntimeError, match="injected state write failure"):
        Engine(store, config()).evaluate(JOB, sample(0), NOW)
    with store.connect() as connection:
        assert connection.execute(select(func.count()).select_from(STATES)).scalar_one() == 0
        assert connection.execute(select(func.count()).select_from(OUTBOX)).scalar_one() == 0
    monkeypatch.setattr(store, "save_state", original_save)
    Engine(store, config()).evaluate(JOB, sample(0), NOW)
    assert len(store.due_messages(NOW)) == 1
    assert store.status()["active_alerts"][0]["active"] == "P0"


def test_mysql_failed_collection_breaks_consecutive_samples_across_restart(mysql_store):
    store, database_url = mysql_store
    job = {"id": "huifu-balance", "account": "test", "kind": "balance", "interval_seconds": 60,
           "collection_error_samples": 3}
    cfg = validate_config({
        "version": 1, "accounts": [{"id": "test", "provider": "huifu", "enabled": True}], "jobs": [job],
        "rules": [{"id": "low-balance", "metric": "huifu_available_balance",
                   "levels": [{"severity": "P1", "op": "lt", "threshold": 100, "for_samples": 2}]}],
    })

    class Collector:
        def collect(self, kind, options, now):
            if now == NOW + timedelta(seconds=60):
                raise ProviderError("transport_error")
            return [Metric("huifu", "test", "account-1", "Account", "huifu_available_balance", 10, "CNY", now)]

    service = Service(cfg, store, collector_factory=lambda _: Collector())
    assert service.collect(job, NOW)
    assert not service.collect(job, NOW + timedelta(seconds=60))
    store.dispose()

    restarted = Store(database_url)
    try:
        service = Service(cfg, restarted, collector_factory=lambda _: Collector())
        assert service.collect(job, NOW + timedelta(seconds=120))
        assert not restarted.status()["active_alerts"]
        assert service.collect(job, NOW + timedelta(seconds=180))
        assert {state["rule_id"] for state in restarted.status()["active_alerts"]} == {"low-balance"}
        assert len(restarted.due_messages(NOW + timedelta(seconds=180))) == 1
    finally:
        restarted.dispose()
