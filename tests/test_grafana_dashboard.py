"""Offline checks for the import artifact and its metric/label contract."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_grafana_dashboard", ROOT / "scripts/build_grafana_dashboard.py")
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


def dashboard():
    return json.loads((ROOT / "grafana/cloud-resource-monitor.json").read_text(encoding="utf-8"))


def query_panels():
    return [panel for panel in dashboard()["panels"] if panel["type"] != "row"]


def test_committed_dashboard_matches_generator():
    assert dashboard() == generator.build_dashboard()


def test_panel_ids_and_grid_are_unambiguous():
    panels = dashboard()["panels"]
    assert len({panel["id"] for panel in panels}) == len(panels)
    cells = set()
    for panel in panels:
        position = panel["gridPos"]
        assert position["w"] > 0 and position["h"] > 0
        assert 0 <= position["x"] < position["x"] + position["w"] <= 24
        for x in range(position["x"], position["x"] + position["w"]):
            for y in range(position["y"], position["y"] + position["h"]):
                assert (x, y) not in cells, panel["title"]
                cells.add((x, y))


def test_insufficient_statistics_are_visible_in_collection_health():
    panels = {panel["title"]: panel for panel in query_panels()}
    health = panels["异常 / 待采集任务"]
    selector = re.search(r'status=~"([^"]+)"', health["targets"][0]["expr"]).group(1)
    assert re.fullmatch(selector, "insufficient_data")
    assert re.fullmatch(selector, "degraded")
    status = panels["任务状态 / 账号范围"]
    properties = {item["id"]: item["value"] for override in status["fieldConfig"]["overrides"]
                  if override["matcher"] == {"id": "byName", "options": "状态"}
                  for item in override["properties"]}
    assert properties["mappings"][0]["options"]["insufficient_data"] == {
        "text": "统计不足", "color": "orange",
    }
    assert properties["mappings"][0]["options"]["degraded"] == {
        "text": "估算不可用", "color": "orange",
    }


def test_filters_keep_one_exporter_and_escape_multiselects():
    variables = {variable["name"]: variable for variable in dashboard()["templating"]["list"]}
    assert variables["DS_PROMETHEUS"]["type"] == "datasource"
    assert variables["instance"]["multi"] is False
    assert variables["instance"]["includeAll"] is False
    for name in ["provider", "account", "resource"]:
        assert variables[name]["multi"] is True
        assert variables[name]["allValue"] == ".*"
    for panel in query_panels():
        for target in panel["targets"]:
            assert 'instance="$instance"' in target["expr"]
            assert "or vector(0)" not in target["expr"]
            if any(f'metric="{name}"' in target["expr"] for name in (
                "api_query_success", "api_query_duration_seconds", "api_last_success_timestamp_seconds",
                "api_signature_failed",
            )):
                assert 'resource_id=~"${resource:regex}"' not in target["expr"]
                assert 'account=~"${account:regex}"' in target["expr"]
                assert "账号范围" in panel["title"]
            elif "cloud_monitor_resource_" in target["expr"] or "cloud_monitor_active_alert" in target["expr"]:
                assert 'resource_id=~"${resource:regex}"' in target["expr"]


def test_network_estimate_quality_is_a_separate_resource_table():
    panel = next(panel for panel in query_panels() if panel["title"] == "内网估算可用性")
    assert panel["type"] == "table" and panel["gridPos"]["w"] == 24
    expression = panel["targets"][0]["expr"]
    assert expression.startswith("cloud_monitor_network_estimate_available{")
    assert 'resource_id=~"${resource:regex}"' in expression
    overrides = {override["matcher"]["options"]: override["properties"]
                 for override in panel["fieldConfig"]["overrides"]}
    mappings = next(item["value"] for item in overrides["估算状态"] if item["id"] == "mappings")
    assert mappings[0]["options"] == {"0": {"text": "估算不可用", "color": "orange"},
                                      "1": {"text": "可用", "color": "green"}}


def test_percent_ratio_currency_and_deadlocks_keep_source_meaning():
    panels = query_panels()
    qps = next(p for p in panels if 'metric="qps_ratio"' in p["targets"][0]["expr"])
    assert qps["fieldConfig"]["defaults"]["unit"] == "percentunit"
    for panel in panels:
        expression = panel["targets"][0]["expr"]
        if 'metric="balance"' in expression:
            assert 'unit="CNY"' in expression
            assert "sum(" not in expression
        if 'metric="db_deadlocks_last_hour"' in expression:
            assert "row_lock" not in expression
            assert "预留" in panel["title"]
        if panel["type"] == "timeseries":
            assert panel["fieldConfig"]["defaults"]["custom"]["spanNulls"] is False
        assert panel["fieldConfig"]["defaults"]["noValue"] == "无数据"


def test_tables_keep_each_series_without_unsafe_label_joins():
    for panel in query_panels():
        if panel["type"] != "table":
            continue
        assert len(panel["targets"]) == 1
        assert panel["targets"][0]["format"] == "table"
        assert panel["targets"][0]["instant"] is True
        assert {item["id"] for item in panel["transformations"]} == {"organize"}
    generic = next(p for p in query_panels() if p["title"] == "当前有效指标明细")
    names = generic["transformations"][0]["options"]["renameByName"]
    assert {"metric", "unit", "dimension_id", "metric_key", "resource_id", "job_id"} <= names.keys()


@pytest.mark.parametrize("panel", [p for p in query_panels() if p["type"] == "table"],
                         ids=lambda panel: str(panel["id"]))
def test_table_labels_stay_text_and_only_values_use_numeric_units(panel):
    columns = panel["transformations"][0]["options"]["renameByName"]
    value_unit = {
        "到期资源 / 按剩余天数排序": "suffix:天",
        "账号余额 / CNY": "currencyCNY",
    }.get(panel["title"], "short")
    for source, name in columns.items():
        config = dict(panel["fieldConfig"]["defaults"])
        for override in panel["fieldConfig"]["overrides"]:
            if override["matcher"] == {"id": "byName", "options": name}:
                config.update({item["id"]: item["value"] for item in override["properties"]})
        assert config["unit"] == (value_unit if source == "Value" else "string"), (panel["title"], source)


def test_expiry_table_keeps_resource_identity_and_day_sorting():
    panel = next(p for p in query_panels() if p["title"] == "到期资源 / 按剩余天数排序")
    columns = panel["transformations"][0]["options"]["renameByName"]
    assert columns["resource_id"] == "资源 ID"
    assert columns["Value"] == "剩余天数"
    assert panel["options"]["sortBy"] == [{"displayName": "剩余天数", "desc": False}]
    properties = {item["id"]: item["value"] for override in panel["fieldConfig"]["overrides"]
                  if override["matcher"] == {"id": "byName", "options": "剩余天数"}
                  for item in override["properties"]}
    assert properties["custom.cellOptions"] == {"type": "color-text"}
    assert properties["color"] == {"mode": "thresholds"}
    assert panel["fieldConfig"]["defaults"]["thresholds"]["steps"] == [
        {"color": "red", "value": None}, {"color": "orange", "value": 7}, {"color": "green", "value": 30},
    ]


def test_all_builtin_metrics_have_dedicated_panels():
    names = {
        "balance", "expiry_days", "package_remaining_percent", "package_used_percent", "storage_used_percent",
        "qps_ratio", "bandwidth_used_percent", "sms_growth_percent", "sms_success_percent",
        "db_cpu_percent", "db_memory_percent",
        "db_disk_used_percent", "db_connections_used_percent", "db_replication_delay_seconds",
        "db_row_lock_waits_per_second", "db_deadlocks_per_second", "db_deadlocks_last_hour",
        "db_slow_queries_per_second", "db_cache_hit_percent", "db_long_running_queries",
    }
    expressions = [target["expr"] for panel in query_panels() for target in panel["targets"]]
    for name in names:
        if name == "expiry_days":
            assert any('metric=~"expiry_days|' in expression for expression in expressions)
        else:
            assert any(f'metric="{name}"' in expression for expression in expressions), name


def test_external_platform_panels_keep_units_and_source_labels():
    panels = query_panels()
    for name in ("huifu_available_balance", "qiniu_budget_used_percent", "qiniu_package_remaining_percent",
                 "api_quota_remaining_percent", "charge_active_remaining_percent", "annual_review_expiry_days",
                 "api_last_success_timestamp_seconds", "performance_cost_milliseconds"):
        assert any(f'metric="{name}"' in p["targets"][0]["expr"] for p in panels), name
    ledger = next(p for p in panels if p["title"] == "爱签套餐台账剩余比例")
    assert 'source="ledger"' in ledger["targets"][0]["expr"]
    balance = next(p for p in panels if p["title"] == "汇付子账户可用余额 / CNY")
    assert 'unit="CNY"' in balance["targets"][0]["expr"]
    assert "sum(" not in balance["targets"][0]["expr"]
    cost = next(p for p in panels if p["title"] == "七牛预估费用 / 按币种账期")
    assert "billing_period" in cost["transformations"][0]["options"]["renameByName"]


def test_expiry_overview_table_and_trend_include_external_platforms_without_saas_authorization():
    panels = query_panels()
    for title in ("最近到期 / 天", "到期资源 / 按剩余天数排序", "资源剩余有效期"):
        panel = next(item for item in panels if item["title"] == title)
        selector = re.search(r'metric=~"([^"]+)"', panel["targets"][0]["expr"]).group(1)
        for name in ("expiry_days", "qiniu_package_expiry_days", "huifu_certificate_expiry_days",
                     "commercial_expiry_days", "package_expiry_days", "subscription_expiry_days",
                     "annual_review_expiry_days"):
            assert re.fullmatch(selector, name), (title, name)
        assert not re.fullmatch(selector, "authorization_expiry_days")
    table = next(item for item in panels if item["title"] == "到期资源 / 按剩余天数排序")
    assert {"metric", "source"} <= table["transformations"][0]["options"]["renameByName"].keys()


def test_regional_and_multi_metric_legends_identify_region_and_unit():
    panels = query_panels()
    for title in ("七牛 CDN 带宽", "七牛现金与可用余额 / CNY", "微信资源包及订阅剩余天数"):
        legend = next(item for item in panels if item["title"] == title)["targets"][0]["legendFormat"]
        assert "{{region}}" in legend
        assert "{{unit}}" in legend


def test_asign_openapi_certification_and_ledger_remain_without_saas_panels():
    panels = {p["title"]: p for p in query_panels()}
    certification = panels["爱签企业认证状态"]
    assert 'metric="company_verified"' in certification["targets"][0]["expr"]
    assert 'provider="asign"' in certification["targets"][0]["expr"]
    assert "scope" not in certification["transformations"][0]["options"]["renameByName"]
    quantity = panels["爱签套餐台账剩余数量"]
    assert 'metric="package_remaining"' in quantity["targets"][0]["expr"]
    assert 'source="ledger"' in quantity["targets"][0]["expr"]
    assert {"account", "resource_id", "unit", "source"} <= quantity["transformations"][0]["options"]["renameByName"].keys()
    assert quantity["gridPos"]["w"] == 24
    assert panels["爱签商业套餐台账剩余天数"]["fieldConfig"]["defaults"]["unit"] == "suffix:天"
    assert panels["爱签套餐台账年龄"]["fieldConfig"]["defaults"]["unit"] == "s"
    rendered = json.dumps(dashboard(), ensure_ascii=False)
    assert "SaaS" not in rendered
    assert "saas_" not in rendered
    assert "authorization_" not in rendered


def test_asign_openapi_section_has_no_empty_grid_cells():
    panels = dashboard()["panels"]
    start = next(index for index, panel in enumerate(panels)
                 if panel["title"] == "爱签 OpenAPI 企业认证与套餐台账")
    end = next(index for index in range(start + 1, len(panels)) if panels[index]["type"] == "row")
    first_y = panels[start]["gridPos"]["y"] + 1
    next_y = panels[end]["gridPos"]["y"]
    area = sum(panel["gridPos"]["w"] * panel["gridPos"]["h"] for panel in panels[start + 1:end])
    assert area == 24 * (next_y - first_y)


def test_existing_dashboard_and_panel_links_keep_their_identity():
    value = dashboard()
    assert value["uid"] == "cloud-resource-monitor"
    panels = {panel["title"]: panel["id"] for panel in value["panels"]}
    assert panels["爱签企业认证状态"] == 61
    assert panels["爱签套餐台账剩余比例"] == 63
    assert panels["爱签商业套餐台账剩余天数"] == 64
    assert panels["爱签套餐台账年龄"] == 66
    assert panels["爱签套餐台账剩余数量"] == 67
    assert panels["微信小程序额度、年审与质量"] == 69
    assert panels["当前有效指标明细"] == 79
    assert not {62, 65, 68} & set(panels.values())


@pytest.mark.parametrize("title,product,scope", [
    ("ECS 内网入出速率", "ecs", "intranet"),
    ("ECS 总网卡入出速率", "ecs", "all"),
    ("RDS 总网络入出吞吐", "rds", "all"),
])
def test_network_rates_keep_product_scope_directions_and_native_units(title, product, scope):
    panel = next(item for item in query_panels() if item["title"] == title)
    assert panel["type"] == "timeseries"
    expression = panel["targets"][0]["expr"]
    selector = re.search(r'metric=~"([^"]+)"', expression).group(1)
    assert re.fullmatch(selector, "network_in_bps")
    assert re.fullmatch(selector, "network_out_bps")
    assert not re.fullmatch(selector, "bandwidth_used_percent")
    for label in (f'product="{product}"', f'scope="{scope}"', 'unit="bit/s"',
                  'provider=~"${provider:regex}"', 'account=~"${account:regex}"',
                  'resource_id=~"${resource:regex}"'):
        assert label in expression
    assert "rate(" not in expression and "sum(" not in expression
    assert panel["fieldConfig"]["defaults"]["unit"] == "bps"
    assert panel["fieldConfig"]["defaults"]["min"] == 0
    assert panel["fieldConfig"]["defaults"]["thresholds"]["steps"] == [
        {"color": "green", "value": None},
        {"color": "blue", "value": 50_000_000 if product == "rds" else 100_000_000},
        {"color": "orange", "value": 100_000_000 if product == "rds" else 300_000_000},
        {"color": "red", "value": 200_000_000 if product == "rds" else 800_000_000},
    ]
    assert "alert" not in panel
    legend = panel["targets"][0]["legendFormat"]
    for label in ("provider", "account", "resource_name", "metric", "region", "dimension_id"):
        assert "{{" + label + "}}" in legend
    assert "阈值线对应 P2/P1/P0" in panel["description"]
    assert "实际告警以规则比较符和持续时间为准" in panel["description"]


def test_total_network_panel_descriptions_do_not_claim_intranet_only():
    panels = {item["title"]: item for item in query_panels()}
    assert "不等于纯内网流量" in panels["ECS 总网卡入出速率"]["description"]
    assert "无法按访问方区分内网与公网" in panels["RDS 总网络入出吞吐"]["description"]
    assert "1 分钟采样" in panels["ECS 内网入出速率"]["description"]
    assert "5 分钟采样" in panels["ECS 总网卡入出速率"]["description"]
