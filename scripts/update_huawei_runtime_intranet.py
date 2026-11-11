"""Replace the four Huawei runtime charts with private-network estimates."""

import argparse
from copy import deepcopy
import json
from pathlib import Path

from append_business_network_panels import _runtime_config, _set_independent_config


DASHBOARD_UID = "cloud-business-runtime-v1"
PANELS = {
    "panel-8": (8, "ecs", "in", "华为云 ECS 总网卡入站速率", "华为云 ECS 内网入站速率（估算）"),
    "panel-9": (9, "ecs", "out", "华为云 ECS 总网卡出站速率", "华为云 ECS 内网出站速率（估算）"),
    "panel-12": (12, "rds", "in", "华为云 RDS 总网络入站吞吐量", "华为云 RDS 内网入站吞吐量（估算）"),
    "panel-13": (13, "rds", "out", "华为云 RDS 总网络出站吞吐量", "华为云 RDS 内网出站吞吐量（估算）"),
}


def _expression(expression, product, direction):
    if (not isinstance(expression, str) or 'provider="huawei"' not in expression
            or f'product="{product}"' not in expression
            or f'metric="network_{direction}_bps"' not in expression
            or 'unit="bit/s"' not in expression):
        raise ValueError("Expected a Huawei directional network rate query")
    if 'scope="all"' not in expression and 'scope="intranet_estimated"' not in expression:
        raise ValueError("Unexpected Huawei network query scope")
    return expression.replace('scope="all"', 'scope="intranet_estimated"')


def update_runtime(dashboard):
    if (dashboard.get("metadata", {}).get("name") != DASHBOARD_UID
            or dashboard.get("apiVersion") != "dashboard.grafana.app/v2"):
        raise ValueError(f"Expected the existing Grafana v2 dashboard {DASHBOARD_UID}")
    result = deepcopy(dashboard)
    for name, (panel_id, product, direction, old_title, title) in PANELS.items():
        element = result["spec"]["elements"][name]
        panel = element["spec"]
        if (element.get("kind") != "Panel" or panel.get("id") != panel_id
                or panel.get("title") not in {old_title, title}):
            raise ValueError(f"Unexpected Huawei runtime panel {name}")
        options = panel["vizConfig"]["spec"]["options"]
        runtime, separator, config = _runtime_config(options)
        if (config.get("dashboardUid") != DASHBOARD_UID or config.get("id") != panel_id
                or config.get("title") != panel["title"]):
            raise ValueError("Independent time configuration does not match the runtime panel")
        queries = panel["data"]["spec"]["queries"]
        targets = config.get("targets", [])
        if len(queries) != 1 or len(targets) != 1:
            raise ValueError("Expected exactly one directional network query")
        query = queries[0]["spec"]["query"]
        if (query.get("group") != "prometheus"
                or query["spec"].get("expr") != targets[0].get("expr")):
            raise ValueError("Independent time target differs from the panel query")
        expression = _expression(query["spec"]["expr"], product, direction)
        query["spec"]["expr"] = expression
        targets[0]["expr"] = expression
        config["title"] = title
        panel["title"] = title
        panel["description"] = (
            "内网速率估算：实例总网络速率减去唯一关联的全部 EIP 同方向公网速率。"
            "使用同一已闭合的 5 分钟平均窗口和严格相同时间戳；入站减公网入站，出站减公网出站。"
            "缺失、过期、负差或关联不明确时不展示估算值；不能覆盖未经过已关联 EIP 的公网路径。"
            "以 MB/s 显示，按 1 MB = 1,000,000 字节换算。"
        )
        _set_independent_config(panel, runtime, separator, config)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dashboard = json.loads(args.input.read_text(encoding="utf-8"))
    result = update_runtime(dashboard)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
