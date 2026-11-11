"""Offline contracts for the existing business dashboard's five alert columns."""

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "update_business_alert_details", ROOT / "scripts/update_business_alert_details.py"
)
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


@pytest.fixture
def original():
    return json.loads((ROOT / "grafana/cloud-business-dev-v1.json").read_text(encoding="utf-8"))


@pytest.fixture
def updated(original):
    return updater.update_alert_details(original)


def panel(dashboard):
    return dashboard["spec"]["elements"]["panel-2"]["spec"]


def content_mappings(dashboard):
    override = next(item for item in panel(dashboard)["vizConfig"]["spec"]["fieldConfig"]["overrides"]
                    if item["matcher"] == {"id": "byName", "options": "告警内容"})
    return next(prop["value"] for prop in override["properties"] if prop["id"] == "mappings")


def render_content(dashboard, value):
    for mapping in content_mappings(dashboard):
        options = mapping["options"]
        match = re.search(options["pattern"], value)
        if match:
            return re.sub(r"\$(\d+)", lambda ref: match.group(int(ref.group(1))) or "",
                          options["result"]["text"])
    return value


def test_only_alert_detail_panel_changes_and_input_is_not_mutated(original):
    before = deepcopy(original)
    result = updater.update_alert_details(original)
    assert original == before
    result["spec"]["elements"]["panel-2"] = before["spec"]["elements"]["panel-2"]
    assert result == before


def test_update_is_idempotent(updated):
    assert updater.update_alert_details(updated) == updated


def test_current_measurements_are_fresh_successful_and_joined_by_full_identity(updated):
    expression = panel(updated)["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]["expr"]
    assert "group_left(unit,scope)" in expression
    assert f"* on({updater.IDENTITY})" in expression
    assert f"and on({updater.IDENTITY}) (cloud_monitor_resource_fresh" in expression
    assert "and on(job,instance,provider,account,job_id) (cloud_monitor_job_status" in expression
    assert 'status="ok"' in expression
    assert "cloud_monitor_scheduler_alive" in expression
    assert 'up{job="$scrape_job",instance="$instance"} == 1' in expression
    assert "* (0 / 0)" in expression
    assert "vector(0)" not in expression
    assert "sum(" not in expression
    assert "/ 1000000" in expression
    assert 'metric=~"network_in_bps|network_out_bps",unit="bit/s"' in expression


def test_generic_fractional_rates_are_not_rounded_to_zero():
    measured = updater._measurement('metric="db_row_lock_waits_per_second"',
                                    'metric="db_row_lock_waits_per_second"')
    assert "round(" not in measured
    assert "/ 1000000" not in measured


def test_exactly_the_original_five_columns_and_no_metadata_or_added_widths(updated):
    details = panel(updated)
    transformations = details["data"]["spec"]["transformations"]
    names = {"severity": "级别", "cloud_account": "账号", "resource_type": "资源类型",
             "resource_display": "资源名称", "alert_content": "告警内容"}
    assert transformations[-1]["spec"]["options"]["renameByName"] == names
    assert transformations[-2]["spec"]["options"]["include"]["names"] == list(names)
    serialized = json.dumps(details, ensure_ascii=False)
    for removed in ("cloud_monitor_alert_rule_info", "metric_display", "alert_condition", "规则 ID", "触发条件"):
        assert removed not in serialized
    assert "其他监测内容" not in serialized
    for override in details["vizConfig"]["spec"]["fieldConfig"]["overrides"]:
        assert not any(prop["id"] == "custom.width" for prop in override["properties"])


@pytest.mark.parametrize(("raw", "expected"), [
    ("network_out_bps|huawei-rds-network|huawei-rds-network-out|P0|Mbps：260.95",
     "RDS 内网出站（估算）：260.95 Mbps；达到 200 Mbps，持续 5 分钟"),
    ("network_in_bps|aliyun-ecs-network|aliyun-ecs-network-in|P1|Mbps：340.42",
     "ECS 内网入站：340.42 Mbps；达到 300 Mbps，持续 10 分钟"),
    ("network_in_bps|aliyun-yuanzhu-ecs-network|yuanzhu-network-in|P1|Mbps：340.42",
     "ECS 内网入站：340.42 Mbps；阈值未提供"),
    ("network_out_bps|aliyun-yuanzhu-rds-network|yuanzhu-network-out|P0|Mbps：260.95",
     "RDS 出站：260.95 Mbps；阈值未提供"),
    ("network_out_bps|huawei-ecs-network|huawei-ecs-network-out|P2|Mbps：120",
     "ECS 内网出站（估算）：120 Mbps；达到 100 Mbps，持续 30 分钟"),
    ("network_out_bps|huawei-rds-network|huawei-rds-network-out|P0|Mbps：NaN",
     "RDS 内网出站（估算）：数值暂不可用；达到 200 Mbps，持续 5 分钟"),
    ("network_in_bps|custom-network|custom-rule|P1|Mbps：8.34", "网络入站：8.34 Mbps；阈值未提供"),
    ("db_row_lock_waits_per_second|mysql|row-lock|P1|次/秒：0.00000012",
     "MySQL 行锁等待速率：0.00000012 次/秒"),
    ("db_cpu_percent|huawei-rds-performance|cpu|P1|%：92.75", "数据库 CPU 使用率：92.75 %"),
    ("db_cpu_percent|huawei-rds-performance|cpu|P1|：NaN", "数据库 CPU 使用率：数值暂不可用"),
    ("collection_failed|huawei-rds-network|collection-failure|P1|：NaN", "采集状态：数值暂不可用"),
    ("unknown_metric|custom|custom-rule|P0|widgets：17.2", "unknown_metric：17.2 widgets"),
    ("unknown_metric|custom|custom-rule|P0|：NaN", "unknown_metric：数值暂不可用"),
    ("expiry_days|huawei-rds|expiry|P1|天：27.6", "还有 27.6 天到期"),
    ("expiry_days|huawei-rds|expiry|P0|天：-2.3", "已到期 2.3 天"),
    ("expiry_days|huawei-rds|expiry|P0|天：0", "今天到期"),
    ("expiry_days|huawei-rds|expiry|P1|：NaN", "到期时间暂不可用"),
])
def test_alert_content_explains_metric_current_value_scope_and_unavailable_state(updated, raw, expected):
    assert render_content(updated, raw) == expected


@pytest.mark.parametrize(("provider", "product", "severity", "threshold", "minutes"), [
    ("aliyun", "ecs", "P0", 800, 5), ("aliyun", "ecs", "P1", 300, 10),
    ("aliyun", "ecs", "P2", 100, 15), ("huawei", "ecs", "P0", 800, 10),
    ("huawei", "ecs", "P1", 300, 15), ("huawei", "ecs", "P2", 100, 30),
    ("aliyun", "rds", "P0", 200, 5), ("aliyun", "rds", "P1", 100, 10),
    ("aliyun", "rds", "P2", 50, 15), ("huawei", "rds", "P0", 200, 5),
    ("huawei", "rds", "P1", 100, 10), ("huawei", "rds", "P2", 50, 15),
])
@pytest.mark.parametrize(("direction", "text"), [("in", "入站"), ("out", "出站")])
def test_network_thresholds_use_exact_rule_severity_and_cloud_window(
        updated, provider, product, severity, threshold, minutes, direction, text):
    raw = (f"network_{direction}_bps|{provider}-another-account-{product}-network|"
           f"{provider}-{product}-network-{direction}|{severity}|Mbps：272.21")
    name = ("RDS " if product == "rds" else "ECS 内网") if provider == "aliyun" else (
        "RDS 内网" if product == "rds" else "ECS 内网")
    qualifier = "（估算）" if provider == "huawei" else ""
    assert render_content(updated, raw) == (
        f"{name}{text}{qualifier}：272.21 Mbps；达到 {threshold} Mbps，持续 {minutes} 分钟"
    )


def test_threshold_text_follows_supplied_config_instead_of_fixed_defaults(original):
    rules = [{"id": "custom.out", "metric": "network_out_bps", "match": {"product": "rds"}, "levels": [
        {"severity": "P0", "op": "gt", "threshold": 125_000_000, "for_seconds": 90},
        {"severity": "P2", "op": "ge", "threshold": 60_000_000},
    ]}]
    updated = updater.update_alert_details(original, rules)
    assert render_content(updated, "network_out_bps|huawei-rds-network|custom.out|P0|Mbps：150") == (
        "RDS 出站：150 Mbps；超过 125 Mbps，持续 90 秒"
    )
    assert render_content(updated, "network_out_bps|huawei-rds-network|custom.out|P2|Mbps：150") == (
        "RDS 出站：150 Mbps；达到 60 Mbps"
    )
    assert render_content(updated, "network_out_bps|huawei-rds-network|customXout|P0|Mbps：150") == (
        "RDS 内网出站（估算）：150 Mbps；阈值未提供"
    )


def test_exact_existing_balance_threshold_text_is_preserved(original, updated):
    balance_prefixes = tuple("^" + metric + r"\|" for metric in updater.BALANCE.split("|"))
    existing = [deepcopy(mapping) for mapping in content_mappings(original)
                if mapping["options"]["pattern"].startswith(balance_prefixes)
                and mapping["options"]["pattern"].count(r"\|") == 2]
    assert existing
    for item in existing:
        item["options"]["pattern"] = item["options"]["pattern"].replace(r"\|[^：]*：", r"\|[^|：]*：")
    assert content_mappings(updated)[:len(existing)] == existing
    assert render_content(updated, "qiniu_cash_balance|qiniu-cash-low|P1：417.08") == (
        "七牛云现金余额417.08元，≤500元"
    )
    assert render_content(updated, "balance|aliyun-balance|P1：1839.2") == "云账户余额1839.2元，≤2000元"


def test_balance_fallback_does_not_depend_on_old_generic_mappings(original):
    mappings = content_mappings(original)
    mappings[:] = [mapping for mapping in mappings
                   if not any(mapping["options"]["pattern"].startswith("^" + metric + r"\|[^|")
                              for metric in updater.BALANCE.split("|"))]
    updated = updater.update_alert_details(original)
    assert render_content(updated, "balance|new-rule|P0：42.4") == "账户余额：42.4 元"
    assert render_content(updated, "balance|new-rule|P0：NaN") == "账户余额：数值暂不可用"
    assert updater.update_alert_details(updated) == updated


def test_account_resource_and_severity_mappings_are_preserved(original, updated):
    def retained(dashboard):
        overrides = deepcopy(panel(dashboard)["vizConfig"]["spec"]["fieldConfig"]["overrides"])
        result = []
        for override in overrides:
            if override["matcher"].get("options") not in {"级别", "账号", "资源类型", "资源名称"}:
                continue
            override["properties"] = [prop for prop in override["properties"] if prop["id"] != "custom.width"]
            if override["properties"]:
                result.append(override)
        return result
    assert retained(original) == retained(updated)


def test_refuses_a_different_dashboard(original):
    original["metadata"]["name"] = "unrelated-dashboard"
    with pytest.raises(ValueError, match="cloud-business-dev-v1"):
        updater.update_alert_details(original)


def _promtool_suite():
    """Exercise actual value joins, independent of any rule metadata exporter."""
    scrape = {"job": "cloud-resource-monitor", "instance": "exporter:18080"}

    def series(name, labels, value):
        encoded = ",".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in sorted(labels.items()))
        return {"series": name + "{" + encoded + "}", "values": str(value) + "+0x5"}

    inputs = [series("cloud_monitor_scheduler_alive", scrape, 1), series("up", scrape, 1)]
    expected = []

    def row(job_id, metric, key, current, unit, display_unit, *, provider="huawei", account="huawei-hycen",
            fresh=True, successful=True):
        labels = {**scrape, "provider": provider, "account": account, "job_id": job_id,
                  "resource_id": "rds-id", "resource_name": "rds-PMS", "metric": metric,
                  "rule_id": job_id + "-rule", "severity": "P0", "metric_key": key}
        inputs.append(series("cloud_monitor_active_alert", labels, 1))
        result = dict(labels)
        available = current is not None and fresh and successful
        if current is not None:
            source = {**labels, "unit": unit, "scope": "all"}
            source.pop("rule_id")
            source.pop("severity")
            inputs.extend([series("cloud_monitor_resource_value", source, current),
                           series("cloud_monitor_resource_fresh", source, 1 if fresh else 0)])
        inputs.append(series("cloud_monitor_job_status", {
            **scrape, "provider": provider, "account": account, "job_id": job_id,
            "status": "ok" if successful else "error",
        }, 1))
        if available:
            result.update({"unit": unit, "scope": "all"})
        if available or metric in {"network_in_bps", "network_out_bps"}:
            result["display_unit"] = display_unit
        result.update({"resource_type": job_id, "cloud_account": provider + " / " + account,
                       "resource_display": "rds-PMS"})
        if metric in updater.BALANCE.split("|"):
            result["alert_metric_prefix"] = "|".join((metric, labels["rule_id"], labels["severity"])) + "："
        else:
            result["alert_metric_prefix"] = "|".join((
                metric, job_id, labels["rule_id"], labels["severity"], result.get("display_unit", ""),
            )) + "："
        if not available:
            value = float("nan")
        elif metric in {"network_in_bps", "network_out_bps"}:
            value = round(current / 1_000_000, 2)
        elif metric in updater.EXPIRY.split("|"):
            value = round(current, 1)
        else:
            value = current
        encoded = ",".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in sorted(result.items()))
        expected.append({"labels": "{" + encoded + "}", "value": value})

    row("huawei-rds-network", "network_out_bps", "network-1", 260950568.24, "bit/s", "Mbps")
    row("aliyun-balance", "balance", "balance-1", 1839.2, "CNY", "元", provider="aliyun", account="aliyun-hycen")
    row("huawei-rds", "expiry_days", "expiry-1", 27.64, "days", "天")
    row("huawei-rds-stale", "network_out_bps", "network-stale", 700_000_000, "bit/s", "Mbps", fresh=False)
    row("huawei-failed", "db_cpu_percent", "failed-1", 88.1, "percent", "%", successful=False)
    row("custom-missing", "unknown_metric", "missing-1", None, "widgets", "widgets")
    row("same-resource-job-1", "network_out_bps", "same-key", 111_000_000, "bit/s", "Mbps")
    row("same-resource-job-2", "network_out_bps", "same-key", 222_000_000, "bit/s", "Mbps")
    row("mysql-row-lock", "db_row_lock_waits_per_second", "small-rate", 0.00000012, "count/s", "次/秒")
    expression = updater.build_expression().replace("$scrape_job", scrape["job"])
    expression = expression.replace("$instance", scrape["instance"]).replace("${account:regex}", ".*")
    finite = [sample for sample in expected if sample["value"] == sample["value"]]
    unavailable = [{"labels": sample["labels"], "value": int(sample["value"] != sample["value"])}
                   for sample in expected]
    # promtool compares NaN expected values as unequal; compare their explicit mask instead.
    return {"evaluation_interval": "1m", "fuzzy_compare": True,
            "tests": [{"interval": "1m", "input_series": inputs, "promql_expr_test": [
                {"expr": "(" + expression + ") >= -Inf", "eval_time": "5m", "exp_samples": finite},
                {"expr": "(" + expression + ") != bool (" + expression + ")", "eval_time": "5m",
                 "exp_samples": unavailable},
            ]}]}


@pytest.mark.skipif(os.environ.get("RUN_PROMTOOL_TESTS") != "1", reason="Opt in to local offline Docker promtool")
def test_promql_semantics_with_existing_exporter(tmp_path):
    fixture = tmp_path / "business-alert-details.yaml"
    fixture.write_text(yaml.safe_dump(_promtool_suite(), allow_unicode=True), encoding="utf-8")
    subprocess.run([
        "docker", "run", "--rm", "--pull=never", "--network", "none",
        "--mount", f"type=bind,source={tmp_path},target=/validation,readonly",
        "--entrypoint", "/bin/promtool", "prom/prometheus:v3.13.0",
        "test", "rules", "/validation/business-alert-details.yaml",
    ], check=True)
