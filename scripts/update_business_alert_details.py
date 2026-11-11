"""Update only alert details in the existing Grafana v2 business dashboard."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re

import yaml

from cloud_monitor.notification_rendering import METRIC_NAMES


DASHBOARD_UID = "cloud-business-dev-v1"
PANEL_NAME = "panel-2"
IDENTITY = "job,instance,provider,account,job_id,metric_key"
ALERT_IDENTITY = IDENTITY + ",severity,rule_id"
FILTER = 'job="$scrape_job",instance="$instance",account=~"${account:regex}"'
BALANCE = "balance|qiniu_cash_balance|qiniu_available_balance"
EXPIRY = (
    "expiry_days|qiniu_package_expiry_days|huifu_certificate_expiry_days|commercial_expiry_days|"
    "package_expiry_days|subscription_expiry_days|annual_review_expiry_days"
)
NETWORK = "network_in_bps|network_out_bps"
NUMBER = r"(-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
NETWORK_CONFIG = Path(__file__).resolve().parents[1] / "config/network-traffic.yaml"
COMPARISON_TEXT = {"ge": "达到", "gt": "超过", "le": "不高于", "lt": "低于", "eq": "等于"}
DISPLAY_UNITS = {
    "bit/s": "Mbps", "CNY": "元", "days": "天", "percent": "%", "ratio": "倍",
    "seconds": "秒", "count": "次", "count/s": "次/秒", "ms|milliseconds": "毫秒",
    "bytes": "字节", "bool": "状态码",
}
NAMES = {
    **METRIC_NAMES,
    "js_error_affected_users": "小程序脚本错误影响人数",
    "js_error_occurrences": "小程序脚本错误次数",
    "qiniu_cdn_bandwidth_bps": "七牛 CDN 带宽",
    "qiniu_storage_bytes": "七牛标准存储用量",
}


def _label(expression, destination, replacement, source, pattern):
    args = (destination, replacement, source, pattern)
    return f"label_replace({expression}, " + ", ".join(json.dumps(arg, ensure_ascii=False) for arg in args) + ")"


def _active(extra=""):
    selector = f'{FILTER},severity=~"P0|P1|P2"' + ("," + extra if extra else "")
    return (
        f"((cloud_monitor_active_alert{{{selector}}} == 1)"
        " and on(job,instance) (cloud_monitor_scheduler_alive{"
        'job="$scrape_job",instance="$instance"} == 1)'
        ' and on(job,instance) (up{job="$scrape_job",instance="$instance"} == 1))'
    )


def _current(extra):
    selector = f'{FILTER},metric_key!="",{extra}'
    return (
        f"max by({IDENTITY},unit,scope) ("
        f"(cloud_monitor_resource_value{{{selector}}}"
        f" and on({IDENTITY}) (cloud_monitor_resource_fresh{{{selector}}} == 1))"
        " and on(job,instance,provider,account,job_id) "
        f'(cloud_monitor_job_status{{{FILTER},status="ok"}} == 1))'
    )


def _measurement(active_extra, source_extra, divisor=None, rounding=None):
    expression = f"({_active(active_extra)} * on({IDENTITY}) group_left(unit,scope) {_current(source_extra)})"
    if divisor:
        expression = f"({expression} / {divisor})"
    return f"round({expression}, {rounding})" if rounding else expression


def build_expression():
    excluded = f"{BALANCE}|{EXPIRY}|{NETWORK}"
    branches = [
        _measurement(f'metric=~"{BALANCE}"', f'metric=~"{BALANCE}",unit="CNY",currency=~"CNY|"',
                     rounding="0.01"),
        _measurement(f'metric=~"{EXPIRY}"', f'metric=~"{EXPIRY}",unit="days"', rounding="0.1"),
        _measurement(f'metric=~"{NETWORK}"', f'metric=~"{NETWORK}",unit="bit/s"',
                     divisor="1000000", rounding="0.01"),
        _measurement(f'metric!~"{excluded}"', f'metric!~"{excluded}"'),
        f"({_active()} * (0 / 0))",
    ]
    measured = "(" + f"\nor on({ALERT_IDENTITY}) ".join(branches) + ")"
    expression = _label(measured, "display_unit", "$1", "unit", "(.*)")
    for unit, label in DISPLAY_UNITS.items():
        # Network measurements alone are divided by 1e6; other bit/s metrics stay bit/s.
        if unit == "bit/s":
            continue
        expression = _label(expression, "display_unit", label, "unit", unit)
    expression = _label(expression, "display_unit", "Mbps", "metric", f"({NETWORK})")
    expression = (
        f'label_join({expression}, "alert_metric_prefix", "|", '
        '"metric", "job_id", "rule_id", "severity", "display_unit")'
    )
    expression = _label(expression, "alert_metric_prefix", "$1|$3|$4", "alert_metric_prefix",
                        f"({BALANCE})\\|([^|]*)\\|([^|]*)\\|([^|]*)\\|[^|]*")
    expression = _label(expression, "alert_metric_prefix", "$1：", "alert_metric_prefix", "(.*)")
    expression = _label(expression, "resource_type", "$1", "job_id", "(.*)")
    expression = f'label_join({expression}, "cloud_account", " / ", "provider", "account")'
    expression = _label(expression, "resource_display", "$1", "resource_id", "(.+)")
    return "# cloud-business-alert-details-v2\n" + _label(
        expression, "resource_display", "$1", "resource_name", "(.+)"
    )


def _regex(pattern, text):
    return {"type": "regex", "options": {"pattern": pattern, "result": {"text": text}}}


def _network_rule_mappings(rules):
    result = []
    for rule in rules:
        if rule["metric"] not in NETWORK.split("|"):
            continue
        match = rule.get("match", {})
        product = match.get("product")
        estimated = match.get("scope") == "intranet_estimated"
        name = "RDS " if product == "rds" else "网络"
        if product == "ecs":
            name = "ECS 内网" if estimated or match.get("scope") == "intranet" else "ECS 总网卡"
        elif product == "rds" and estimated:
            name = "RDS 内网"
        direction = "入站" if rule["metric"] == "network_in_bps" else "出站"
        label = name + direction + ("（估算）" if estimated else "")
        for level in rule["levels"]:
            condition = f"；{COMPARISON_TEXT[level['op']]} {level['threshold'] / 1_000_000:g} Mbps"
            seconds = level.get("for_seconds", 0)
            if seconds:
                duration = f"{seconds / 60:g} 分钟" if seconds % 60 == 0 else f"{seconds:g} 秒"
                condition += "，持续 " + duration
            if level.get("for_samples", 1) > 1:
                condition += f"，连续 {level['for_samples']} 次"
            # Match the exact rule and severity; another task may bind the same rule.
            prefix = ("^" + re.escape(rule["metric"]) + r"\|[^|]*\|" + re.escape(rule["id"])
                      + r"\|" + re.escape(level["severity"]) + r"\|([^：]*)：")
            result.extend([
                _regex(prefix + NUMBER + "$", label + "：$2 Mbps" + condition),
                _regex(prefix + ".*$", label + "：数值暂不可用" + condition),
            ])
    return result


def _content_mappings(existing, network_rules):
    # Preserve the existing balance text, including its configured threshold mappings.
    result = [deepcopy(mapping) for mapping in existing
              if mapping.get("type") == "regex"
              and mapping["options"]["pattern"].count(r"\|") == 2
              and any(mapping["options"]["pattern"].startswith("^" + metric + r"\|")
                      for metric in BALANCE.split("|"))]
    for mapping in result:
        mapping["options"]["pattern"] = mapping["options"]["pattern"].replace(
            r"\|[^：]*：", r"\|[^|：]*："
        )
    for metric in BALANCE.split("|"):
        for mapping in (
            _regex(r"^" + metric + r"\|[^|]*\|[^|：]*：" + NUMBER + "$", NAMES[metric] + "：$1 元"),
            _regex(r"^" + metric + r"\|[^|]*\|[^|：]*：.*$", NAMES[metric] + "：数值暂不可用"),
        ):
            if not any(item["options"]["pattern"] == mapping["options"]["pattern"] for item in result):
                result.append(mapping)
    suffix = r"\|[^|]*\|[^|]*\|[^|]*\|([^：]*)："
    for metric in EXPIRY.split("|"):
        prefix = "^" + metric + suffix
        result.extend([
            _regex(prefix + r"-?0(?:\.0+)?$", "今天到期"),
            _regex(prefix + r"-([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)$", "已到期 $2 天"),
            _regex(prefix + r"([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)$", "还有 $2 天到期"),
            _regex(prefix + ".*$", "到期时间暂不可用"),
        ])
    result.extend(_network_rule_mappings(network_rules))
    for metric, direction in (("network_in_bps", "入站"), ("network_out_bps", "出站")):
        scopes = (
            ("aliyun(?:-[^|]+)?-ecs-network", "ECS 内网", ""),
            ("huawei(?:-[^|]+)?-ecs-network", "ECS 内网", "（估算）"),
            ("huawei(?:-[^|]+)?-rds-network", "RDS 内网", "（估算）"),
            ("aliyun(?:-[^|]+)?-rds-network", "RDS ", ""),
            ("[^|]*", "网络", ""),
        )
        for job, name, qualifier in scopes:
            prefix = "^" + metric + r"\|" + job + r"\|[^|]*\|[^|]*\|([^：]*)："
            label = name + direction + qualifier
            result.extend([
                _regex(prefix + NUMBER + "$", label + "：$2 Mbps；阈值未提供"),
                _regex(prefix + ".*$", label + "：数值暂不可用；阈值未提供"),
            ])
    for metric, name in NAMES.items():
        if metric in set((BALANCE + "|" + EXPIRY + "|" + NETWORK).split("|")):
            continue
        prefix = "^" + re.escape(metric) + suffix
        result.extend([
            _regex(prefix + NUMBER + "$", name + "：$2 $1"),
            _regex(prefix + ".*$", name + "：数值暂不可用"),
        ])
    result.extend([
        _regex(r"^([^|]+)\|[^|]*\|[^|]*\|[^|]*\|([^：]*)：" + NUMBER + "$", "$1：$3 $2"),
        _regex(r"^([^|]+)\|[^|]*\|[^|]*\|[^|]*\|[^：]*：.*$", "$1：数值暂不可用"),
    ])
    return result


def _pagination_huawei_mappings(existing, network_rules):
    # Older exports contain doubled regex escapes; preserve their exact encoding.
    def normalize(pattern):
        return pattern.replace("\\\\", "\\")

    rules = [rule for rule in network_rules if rule.get("match", {}).get("provider") == "huawei"
             and rule.get("match", {}).get("scope") == "intranet_estimated"]
    labels = {normalize(item["options"]["pattern"]): item["options"]["result"]["text"].split("：", 1)[0]
              for item in _network_rule_mappings(rules)}
    products = {rule.get("match", {}).get("product") for rule in rules}
    fallback_labels = {}
    combined = {}
    for product in products & {"ecs", "rds"}:
        for metric, direction in (("network_in_bps", "入站"), ("network_out_bps", "出站")):
            prefix = "^" + metric + r"\|huawei(?:-[^|]+)?-" + product + r"-network\|[^|]*\|[^|]*\|([^：]*)："
            for suffix in (NUMBER + "$", ".*$"):
                key = prefix + suffix
                fallback_labels[key] = product.upper() + " 内网" + direction + "（估算）"
                if product == "rds":
                    combined[key.replace("huawei", "(?:aliyun|huawei)", 1)] = key
    labels.update(fallback_labels)
    present = {normalize(item.get("options", {}).get("pattern", "")) for item in existing}
    result = []
    for mapping in deepcopy(existing):
        options = mapping.get("options", {})
        pattern = options.get("pattern", "")
        key = normalize(pattern)
        if key in combined and combined[key] not in present:
            specific = deepcopy(mapping)
            specific["options"]["pattern"] = pattern.replace("(?:aliyun|huawei)", "huawei", 1)
            old = specific["options"]["result"]["text"]
            specific["options"]["result"]["text"] = labels[combined[key]] + "：" + old.split("：", 1)[1]
            result.append(specific)
            present.add(combined[key])
        if mapping.get("type") == "regex" and key in labels:
            old = options["result"]["text"]
            options["result"]["text"] = labels[key] + "：" + old.split("：", 1)[1]
        result.append(mapping)
    return result


def _update_paginated_alert_details(panel, network_rules):
    options = panel["vizConfig"]["spec"]["options"]
    config = options.get("alertDetailsPagination")
    script = options.get("getOption")
    marker = "})(context, "
    if (not isinstance(config, dict) or config.get("id") != 2 or config.get("version") != 1
            or not isinstance(script, str) or script.count(marker) != 1):
        raise ValueError("Expected the existing alert details pagination configuration")
    start = script.index(marker) + len(marker)
    embedded, length = json.JSONDecoder().raw_decode(script[start:])
    if embedded != config or script[start + length:].strip() != ");":
        raise ValueError("Alert details pagination configurations must agree")
    content = embedded.get("mappings", {}).get("告警内容")
    if not isinstance(content, list):
        raise ValueError("Expected the existing alert content mappings")
    embedded["mappings"]["告警内容"] = _pagination_huawei_mappings(content, network_rules)
    options["alertDetailsPagination"] = deepcopy(embedded)
    options["getOption"] = (script[:start] + json.dumps(embedded, ensure_ascii=False, separators=(",", ":"))
                            + script[start + length:])


def update_alert_details(dashboard, network_rules=None):
    if dashboard.get("metadata", {}).get("name") != DASHBOARD_UID:
        raise ValueError(f"Expected the existing dashboard {DASHBOARD_UID}")
    if dashboard.get("apiVersion") != "dashboard.grafana.app/v2":
        raise ValueError("Export the Grafana v2 JSON from Edit as code")
    result = deepcopy(dashboard)
    panel = result["spec"]["elements"][PANEL_NAME]["spec"]
    if network_rules is None:
        network_rules = yaml.safe_load(NETWORK_CONFIG.read_text(encoding="utf-8"))["rules"]
    if panel.get("id") == 2 and panel["vizConfig"]["group"] == "volkovlabs-echarts-panel":
        _update_paginated_alert_details(panel, network_rules)
        return result
    if panel.get("id") != 2 or panel["vizConfig"]["group"] != "table":
        raise ValueError("Expected panel-2 to be the existing alert details table or pagination panel")
    queries = panel["data"]["spec"]["queries"]
    if len(queries) != 1 or queries[0]["spec"]["query"]["group"] != "prometheus":
        raise ValueError("Expected the alert details table to have one Prometheus query")
    queries[0]["spec"]["query"]["spec"].update(
        {"expr": build_expression(), "format": "table", "instant": True, "range": False}
    )
    names = {
        "severity": "级别", "cloud_account": "账号", "resource_type": "资源类型",
        "resource_display": "资源名称", "alert_content": "告警内容",
    }
    panel["data"]["spec"]["transformations"] = [
        {"kind": "Transformation", "group": "calculateField", "spec": {"options": {
            "alias": "alert_content", "binary": {"left": "alert_metric_prefix", "operator": "+", "right": "Value"},
            "mode": "binary", "replaceFields": False, "timeSeries": False,
        }}},
        {"kind": "Transformation", "group": "convertFieldType", "spec": {"options": {
            "conversions": [{"destinationType": "string", "targetField": "alert_content"}],
        }}},
        {"kind": "Transformation", "group": "filterFieldsByName", "spec": {"options": {
            "include": {"names": list(names)},
        }}},
        {"kind": "Transformation", "group": "organize", "spec": {"options": {
            "excludeByName": {}, "indexByName": {name: index for index, name in enumerate(names)},
            "renameByName": names,
        }}},
    ]
    config = panel["vizConfig"]["spec"]["fieldConfig"]
    content = next(override for override in config["overrides"]
                   if override["matcher"] == {"id": "byName", "options": "告警内容"})
    mappings = next(prop for prop in content["properties"] if prop["id"] == "mappings")
    mappings["value"] = _content_mappings(mappings["value"], network_rules)
    config["overrides"] = [override for override in config["overrides"]
                           if override["matcher"] not in [
                               {"id": "byName", "options": "触发条件"},
                               {"id": "byName", "options": "规则 ID"},
                           ]]
    for override in config["overrides"]:
        if override["matcher"] in [{"id": "byName", "options": name} for name in names.values()]:
            override["properties"] = [prop for prop in override["properties"] if prop["id"] != "custom.width"]
    config["overrides"] = [override for override in config["overrides"] if override["properties"]]
    panel["description"] = (
        "当前筛选范围内处于活跃状态的 P0、P1、P2 告警。告警内容包括指标、当前有效值及单位；"
        "网络速率以 Mbps 显示，并按对应规则和级别展示配置中的阈值与持续时间；余额和到期保留原有显示。"
        "当前值仅使用采集成功且未过期的数据，缺少时明确显示暂不可用。"
        "未知指标保留原指标名称，便于追查。"
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--network-config", type=Path, default=NETWORK_CONFIG,
                        help="Network rules matching the deployed configuration")
    args = parser.parse_args()
    dashboard = json.loads(args.input.read_text(encoding="utf-8"))
    rules = yaml.safe_load(args.network_config.read_text(encoding="utf-8"))["rules"]
    result = update_alert_details(dashboard, rules)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
