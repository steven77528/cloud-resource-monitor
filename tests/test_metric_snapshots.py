from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import threading

import pytest
from sqlalchemy import func, select

from cloud_monitor.models import Metric
from cloud_monitor.store import LATEST_SAMPLES, SAMPLES, Store


NOW = datetime(2026, 9, 10, 0, 0, 0, 123456, tzinfo=timezone.utc)


def sample(value, seconds=0, resource_id="ecs-1"):
    return Metric("aliyun", "test", resource_id, "Test resource", "expiry_days", value, "days",
                  NOW + timedelta(seconds=seconds), {"region": "cn-hangzhou"})


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "snapshot.db")
    try:
        yield result
    finally:
        result.dispose()


def history(store):
    with store.connect() as connection:
        return [dict(row) for row in connection.execute(select(SAMPLES)).mappings()]


def test_latest_sample_deduplicates_unordered_batches_and_preserves_history(store):
    first, second, newest = sample(30), sample(29, 60), sample(28, 120)
    store.save_samples("job-1", [second, first, newest, newest, first])
    assert len(history(store)) == 3
    latest = store.latest_samples(["job-1"])
    assert latest == [{"job_id": "job-1", "metric_key": newest.key(),
                       "observed_at": newest.observed_at.timestamp(), "payload": newest.to_dict()}]


def test_older_overlap_and_equal_timestamp_do_not_replace_persisted_newest(store):
    newest = sample(28, 120)
    store.save_samples("job-1", [newest])
    store.save_samples("job-1", [sample(31), sample(999, 120), sample(30, 60)])
    assert len(history(store)) == 3
    assert store.latest_samples(["job-1"])[0]["payload"] == newest.to_dict()
    latest_history = max(history(store), key=lambda row: row["observed_at"])
    assert json.loads(latest_history["payload"]) == newest.to_dict()


def test_latest_sample_identity_includes_job_and_metric_and_filters_since(store):
    store.save_samples("job-1", [sample(30), sample(40, 60, resource_id="ecs-2")])
    store.save_samples("job-2", [sample(20, 120)])
    store.save_samples("job-1", [])
    assert len(store.latest_samples(["job-1", "job-2"])) == 3
    assert len(store.latest_samples(["job-1"])) == 2
    assert store.latest_samples(["job-2"])[0]["payload"]["value"] == 20
    assert store.latest_samples([]) == []
    assert store.latest_samples(["unknown"]) == []
    cutoff = NOW + timedelta(seconds=60)
    assert [row["payload"]["value"] for row in store.latest_samples(["job-1"], cutoff)] == [40]
    assert store.latest_samples(["job-1"], cutoff.timestamp()) == store.latest_samples(["job-1"], cutoff)


def test_latest_samples_survive_restart_without_reading_history(store):
    store.save_samples("job-1", [sample(25)])
    path = store.path
    store.dispose()
    restarted = Store(path)
    try:
        # The exporter must remain usable independently of the growing history table.
        with restarted.connect() as connection:
            SAMPLES.drop(connection)
        assert restarted.latest_samples(["job-1"])[0]["payload"] == sample(25).to_dict()
    finally:
        restarted.dispose()


def test_snapshot_table_is_added_without_history_backfill(store):
    store.save_samples("job-1", [sample(25)])
    with store.connect() as connection:
        LATEST_SAMPLES.drop(connection)
    path = store.path
    store.dispose()
    restarted = Store(path)
    try:
        assert len(history(restarted)) == 1
        assert restarted.latest_samples(["job-1"]) == []
        restarted.save_samples("job-1", [sample(24, 60)])
        assert restarted.latest_samples(["job-1"])[0]["payload"]["value"] == 24
    finally:
        restarted.dispose()


def test_prune_removes_expired_history_and_snapshots_at_same_cutoff(store):
    expired = -30 * 86400 - 1
    boundary = -30 * 86400
    store.save_samples("old-job", [sample(30, expired)])
    store.save_samples("active-job", [sample(30, expired), sample(29, boundary),
                                     sample(40, resource_id="ecs-2")])
    store.prune(NOW, retention_days=30)
    assert len(history(store)) == 2
    assert store.latest_samples(["old-job"]) == []
    assert len(store.latest_samples(["active-job"])) == 2
    assert min(row["observed_at"] for row in store.latest_samples(["active-job"])) == (
        NOW - timedelta(days=30)).timestamp()


def test_history_and_latest_values_rollback_together(store, monkeypatch):
    store.save_samples("job-1", [sample(30)])
    original_upsert = store._upsert_latest_samples

    def fail_after_snapshot_write(connection, rows):
        original_upsert(connection, rows)
        raise RuntimeError("injected snapshot write failure")

    monkeypatch.setattr(store, "_upsert_latest_samples", fail_after_snapshot_write)
    with pytest.raises(RuntimeError, match="injected snapshot write failure"):
        store.save_samples("job-1", [sample(29, 60), sample(50, 60, resource_id="ecs-2")])
    assert len(history(store)) == 1
    assert [row["payload"]["value"] for row in store.latest_samples(["job-1"])] == [30]


def test_multiple_store_writers_preserve_latest_snapshot(store):
    contender = Store(store.path)
    ready = threading.Barrier(2)

    def write(target, newer_first):
        ready.wait(timeout=5)
        offsets = range(20, -1, -1) if newer_first else range(21)
        for offset in offsets:
            target.save_samples("job-1", [sample(offset, offset)])

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, store, True), pool.submit(write, contender, False)]
            for future in futures:
                future.result(timeout=30)
        assert store.latest_samples(["job-1"])[0]["payload"] == sample(20, 20).to_dict()
        with store.connect() as connection:
            assert connection.execute(select(func.count()).select_from(SAMPLES)).scalar_one() == 21
    finally:
        contender.dispose()
