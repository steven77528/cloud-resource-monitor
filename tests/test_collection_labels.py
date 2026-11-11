from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest

from cloud_monitor.config import validate_config
from cloud_monitor.engine import Engine
from cloud_monitor.models import Metric
from cloud_monitor.service import COLLECTION_RULE, Service
from cloud_monitor.store import Store


NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)
ACCOUNT = {"provider": "aliyun", "id": "test", "enabled": True}


def health_metric(kind, options=None):
    service = Service.__new__(Service)
    service.engine = Mock()
    job = {"id": "internal-job-id", "kind": kind, "options": options or {}}
    service._collection_health(job, ACCOUNT, True, NOW)
    return service.engine.evaluate.call_args.args[1]


@pytest.mark.parametrize("kind,label", [
    ("ecs_expiry", "ECS 云服务器到期采集"),
    ("rds_expiry", "RDS 数据库到期采集"),
    ("certificates", "SSL 证书到期采集"),
    ("balance", "账号余额采集"),
    ("sms_statistics", "短信发送量统计采集"),
])
def test_collection_failure_identifies_resource_type_without_job_name(kind, label):
    metric = health_metric(kind)
    assert metric.resource_name == label
    assert metric.resource_id == "internal-job-id"
    assert metric.dimensions == {"collection_kind": kind}


def test_metric_collection_scope_names_resources_and_deduplicates_directions():
    incoming = {"namespace": "acs_alb", "resource_id": "alb-123", "resource_name": "生产入口",
                "metric_name": "TrafficRX"}
    metric = health_metric("metrics", {"metrics": [incoming, {**incoming, "metric_name": "TrafficTX"}]})
    assert metric.resource_name == "ALB 负载均衡指标采集（配置范围：生产入口 (alb-123)）"
    assert "internal-job-id" not in metric.resource_name


def test_metric_collection_scope_falls_back_to_dimension_and_limits_large_batches():
    entries = [{"namespace": "acs_oss_dashboard", "dimensions": {"BucketName": f"bucket-{index}"}}
               for index in range(5)]
    metric = health_metric("metrics", {"metrics": entries})
    assert metric.resource_name == "OSS 对象存储指标采集（配置范围：bucket-0、bucket-1、bucket-2 等 5 个资源）"
    assert "bucket-4" not in metric.resource_name


def test_huawei_bandwidth_scope_uses_its_own_resource_type():
    metric = health_metric("metrics", {"metrics": [{"namespace": "SYS.VPC", "resource_id": "bandwidth-123"}]})
    assert metric.resource_name == "公网 IP 与带宽指标采集（配置范围：bandwidth-123）"


def test_configured_certificate_scope_keeps_real_resource_identifiers():
    metric = health_metric("certificates", {"certificate_ids": [1, 2]})
    assert metric.resource_name == "SSL 证书到期采集（配置范围：1、2）"


def test_display_metadata_preserves_existing_collection_alert_identity():
    old = Metric("aliyun", "test", "internal-job-id", "internal-job-id",
                 "collection_failed", 1, "bool", NOW)
    new = health_metric("metrics", {"metrics": [{"namespace": "acs_alb", "resource_id": "alb-123"}]})
    assert old.key() == new.key()


def test_existing_collection_failure_recovers_after_display_upgrade(tmp_path):
    job = {"id": "internal-job-id", "account": "test", "kind": "metrics", "interval_seconds": 60,
           "options": {"metrics": [{"namespace": "acs_alb", "resource_id": "alb-123"}]}}
    config = validate_config({"version": 1, "accounts": [ACCOUNT], "jobs": [job], "rules": []})
    store = Store(tmp_path / "state.db")
    old_metric = Metric("aliyun", "test", job["id"], job["id"], "collection_failed", 1, "bool", NOW)
    Engine(store, config).evaluate(job, old_metric, NOW, rules=[COLLECTION_RULE])
    store.delivered(store.list_messages()[0]["id"])
    service = Service(config, store, notifier=Mock())
    for seconds in (60, 120):
        service._collection_health(job, ACCOUNT, False, NOW + timedelta(seconds=seconds))
    rows = store.list_messages()
    assert len(rows) == 2
    recovery = json.loads(rows[-1]["payload"])
    assert recovery["phase"] == "recovery"
    assert recovery["resource_id"] == job["id"]
    assert recovery["resource_name"] == "ALB 负载均衡指标采集（配置范围：alb-123）"
    assert not store.status()["active_alerts"]
