"""Repair data presentation in four existing Grafana v2 detail dashboards."""

import argparse
from copy import deepcopy
import json
from pathlib import Path


CONTAINER_UID = "detail-9323b204dbc111"
NACOS_UID = "detail-7772a5bacd7ad7"
KAFKA_UIDS = ("detail-431e149b73b9b7", "detail-852bebbd38276b")
SUPPORTED_UIDS = (CONTAINER_UID, NACOS_UID, *KAFKA_UIDS)
SELECTOR = ('cluster=~"$cluster",job=~"$job",instance=~"$instance",'
            'job!="hdg-application",environment=~"${environment:regex}"')
WRAPPED_CORE_START = "label_replace(label_replace(label_join(label_replace(((\n"
WRAPPED_CORE_END = '\n) < Inf > -Inf),"suite_instance_raw"'


def _panel(dashboard, panel_id, visualization):
    element = dashboard["spec"]["elements"].get(f"panel-{panel_id}")
    if not element or element.get("kind") != "Panel":
        raise ValueError(f"Missing expected panel {panel_id}")
    panel = element["spec"]
    if panel.get("id") != panel_id or panel["vizConfig"].get("group") != visualization:
        raise ValueError(f"Panel {panel_id} no longer has its expected visualization")
    return panel


def _query(panel):
    queries = panel["data"]["spec"]["queries"]
    if len(queries) != 1:
        raise ValueError(f"Panel {panel['id']} must have exactly one query")
    query = queries[0]
    if (query.get("kind") != "PanelQuery"
            or query["spec"]["query"].get("group") != "prometheus"):
        raise ValueError(f"Panel {panel['id']} must query Prometheus")
    result = query["spec"]["query"]["spec"]
    if not isinstance(result.get("expr"), str):
        raise ValueError("Missing PromQL expression")
    return result


def _replace_core(panel, original, replacement):
    query = _query(panel)
    expression = query["expr"]
    if not expression.startswith(WRAPPED_CORE_START) or expression.count(WRAPPED_CORE_END) != 1:
        raise ValueError(f"Panel {panel['id']} has an unrecognized query wrapper")
    end = expression.index(WRAPPED_CORE_END)
    current = expression[len(WRAPPED_CORE_START):end]
    if current not in (original, replacement):
        raise ValueError(f"Panel {panel['id']} has an unrecognized core expression")
    query["expr"] = WRAPPED_CORE_START + replacement + expression[end:]


def _null_text(panel, text):
    defaults = panel["vizConfig"]["spec"]["fieldConfig"]["defaults"]
    defaults["noValue"] = text
    for mapping in defaults.get("mappings", []):
        if mapping.get("type") == "special" and mapping.get("options", {}).get("match") == "null":
            mapping["options"].setdefault("result", {})["text"] = text


def _negative_one_text(panel, text):
    mappings = panel["vizConfig"]["spec"]["fieldConfig"]["defaults"].setdefault("mappings", [])
    for mapping in mappings:
        if mapping.get("type") == "value" and "-1" in mapping.get("options", {}):
            mapping["options"]["-1"] = {"text": text}
            return
    mappings.append({"type": "value", "options": {"-1": {"text": text}}})


def _container(dashboard):
    panel = _panel(dashboard, 28, "table")
    query = _query(panel)
    if ("cadvisor_version_info{" not in query["expr"] or query.get("format") != "table"
            or query.get("instant") is not True or query.get("range") is not False):
        raise ValueError("The container version table must use its original instant table query")
    columns = {
        "environment": "所属账号", "instance": "实例", "cadvisorVersion": "cAdvisor 版本",
        "cadvisorRevision": "构建版本", "kernelVersion": "内核版本", "osVersion": "操作系统",
    }
    organize = {"group": "organize", "kind": "Transformation", "spec": {"options": {
        "excludeByName": {name: True for name in (
            "Time", "__name__", "cluster", "job", "suite_instance_raw", "suite_host_key",
            "Value", "Value #A", "Mean",
        )},
        "indexByName": {name: index for index, name in enumerate(columns)},
        "renameByName": columns,
    }}}
    transformations = panel["data"]["spec"].get("transformations", [])
    retained = []
    for transformation in transformations:
        group = transformation.get("group")
        if group == "reduce":
            expected = {"includeTimeField": False, "labelsToFields": False,
                        "mode": "seriesToRows", "reducers": ["mean"]}
            if transformation.get("spec", {}).get("options") != expected:
                raise ValueError("Unexpected container reduce transformation")
        elif group == "organize":
            if transformation != organize:
                raise ValueError("Unexpected container organize transformation")
        elif group == "renameByRegex":
            retained.append(transformation)
        else:
            raise ValueError("Unexpected container table transformation")
    panel["data"]["spec"]["transformations"] = [*retained, organize]
    viz = panel["vizConfig"]["spec"]
    viz["fieldConfig"]["defaults"]["unit"] = "none"
    overrides = []
    for override in viz["fieldConfig"].get("overrides", []):
        if override.get("matcher") == {"id": "byName", "options": "Mean"}:
            properties = [prop for prop in override.get("properties", [])
                          if prop.get("id") != "custom.hideFrom.viz"]
            if properties:
                override["properties"] = properties
                overrides.append(override)
        else:
            overrides.append(override)
    viz["fieldConfig"]["overrides"] = overrides
    viz["options"]["showHeader"] = True


def _kafka(dashboard):
    for panel_id, metric in ((14, "kafka_topic_partition_current_offset"),
                             (16, "kafka_topic_partition_current_offset"),
                             (18, "kafka_consumergroup_current_offset")):
        panel = _panel(dashboard, panel_id, "timeseries")
        query = _query(panel)
        expression = query["expr"]
        if metric + "{" not in expression or "deriv(" not in expression:
            raise ValueError(f"Panel {panel_id} no longer uses its signed offset derivative")
        before, after = "[$__rate_interval:]", "[$__rate_interval:15s]"
        if expression.count(before) + expression.count(after) != 1:
            raise ValueError(f"Panel {panel_id} has an unexpected subquery")
        query["expr"] = expression.replace(before, after)


def _nacos(dashboard):
    panel = _panel(dashboard, 61, "stat")
    original_sum = f"sum(rate(nacos_timer_seconds_sum{{{SELECTOR}}}[$__rate_interval]))"
    original_count = f"sum(rate(nacos_timer_seconds_count{{{SELECTOR}}}[$__rate_interval]))"
    notify_sum = f'sum(rate(nacos_timer_seconds_sum{{name="notifyRt",{SELECTOR}}}[$__rate_interval]))'
    notify_count = f'sum(rate(nacos_timer_seconds_count{{name="notifyRt",{SELECTOR}}}[$__rate_interval]))'
    original = f"1000 * ({original_sum}) / ({original_count} > 0)"
    replacement = (f"(1000 * {notify_sum} / ({notify_count} > 0))"
                   f" or ((0 * ({notify_count} == 0) - 1) and on() {notify_sum})")
    _replace_core(panel, original, replacement)
    panel["title"] = "通知平均耗时"
    panel["description"] = ("只统计 notifyRt 通知计时器。当前窗口内计数速率为零且计数与耗时指标均存在时，"
                            "显示无通知样本；缺失指标或样本不足不记为 0 毫秒。")
    _null_text(panel, "指标缺失或采样不足")
    _negative_one_text(panel, "当前窗口无通知样本")

    panel = _panel(dashboard, 70, "stat")
    push = f"nacos_monitor{{name='avgPushCost',{SELECTOR}}}"
    _replace_core(panel, f"max({push})", f"max({push} >= 0) or max({push} == -1)")
    panel["title"] = "平均推送耗时（最高实例）"
    panel["description"] = ("展示有效平均推送耗时的最高实例；指标返回 -1 且没有有效实例值时，"
                            "显示暂无有效推送耗时。指标缺失或采样不足不记为 0 毫秒。")
    _null_text(panel, "指标缺失或采样不足")
    _negative_one_text(panel, "暂无有效推送耗时")

    panel = _panel(dashboard, 50, "timeseries")
    per_instance_push = ('nacos_monitor{cluster=~"$cluster",job=~"$job",instance=~"$instance",'
                         'name="avgPushCost",job!="hdg-application",environment=~"${environment:regex}"}')
    _replace_core(panel, per_instance_push, f"{per_instance_push} >= 0")
    panel["title"] = "各实例平均推送耗时"
    panel["description"] = ("展示各实例最近上报的有效平均推送耗时；负数无效值不绘制。"
                            "不同实例的平均值不相加，缺失或无效值不记为 0 毫秒。")
    _null_text(panel, "暂无有效推送耗时或指标缺失")

    for panel_id in (51, 54):
        panel = _panel(dashboard, panel_id, "timeseries")
        _query(panel)["legendFormat"] = panel["title"]

    for panel_id in range(55, 61):
        panel = _panel(dashboard, panel_id, "timeseries")
        query = _query(panel)
        if "nacos_exception_total{" not in query["expr"]:
            raise ValueError(f"Panel {panel_id} no longer queries its exception counter")
        query["legendFormat"] = panel["title"]
        panel["description"] = ("每分钟异常次数，按该异常类型的原始计数器计算。"
                                "无数据表示该类型指标未暴露、未采集或有效采样不足，不能判定异常次数为 0；"
                                "只有采集到稳定的计数器才显示 0。")
        _null_text(panel, "指标未暴露或采样不足")
    _nacos_empty_status(dashboard)


def _nacos_empty_status(dashboard):
    panel = _panel(dashboard, 94, "volkovlabs-echarts-panel")
    options = panel["vizConfig"]["spec"]["options"]
    runtime, separator, encoded = options.get("getOption", "").rpartition("})(context, ")
    if not separator or not encoded.endswith(");"):
        raise ValueError("Unrecognized Nacos independent time manager runtime")
    config = json.loads(encoded[:-2])
    if (config != options.get("nativeIndependentTimeManager")
            or config.get("dashboardUid") != NACOS_UID or config.get("helperPanelId") != 94):
        raise ValueError("Nacos independent time manager configuration is inconsistent")
    before = "else setStatus(record, 'ready', data?.series?.length ? text.timezone : text.empty + ' | ' + text.timezone);"
    after = "else setStatus(record, 'ready', data?.series?.length ? text.timezone : (record.emptyText || text.empty) + ' | ' + text.timezone);"
    before_record = "title: specification.title || panel.state.title || String(id), editing: false"
    after_record = "title: specification.title || panel.state.title || String(id), emptyText: specification.emptyText, editing: false"
    for original, replacement in ((before, after), (before_record, after_record)):
        if runtime.count(original) + runtime.count(replacement) != 1:
            raise ValueError("Nacos independent time manager has an unrecognized status implementation")
        runtime = runtime.replace(original, replacement)
    selected = {item.get("id"): item for item in config.get("panels", [])
                if item.get("id") in (50, 55, 56, 57, 58, 59, 60)}
    if set(selected) != {50, 55, 56, 57, 58, 59, 60}:
        raise ValueError("Nacos independent time manager is missing affected panels")
    selected[50]["title"] = dashboard["spec"]["elements"]["panel-50"]["spec"]["title"]
    selected[50]["emptyText"] = "无有效推送耗时或指标缺失"
    for panel_id in range(55, 61):
        selected[panel_id]["emptyText"] = "该类异常指标未暴露或采样不足"
    options["nativeIndependentTimeManager"] = config
    options["getOption"] = runtime + separator + json.dumps(config, ensure_ascii=False, separators=(",", ":")) + ");"


def update_dashboard(dashboard):
    """Return a copy with targeted repairs; retain identity and all unrelated data."""
    if dashboard.get("apiVersion") != "dashboard.grafana.app/v2" or dashboard.get("kind") != "Dashboard":
        raise ValueError("Export the existing Grafana v2 dashboard from Edit as code")
    uid = dashboard.get("metadata", {}).get("name")
    if uid not in SUPPORTED_UIDS:
        raise ValueError("This updater only accepts the four existing detail dashboard UIDs")
    if not isinstance(dashboard.get("spec", {}).get("elements"), dict):
        raise ValueError("Dashboard elements are missing")
    result = deepcopy(dashboard)
    if uid == CONTAINER_UID:
        _container(result)
    elif uid == NACOS_UID:
        _nacos(result)
    else:
        _kafka(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Fresh existing Grafana v2 JSON export")
    parser.add_argument("output", type=Path, help="Updated JSON, separate from the original backup")
    args = parser.parse_args(argv)
    if args.input.resolve() == args.output.resolve():
        parser.error("Input and output must be different files; preserve the original export")
    dashboard = json.loads(args.input.read_text(encoding="utf-8-sig"))
    result = update_dashboard(dashboard)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
