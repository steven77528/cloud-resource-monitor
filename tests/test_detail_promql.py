"""Optional offline Prometheus evaluation of the actual detail dashboard queries."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "update_detail_dashboard_data", ROOT / "scripts/update_detail_dashboard_data.py"
)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PROMTOOL_TESTS") != "1",
    reason="Opt in to local offline Docker promtool",
)


def _core(uid, panel_id):
    backup = json.loads((ROOT / "grafana/backups" / f"{uid}.before.json").read_text(encoding="utf-8"))
    dashboard = migration.update_dashboard(backup)
    query = dashboard["spec"]["elements"][f"panel-{panel_id}"]["spec"]["data"]["spec"]["queries"][0]
    expression = query["spec"]["query"]["spec"]["expr"]
    assert expression.startswith(migration.WRAPPED_CORE_START)
    core = expression[len(migration.WRAPPED_CORE_START):expression.index(migration.WRAPPED_CORE_END)]
    core = core.replace("$__rate_interval", "1m").replace("${environment:regex}", ".*")
    for variable in ("cluster", "job", "instance", "topic", "consumergroup"):
        core = core.replace("$" + variable, ".*")
    assert "$" not in core
    return core


def _labels(labels, name=""):
    return name + "{" + ",".join(f"{key}={json.dumps(value)}" for key, value in sorted(labels.items())) + "}"


def _series(metric, values, **labels):
    return {"series": _labels(labels, metric), "values": values}


def _sample(value, **labels):
    return {"labels": _labels(labels), "value": value}


def _case(inputs, expression, expected):
    return {"interval": "15s", "input_series": inputs, "promql_expr_test": [
        {"expr": expression, "eval_time": "2m", "exp_samples": expected},
    ]}


def _run_promtool(tmp_path, name, cases):
    fixture = tmp_path / f"{name}.yaml"
    suite = {"evaluation_interval": "1m", "fuzzy_compare": True, "tests": cases}
    fixture.write_text(yaml.safe_dump(suite, allow_unicode=True), encoding="utf-8")
    subprocess.run([
        "docker", "run", "--rm", "--pull=never", "--network", "none",
        "--mount", f"type=bind,source={tmp_path},target=/validation,readonly",
        "--entrypoint", "/bin/promtool", "prom/prometheus:v3.13.0",
        "test", "rules", f"/validation/{fixture.name}",
    ], check=True)


def _timer(name, *, count="10+0x8", total="1+0x8", instance="nacos-1"):
    labels = {"name": name, "cluster": "test", "job": "nacos", "instance": instance, "environment": "test"}
    result = []
    if count is not None:
        result.append(_series("nacos_timer_seconds_count", count, **labels))
    if total is not None:
        result.append(_series("nacos_timer_seconds_sum", total, **labels))
    return result


def test_notify_rate_distinguishes_observations_zero_and_missing(tmp_path):
    expression = _core(migration.NACOS_UID, 61)
    unrelated = _timer("dumpRt", count="0+1x8", total="0+10000x8")
    cases = [
        _case(_timer("notifyRt", count="0+30x8", total="0+3x8") + unrelated,
              expression, [_sample(100)]),
        _case(_timer("notifyRt", count="0+30x8", total="0+0x8"), expression, [_sample(0)]),
        _case(_timer("notifyRt") + unrelated, expression, [_sample(-1)]),
        _case(unrelated, expression, []),
        _case(_timer("notifyRt", total=None), expression, []),
        _case(_timer("notifyRt", count=None), expression, []),
        _case(_timer("notifyRt", count="_ _ _ _ _ _ _ _ 10", total="_ _ _ _ _ _ _ _ 1"),
              expression, []),
    ]
    _run_promtool(tmp_path, "detail-notify-status", cases)


def test_push_sentinel_does_not_hide_valid_zero_or_become_a_negative_curve(tmp_path):
    stat = _core(migration.NACOS_UID, 70)
    curve = _core(migration.NACOS_UID, 50)
    labels = {"name": "avgPushCost", "cluster": "test", "job": "nacos", "environment": "test"}
    invalid = _series("nacos_monitor", "-1+0x8", **labels, instance="invalid")
    valid = _series("nacos_monitor", "0+0x8", **labels, instance="healthy")
    curve_sample = {"labels": _labels({**labels, "instance": "healthy"}, "nacos_monitor"), "value": 0}
    cases = [
        _case([invalid, valid], stat, [_sample(0)]),
        _case([invalid, valid], curve, [curve_sample]),
        _case([invalid], stat, [_sample(-1)]),
        _case([invalid], curve, []),
        _case([], stat, []),
        _case([], curve, []),
    ]
    _run_promtool(tmp_path, "detail-push-status", cases)


def test_exception_zero_requires_an_observed_counter(tmp_path):
    cases = []
    for panel_id, exception in ((55, "configNotify"), (56, "unhealth"), (57, "disk"),
                                (58, "leaderSendBeatFailed"), (59, "illegalArgument"), (60, "nacos")):
        expression = _core(migration.NACOS_UID, panel_id)
        labels = {"cluster": "test", "job": "nacos", "instance": "nacos-1", "environment": "test"}
        observed = _series("nacos_exception_total", "2+0x8", **labels, name=exception)
        unrelated = _series("nacos_exception_total", "0+1x8", **labels, name="db")
        cases.append(_case([observed, unrelated], expression, [_sample(0)]))
        cases.append(_case([unrelated], expression, []))
    _run_promtool(tmp_path, "detail-exception-status", cases)


def _offsets(metric, consumer=False):
    inputs = []
    expected = []
    for topic, values, slope in (("growing", "1000+15x8", 1), ("retreating", "1000-15x8", -1)):
        aggregate = {"cluster": "test", "job": "kafka_exporter", "topic": topic}
        if consumer:
            aggregate["consumergroup"] = "consumer-test"
        # Two exporter replicas must not double-count a partition's progress.
        for instance in ("exporter-1", "exporter-2"):
            inputs.append(_series(metric, values, **aggregate, partition="0", instance=instance, environment="test"))
        expected.append(_sample(slope, **aggregate))
    return inputs, expected


@pytest.mark.parametrize("uid", migration.KAFKA_UIDS)
def test_kafka_resolution_preserves_signed_progress_and_partition_deduplication(tmp_path, uid):
    cases = []
    for panel_id, metric, scale, consumer in (
        (14, "kafka_topic_partition_current_offset", 1, False),
        (16, "kafka_topic_partition_current_offset", 60, False),
        (18, "kafka_consumergroup_current_offset", 60, True),
    ):
        expression = _core(uid, panel_id)
        assert "[1m:15s]" in expression
        inputs, expected = _offsets(metric, consumer)
        expected = [{**sample, "value": sample["value"] * scale} for sample in expected]
        cases.append(_case(inputs, expression, expected))
        # A 1m implicit evaluation step yields only one point in this 1m window.
        cases.append(_case(inputs, expression.replace("[1m:15s]", "[1m:]"), []))
    _run_promtool(tmp_path, f"detail-kafka-{uid}", cases)
