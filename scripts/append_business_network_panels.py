"""Maintain network charts in the existing Grafana v2 business dashboard export."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re


DASHBOARD_UID = "cloud-business-dev-v1"
SECTION_TITLE = "ECS 与 RDS 网络速率"
CHARTS = (
    ("阿里云 ECS 内网入出速率", "aliyun", "ecs", "intranet", 0, 0, 12,
     "阿里云 ECS 内网入出速率，不含公网流量；源采样周期为 1 分钟。"),
    ("华为云 ECS 总网卡入出速率", "huawei", "ecs", "all", 12, 0, 12,
     "华为云 ECS 所有网卡入出速率，包含公网与内网，不等于纯内网流量；源采样周期为 5 分钟。"),
    ("RDS 总网络输入／输出吞吐量", None, "rds", "all", 0, 10, 24,
     "阿里云与华为云 RDS 实例总网络吞吐，可能包含复制流量，不能还原 ECS 到 RDS 的通信关系。"
     "源采样周期为 1 分钟；华为云 RDS 数据可能延迟 5-10 分钟。"),
)
DIRECTION_TITLES = (
    ("阿里云 ECS 内网入站速率", "阿里云 ECS 内网出站速率"),
    ("华为云 ECS 总网卡入站速率", "华为云 ECS 总网卡出站速率"),
    ("RDS 总网络入站吞吐量", "RDS 总网络出站吞吐量"),
)
RDS_PROVIDER_TITLES = {
    "aliyun": ("阿里云 RDS 总网络入站吞吐量", "阿里云 RDS 总网络出站吞吐量"),
    "huawei": ("华为云 RDS 总网络入站吞吐量", "华为云 RDS 总网络出站吞吐量"),
}
LEGACY_TITLE_ALIASES = {"华为云 ECS 总网卡入出速率（含公网与内网）": CHARTS[1][0]}
HUAWEI_QUALIFIED_TITLES = ("华为云 ECS 总网卡入站速率（含公网与内网）", "华为云 ECS 总网卡出站速率（含公网与内网）")
DIRECTION_TITLE_ALIASES = dict(zip(HUAWEI_QUALIFIED_TITLES, DIRECTION_TITLES[1]))


def _network_section(dashboard):
    if dashboard.get("metadata", {}).get("name") != DASHBOARD_UID:
        raise ValueError(f"Expected the existing dashboard {DASHBOARD_UID}")
    if (dashboard.get("apiVersion") != "dashboard.grafana.app/v2"
            or dashboard.get("spec", {}).get("layout", {}).get("kind") != "RowsLayout"):
        raise ValueError("Export the Grafana v2 JSON from Edit as code, with RowsLayout")
    spec = dashboard["spec"]
    rows = spec["layout"]["spec"]["rows"]
    sections = [row for row in rows if row["spec"].get("title") == SECTION_TITLE]
    if len(sections) != 1:
        raise ValueError("Expected exactly one existing network section")
    section = sections[0]
    layout = section["spec"]["layout"]
    if layout.get("kind") != "GridLayout":
        raise ValueError("The network section must use GridLayout")
    items = layout["spec"]["items"]
    if any(item.get("kind") != "GridLayoutItem"
           or item.get("spec", {}).get("element", {}).get("kind") != "ElementReference" for item in items):
        raise ValueError("The network section contains an unrecognized layout item")
    names = [item["spec"]["element"]["name"] for item in items]
    if len(names) != len(set(names)):
        raise ValueError("The network section contains duplicate panel references")
    selected = {}
    for name in names:
        panel = spec["elements"].get(name)
        if panel is None or panel.get("kind") != "Panel":
            raise ValueError("The network section contains an unrecognized element")
        selected[name] = panel
    titles = [panel["spec"]["title"] for panel in selected.values()]
    legacy_titles = {chart[0] for chart in CHARTS}
    split_titles = {title for pair in DIRECTION_TITLES for title in pair}
    provider_titles = {title for pair in DIRECTION_TITLES[:2] for title in pair}
    provider_titles.update(title for pair in RDS_PROVIDER_TITLES.values() for title in pair)
    normalized_titles = {LEGACY_TITLE_ALIASES.get(title, title) for title in titles}
    normalized_titles = {DIRECTION_TITLE_ALIASES.get(title, title) for title in normalized_titles}
    if len(titles) == 3 and normalized_titles == legacy_titles:
        split = False
    elif ((len(titles) == 6 and normalized_titles == split_titles)
          or (len(titles) == 8 and normalized_titles == provider_titles)):
        split = True
    else:
        raise ValueError("Expected the complete three original, six directional, or eight cloud network charts")
    ids = [panel["spec"]["id"] for panel in spec["elements"].values()]
    if (any(type(panel_id) is not int or panel_id < 0 for panel_id in ids)
            or len(ids) != len(set(ids))):
        raise ValueError("Dashboard panel IDs must be unique integers")
    for other_row in rows:
        if other_row is section:
            continue
        other_layout = other_row["spec"].get("layout", {})
        for item in other_layout.get("spec", {}).get("items", []):
            if item.get("spec", {}).get("element", {}).get("name") in selected:
                raise ValueError("Network panels must only be referenced by the network section")
    return section, selected, split


def _chart_directions(title):
    title = LEGACY_TITLE_ALIASES.get(title, title)
    title = DIRECTION_TITLE_ALIASES.get(title, title)
    for index, chart in enumerate(CHARTS):
        if title == chart[0]:
            return ("in", "out")
        for direction, split_title in zip(("in", "out"), DIRECTION_TITLES[index]):
            if title == split_title:
                return (direction,)
    for pair in RDS_PROVIDER_TITLES.values():
        for direction, split_title in zip(("in", "out"), pair):
            if title == split_title:
                return (direction,)
    raise ValueError("Unrecognized network chart title")


def _directional_queries(panel_spec):
    queries = panel_spec["data"]["spec"]["queries"]
    expected = _chart_directions(panel_spec["title"])
    by_direction = {}
    for query in queries:
        query_spec = query["spec"]
        data_query = query_spec["query"]
        if query.get("kind") != "PanelQuery" or data_query.get("group") != "prometheus":
            raise ValueError("Network charts must use Prometheus panel queries")
        expression = data_query["spec"].get("expr", "")
        if not isinstance(expression, str):
            raise ValueError("Network queries must contain a PromQL expression")
        directions = [direction for direction in ("in", "out")
                      if f'metric="network_{direction}_bps"' in expression]
        if len(directions) != 1 or directions[0] in by_direction:
            raise ValueError("Expected one query for each network direction")
        direction = directions[0]
        if query_spec.get("refId") != {"in": "A", "out": "B"}[direction]:
            raise ValueError("Unrecognized network query reference ID")
        by_direction[direction] = query
    if set(by_direction) != set(expected):
        raise ValueError("Network queries do not match the chart direction")
    return by_direction


def _runtime_config(options):
    runtime, separator, encoded = options.get("getOption", "").rpartition("})(context, ")
    if not separator or not encoded.endswith(");"):
        raise ValueError("Unrecognized independent time chart runtime")
    try:
        config = json.loads(encoded[:-2])
    except json.JSONDecodeError as error:
        raise ValueError("Invalid independent time chart configuration") from error
    if not isinstance(config, dict) or config != options.get("independentTime"):
        raise ValueError("The independent time chart configuration is inconsistent")
    return runtime, separator, config


def _independent_config(panel_spec, queries):
    options = panel_spec["vizConfig"]["spec"]["options"]
    runtime, separator, config = _runtime_config(options)
    if (config.get("dashboardUid") != DASHBOARD_UID or config.get("id") != panel_spec["id"]
            or config.get("title") != panel_spec["title"] or config.get("unit") != "bps"):
        raise ValueError("The independent time configuration does not match the network panel")
    targets = config.get("targets", [])
    if not isinstance(targets, list) or any(not isinstance(target, dict) for target in targets):
        raise ValueError("Independent time targets must be query objects")
    by_ref = {target.get("refId"): target for target in targets}
    if len(by_ref) != len(targets) or len(targets) != len(queries):
        raise ValueError("Independent time targets do not match the network queries")
    for query in queries.values():
        query_spec = query["spec"]
        target = by_ref.get(query_spec["refId"])
        if target is None or target.get("expr") != query_spec["query"]["spec"]["expr"]:
            raise ValueError("Independent time targets do not match the network queries")
    return runtime, separator, config


def _set_independent_config(panel_spec, runtime, separator, config):
    options = panel_spec["vizConfig"]["spec"]["options"]
    options["independentTime"] = config
    options["getOption"] = runtime + separator + json.dumps(
        config, ensure_ascii=False, separators=(",", ":")) + ");"


def build_section(datasource, first_id=15):
    elements, items = {}, []
    for index, (title, provider, product, scope, x, y, width, description) in enumerate(CHARTS):
        panel_id = first_id + index
        name = f"panel-{panel_id}"
        queries = []
        for ref_id, direction, label in (("A", "in", "入站"), ("B", "out", "出站")):
            selector = ('job="$scrape_job",instance="$instance",account=~"${account:regex}",'
                        f'product="{product}",scope="{scope}",unit="bit/s",metric="network_{direction}_bps"')
            if provider:
                selector += f',provider="{provider}"'
            expression = (
                f"cloud_monitor_resource_value{{{selector}}}"
                " and on(job,instance,provider,account,job_id,metric_key) "
                f"(cloud_monitor_resource_fresh{{{selector}}} == 1)"
                " and on(job,instance) (cloud_monitor_scheduler_alive{job=\"$scrape_job\",instance=\"$instance\"} == 1)"
                " and on(job,instance) (up{job=\"$scrape_job\",instance=\"$instance\"} == 1)"
            )
            queries.append({"kind": "PanelQuery", "spec": {
                "refId": ref_id, "hidden": False,
                "query": {"kind": "DataQuery", "group": "prometheus", "version": "v0",
                          "datasource": deepcopy(datasource), "spec": {
                              "editorMode": "code", "expr": expression, "format": "time_series",
                              "instant": False, "range": True,
                              "legendFormat": label + " · {{provider}} / {{account}} / {{resource_name}} ({{resource_id}}) / {{region}}",
                          }},
            }})
        limits = (50_000_000, 100_000_000, 200_000_000) if product == "rds" else (100_000_000, 300_000_000, 800_000_000)
        elements[name] = {"kind": "Panel", "spec": {
            "id": panel_id, "title": title, "links": [],
            "description": description + " 单位 bit/s，入出方向分别显示，不相加。"
            "阈值线依次为 P2/P1/P0 初始运维线，实际告警以采集端规则的持续时间为准。",
            "data": {"kind": "QueryGroup", "spec": {
                "queries": queries, "queryOptions": {"maxDataPoints": 1000}, "transformations": [],
            }},
            "vizConfig": {"kind": "VizConfig", "group": "timeseries", "version": "13.2.0", "spec": {
                "fieldConfig": {"defaults": {
                    "unit": "bps", "min": 0, "noValue": "无数据", "color": {"mode": "palette-classic"},
                    "thresholds": {"mode": "absolute", "steps": [
                        {"color": "green", "value": 0},
                        *({"color": color, "value": value} for color, value in zip(("blue", "orange", "red"), limits)),
                    ]},
                    "custom": {
                        "drawStyle": "line", "lineInterpolation": "linear", "lineWidth": 1,
                        "fillOpacity": 0, "showPoints": "never", "spanNulls": False, "insertNulls": False,
                        "stacking": {"mode": "none", "group": "A"},
                        "axisLabel": "bit/s", "axisPlacement": "auto",
                        "scaleDistribution": {"type": "linear"}, "thresholdsStyle": {"mode": "dashed"},
                        "hideFrom": {"tooltip": False, "viz": False, "legend": False},
                    },
                }, "overrides": []},
                "options": {
                    "legend": {"displayMode": "table", "placement": "bottom", "showLegend": True,
                               "calcs": ["lastNotNull", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"},
                },
            }},
        }}
        items.append({"kind": "GridLayoutItem", "spec": {
            "element": {"kind": "ElementReference", "name": name},
            "x": x, "y": y, "width": width, "height": 10,
        }})
    row = {"kind": "RowsLayoutRow", "spec": {
        "title": SECTION_TITLE, "collapse": False,
        "layout": {"kind": "GridLayout", "spec": {"items": items}},
    }}
    return elements, row


def append_section(dashboard):
    if dashboard.get("metadata", {}).get("name") != DASHBOARD_UID:
        raise ValueError(f"Expected the existing dashboard {DASHBOARD_UID}")
    if dashboard.get("apiVersion") != "dashboard.grafana.app/v2" or dashboard["spec"]["layout"]["kind"] != "RowsLayout":
        raise ValueError("Export the Grafana v2 JSON from Edit as code, with RowsLayout")
    result = deepcopy(dashboard)
    spec = result["spec"]
    rows = spec["layout"]["spec"]["rows"]
    if any(row["spec"].get("title") == SECTION_TITLE for row in rows):
        raise ValueError("The network section already exists; refusing to duplicate it")
    datasource = next(v["spec"]["query"]["datasource"] for v in spec["variables"]
                      if v["spec"].get("name") == "account")
    first_id = 1 + max(element["spec"]["id"] for element in spec["elements"].values())
    elements, row = build_section(datasource, first_id)
    if spec["elements"].keys() & elements.keys():
        raise ValueError("Generated panel names conflict with existing elements")
    spec["elements"].update(elements)
    rows.append(row)
    return split_rds_providers(result)


def enable_independent_time(dashboard):
    result = deepcopy(dashboard)
    section, selected, split = _network_section(result)
    spec = result["spec"]
    template = None
    for panel in selected.values():
        panel_spec = panel["spec"]
        queries = _directional_queries(panel_spec)
        existing_viz = panel_spec["vizConfig"]
        if existing_viz["group"] == "volkovlabs-echarts-panel":
            _independent_config(panel_spec, queries)
            for query in queries.values():
                query["spec"]["hidden"] = True
            continue
        if existing_viz["group"] != "timeseries":
            raise ValueError("Unrecognized network chart visualization")
        if template is None:
            template = next((element["spec"]["vizConfig"] for element in spec["elements"].values()
                             if element["spec"]["title"] == "网络带宽使用率趋势"
                             and element["spec"]["vizConfig"]["group"] == "volkovlabs-echarts-panel"), None)
            if template is None:
                raise ValueError("The existing bandwidth chart with independent time controls is required")
            runtime, separator, _ = _runtime_config(template["spec"]["options"])
            original_format = "    if (!Number.isFinite(value)) return '-';\n"
            if runtime.count(original_format) != 1:
                raise ValueError("Unrecognized value formatter in the existing chart template")
            if "if (config.unit === 'bps')" not in runtime:
                runtime = runtime.replace(original_format, original_format + """    if (config.unit === 'bps') {
      const units = ['bit/s', 'kbit/s', 'Mbit/s', 'Gbit/s'];
      const index = value === 0 ? 0 : Math.max(0, Math.min(3, Math.floor(Math.log10(Math.abs(value)) / 3)));
      return (value / Math.pow(1000, index)).toLocaleString('zh-CN', { maximumFractionDigits: config.decimals }) + ' ' + units[index];
    }
""")
        targets = []
        for query in queries.values():
            data_query = query["spec"]["query"]
            targets.append({**deepcopy(data_query["spec"]), "refId": query["spec"]["refId"],
                            "hide": False, "datasource": {"type": "prometheus", "uid": data_query["datasource"]["name"]}})
        colors = {"blue": "#5794f2", "orange": "#ff9830", "red": "#f2495c"}
        thresholds = [{"value": step["value"], "color": colors.get(step["color"], step["color"])}
                      for step in existing_viz["spec"]["fieldConfig"]["defaults"]["thresholds"]["steps"]
                      if step["value"] is not None and step["value"] > 0]
        config = {"dashboardUid": DASHBOARD_UID, "id": panel_spec["id"], "title": panel_spec["title"],
                  "unit": "bps", "decimals": 2, "suffix": " bit/s", "targets": targets, "thresholds": thresholds}
        panel_spec["vizConfig"] = deepcopy(template)
        panel_spec["vizConfig"]["spec"]["fieldConfig"] = {
            "defaults": {"min": 0, "unit": "bps"}, "overrides": []}
        _set_independent_config(panel_spec, runtime, separator, config)
        for query in queries.values():
            query["spec"]["hidden"] = True
    if not split:
        for item in section["spec"]["layout"]["spec"]["items"]:
            item["spec"]["height"] = 12
            if item["spec"]["y"] > 0:
                item["spec"]["y"] = 12
    return result


def split_directions(dashboard):
    """Split the existing network section while retaining each chart's own runtime."""
    result = enable_independent_time(dashboard)
    section, selected, split = _network_section(result)
    if split:
        return result
    spec = result["spec"]
    next_id = 1 + max(panel["spec"]["id"] for panel in spec["elements"].values())
    original_items = {item["spec"]["element"]["name"]: item
                      for item in section["spec"]["layout"]["spec"]["items"]}
    by_title = {LEGACY_TITLE_ALIASES.get(panel["spec"]["title"], panel["spec"]["title"]): (name, panel)
                for name, panel in selected.items()}
    items = []
    for index, chart in enumerate(CHARTS):
        source_name, source_panel = by_title[chart[0]]
        source_spec = source_panel["spec"]
        queries = _directional_queries(source_spec)
        runtime, separator, source_config = _independent_config(source_spec, queries)
        titles = HUAWEI_QUALIFIED_TITLES if source_spec["title"] in LEGACY_TITLE_ALIASES else DIRECTION_TITLES[index]
        for direction_index, direction in enumerate(("in", "out")):
            name = source_name if direction == "in" else f"panel-{next_id}"
            if direction == "out" and name in spec["elements"]:
                raise ValueError("Generated network panel name conflicts with an existing element")
            panel = deepcopy(source_panel)
            panel_spec = panel["spec"]
            panel_spec["id"] = source_spec["id"] if direction == "in" else next_id
            panel_spec["title"] = titles[direction_index]
            panel_spec["data"]["spec"]["queries"] = [deepcopy(queries[direction])]
            config = deepcopy(source_config)
            config["id"] = panel_spec["id"]
            config["title"] = panel_spec["title"]
            config["targets"] = [deepcopy(target) for target in source_config["targets"]
                                 if target["refId"] == queries[direction]["spec"]["refId"]]
            _set_independent_config(panel_spec, runtime, separator, config)
            spec["elements"][name] = panel
            item = deepcopy(original_items[source_name])
            item["spec"].update({"x": direction_index * 12, "y": index * 12,
                                 "width": 12, "height": 12})
            item["spec"]["element"]["name"] = name
            items.append(item)
            if direction == "out":
                next_id += 1
    section["spec"]["layout"]["spec"]["items"] = items
    _network_section(result)
    return result


def _rds_provider_expression(expression, provider):
    prefixes = ("cloud_monitor_resource_value{", "cloud_monitor_resource_fresh{")
    if any(expression.count(prefix) != 1 for prefix in prefixes):
        raise ValueError("Expected one RDS value selector and one freshness selector")
    matchers = re.findall(r'\bprovider\s*(=~|!~|!=|=)\s*"([^"\\]*)"', expression)
    provider_prefix = f'provider="{provider}",'
    if re.search(r'\bprovider\s*(?:=~|!~|!=|=)', expression):
        if (matchers != [("=", provider), ("=", provider)]
                or any(not expression.startswith(provider_prefix, expression.index(prefix) + len(prefix))
                       for prefix in prefixes)):
            raise ValueError("RDS query contains conflicting or unrecognized provider filters")
        return expression
    for prefix in prefixes:
        expression = expression.replace(prefix, prefix + provider_prefix, 1)
    return expression


def split_rds_providers(dashboard):
    """Separate RDS charts by cloud while preserving the four existing ECS charts."""
    result = split_directions(dashboard)
    section, selected, _ = _network_section(result)
    if len(selected) == 8:
        for panel in selected.values():
            panel_spec = panel["spec"]
            for provider, titles in RDS_PROVIDER_TITLES.items():
                if panel_spec["title"] in titles:
                    query = next(iter(_directional_queries(panel_spec).values()))
                    expression = query["spec"]["query"]["spec"]["expr"]
                    if _rds_provider_expression(expression, provider) != expression:
                        raise ValueError("RDS cloud chart is missing its provider filters")
        return result
    spec = result["spec"]
    next_id = 1 + max(panel["spec"]["id"] for panel in spec["elements"].values())
    original_items = section["spec"]["layout"]["spec"]["items"]
    by_title = {panel["spec"]["title"]: (name, panel) for name, panel in selected.items()}
    items = [deepcopy(item) for item in original_items
             if spec["elements"][item["spec"]["element"]["name"]]["spec"]["title"]
             not in DIRECTION_TITLES[2]]
    item_by_name = {item["spec"]["element"]["name"]: item for item in original_items}
    for provider_index, (provider, titles) in enumerate(RDS_PROVIDER_TITLES.items()):
        for direction_index, source_title in enumerate(DIRECTION_TITLES[2]):
            source_name, source_panel = by_title[source_title]
            source_spec = source_panel["spec"]
            queries = _directional_queries(source_spec)
            runtime, separator, source_config = _independent_config(source_spec, queries)
            panel = deepcopy(source_panel)
            panel_spec = panel["spec"]
            if provider_index == 0:
                name = source_name
            else:
                name = f"panel-{next_id}"
                if name in spec["elements"]:
                    raise ValueError("Generated RDS panel name conflicts with an existing element")
                panel_spec["id"] = next_id
                next_id += 1
            panel_spec["title"] = titles[direction_index]
            cloud_name = {"aliyun": "阿里云", "huawei": "华为云"}[provider]
            description = panel_spec.get("description", "").replace("阿里云与华为云 RDS", cloud_name + " RDS")
            if provider == "aliyun":
                description = description.replace("；华为云 RDS 数据可能延迟 5-10 分钟", "")
            panel_spec["description"] = description
            query = next(iter(_directional_queries(panel_spec).values()))
            query["spec"]["query"]["spec"]["expr"] = _rds_provider_expression(
                query["spec"]["query"]["spec"]["expr"], provider)
            config = deepcopy(source_config)
            config.update({"id": panel_spec["id"], "title": panel_spec["title"]})
            config["targets"][0]["expr"] = query["spec"]["query"]["spec"]["expr"]
            _set_independent_config(panel_spec, runtime, separator, config)
            spec["elements"][name] = panel
            item = deepcopy(item_by_name[source_name])
            item["spec"].update({"x": direction_index * 12, "y": (2 + provider_index) * 12,
                                 "width": 12, "height": 12})
            item["spec"]["element"]["name"] = name
            items.append(item)
    section["spec"]["layout"]["spec"]["items"] = items
    _network_section(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--independent-time", action="store_true",
                      help="Add independent time controls to existing network charts")
    mode.add_argument("--split-directions", action="store_true",
                      help="Split the existing three network charts into six directional charts")
    mode.add_argument("--split-rds-providers", action="store_true",
                      help="Split RDS charts by cloud for eight directional network charts")
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("Use a separate output path to retain the original dashboard export")
    dashboard = json.loads(args.input.read_text(encoding="utf-8"))
    if args.split_rds_providers:
        result = split_rds_providers(dashboard)
    elif args.split_directions:
        result = split_directions(dashboard)
    elif args.independent_time:
        result = enable_independent_time(dashboard)
    else:
        result = append_section(dashboard)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
