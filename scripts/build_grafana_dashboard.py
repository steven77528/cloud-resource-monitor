"""Build the importable Grafana dashboard from small, editable panel definitions."""

from __future__ import annotations

import argparse
import json
from itertools import count
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "grafana" / "cloud-resource-monitor.json"
DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
TARGET = 'instance="$instance"'
ACCOUNT = TARGET + ',provider=~"${provider:regex}",account=~"${account:regex}"'
RESOURCE = ACCOUNT + ',resource_id=~"${resource:regex}"'
LEGEND = "{{account}} / {{resource_name}} / {{job_id}} / {{metric_name}} / {{scope}} / {{region}} / {{unit}} / {{dimension_id}}"
EXPIRY_METRICS = "|".join((
    "expiry_days", "qiniu_package_expiry_days", "huifu_certificate_expiry_days",
    "commercial_expiry_days", "package_expiry_days", "subscription_expiry_days", "annual_review_expiry_days",
))


def metric(name: str, extra: str = "", *, scope: str = RESOURCE) -> str:
    suffix = f",{extra}" if extra else ""
    return f'cloud_monitor_resource_value{{{scope},metric="{name}"{suffix}}}'


def metric_group(names: str, extra: str = "") -> str:
    suffix = f",{extra}" if extra else ""
    return f'cloud_monitor_resource_value{{{RESOURCE},metric=~"{names}"{suffix}}}'


def fields(unit: str = "short", thresholds: list | None = None) -> dict:
    return {
        "defaults": {
            "unit": unit,
            "noValue": "无数据",
            "color": {"mode": "palette-classic"},
            "mappings": [],
            "thresholds": {
                "mode": "absolute",
                "steps": thresholds or [{"color": "green", "value": None}],
            },
            "custom": {},
        },
        "overrides": [],
    }


def threshold(*steps: tuple[str, float | None]) -> list:
    return [{"color": color, "value": value} for color, value in steps]


def build_dashboard() -> dict:
    panels = []
    y = 0
    # Keep retired IDs unused so existing Grafana panel links still resolve.
    panel_ids = (identifier for identifier in count(1) if identifier not in {62, 65, 68})

    def row(title: str) -> None:
        nonlocal y
        panels.append({"id": next(panel_ids), "type": "row", "title": title, "collapsed": False,
                       "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []})
        y += 1

    def panel(title: str, expression: str, *, kind: str = "timeseries", x: int = 0,
              width: int = 12, height: int = 8, unit: str = "short", limits: list | None = None,
              description: str = "", legend: str = LEGEND, minimum: float | None = None,
              maximum: float | None = None, columns: dict | None = None) -> dict:
        config = fields(unit, limits)
        if minimum is not None:
            config["defaults"]["min"] = minimum
        if maximum is not None:
            config["defaults"]["max"] = maximum
        item = {
            "id": next(panel_ids),
            "title": title,
            "type": kind,
            "datasource": DS,
            "gridPos": {"h": height, "w": width, "x": x, "y": y},
            "description": description + (
                " 图中阈值线和颜色仅供参考；实际告警以规则比较符和持续时间为准。"
                if limits and "cloud_monitor_resource_value" in expression else ""
            ),
            "fieldConfig": config,
            "targets": [{"datasource": DS, "refId": "A", "expr": expression,
                         "editorMode": "code", "legendFormat": legend,
                         "format": "table" if kind == "table" else "time_series",
                         "instant": kind in {"stat", "table", "bargauge"},
                         "range": kind == "timeseries"}],
        }
        if kind == "timeseries":
            config["defaults"]["custom"] = {
                "drawStyle": "line", "lineInterpolation": "linear", "lineWidth": 2,
                "fillOpacity": 8, "showPoints": "never", "spanNulls": False,
                "axisCenteredZero": False, "axisColorMode": "text", "axisLabel": "",
                "axisPlacement": "auto", "barAlignment": 0,
                "gradientMode": "none", "pointSize": 4, "scaleDistribution": {"type": "linear"},
                "stacking": {"mode": "none", "group": "A"},
                "thresholdsStyle": {"mode": "dashed"},
                "hideFrom": {"tooltip": False, "viz": False, "legend": False},
            }
            item["options"] = {"legend": {"displayMode": "table", "placement": "bottom",
                                          "calcs": ["lastNotNull", "max"], "showLegend": True},
                               "tooltip": {"mode": "multi", "sort": "desc"}}
        elif kind == "stat":
            config["defaults"]["color"] = {"mode": "thresholds"}
            item["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                               "orientation": "auto", "textMode": "value", "colorMode": "value",
                               "graphMode": "none", "justifyMode": "auto", "wideLayout": True}
        elif kind == "table":
            item["options"] = {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}
            config["defaults"]["custom"] = {"align": "auto", "cellOptions": {"type": "auto"},
                                               "inspect": False, "filterable": True}
            visible = columns or {"account": "账号", "resource_name": "资源", "resource_id": "资源 ID",
                                  "metric": "指标", "Value": "当前值", "unit": "单位",
                                  "region": "地域", "job_id": "任务", "dimension_id": "维度 ID"}
            # Numeric-looking labels are identifiers; only the sample value has a measurement unit.
            config["defaults"]["unit"] = "string"
            if "Value" in visible:
                config["overrides"].append({
                    "matcher": {"id": "byName", "options": visible["Value"]},
                    "properties": [{"id": "unit", "value": unit}],
                })
            known = {"Time", "__name__", "job", "instance", "provider", "account", "job_id",
                     "resource_id", "resource_name", "metric", "unit", "region", "product", "engine",
                     "namespace", "metric_name", "scope", "dimension_id", "metric_key", "rule_id",
                     "severity", "status", "kind", "reason", "Value"}
            known.update({"source", "currency", "billing_period", "spu_id", "package_id", "cost_time_type"})
            # One instant table response preserves every label set without joining on non-unique labels.
            item["transformations"] = [{"id": "organize", "options": {
                "excludeByName": {key: True for key in sorted(known - set(visible))},
                "indexByName": {key: index for index, key in enumerate(visible)},
                "renameByName": visible,
            }}]
        elif kind == "bargauge":
            config["defaults"]["color"] = {"mode": "thresholds"}
            config["defaults"]["displayName"] = "${__field.labels.account} / ${__field.labels.resource_name}"
            item["options"] = {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                               "valueMode": "color", "minVizHeight": 24, "minVizWidth": 0,
                               "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}
        panels.append(item)
        return item

    def alert_count(severity: str) -> str:
        return (f'sum(cloud_monitor_active_alert{{{RESOURCE},severity="{severity}"}}) '
                f'or sum(cloud_monitor_alerts{{{ACCOUNT},severity="{severity}"}} * 0)')

    row("总览")
    for x, severity, color in [(0, "P0", "red"), (4, "P1", "orange"), (8, "P2", "yellow")]:
        panel(f"{severity} 活跃告警", alert_count(severity), kind="stat", x=x, width=4, height=4,
              limits=threshold(("green", None), (color, 1)), minimum=0,
              description="当前资源筛选范围内的活跃告警；只有导出器确认账号告警状态时才显示 0。")
    panel("异常 / 待采集任务", f'sum(cloud_monitor_job_status{{{ACCOUNT},status=~"error|stale|no_data|insufficient_data|degraded|waiting"}})',
          kind="stat", x=12, width=4, height=4, limits=threshold(("green", None), ("red", 1)),
          description="账号范围内 error、stale、no_data、insufficient_data、degraded、waiting 的任务总数；degraded 表示存在不可用估算；不随资源筛选变化。", minimum=0)
    panel("最近到期 / 天", f"min({metric_group(EXPIRY_METRICS)})", kind="stat", x=16, width=4, height=4,
          unit="suffix:天", limits=threshold(("red", None), ("orange", 7), ("green", 30)))
    scheduler = panel("采集调度器 / 全局", f"cloud_monitor_scheduler_alive{{{TARGET}}}", kind="stat",
                      x=20, width=4, height=4, limits=threshold(("red", None), ("green", 1)),
                      description="导出器对应采集调度器的心跳。无数据不代表正常。")
    scheduler["fieldConfig"]["defaults"]["mappings"] = [{"type": "value", "options": {
        "0": {"text": "离线", "color": "red"}, "1": {"text": "运行中", "color": "green"}}}]
    y += 4

    row("告警与采集健康")
    alerts = panel("当前活跃告警", f"cloud_monitor_active_alert{{{RESOURCE}}} == 1", kind="table", width=14,
                   columns={"severity": "级别", "provider": "云厂商", "account": "账号",
                            "resource_name": "资源", "resource_id": "资源 ID", "metric": "指标",
                            "rule_id": "规则", "job_id": "任务"})
    alerts["fieldConfig"]["overrides"] += [{"matcher": {"id": "byName", "options": "级别"},
                                            "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                                           {"id": "mappings", "value": [{"type": "value", "options": {
                                                               "P0": {"color": "red"}, "P1": {"color": "orange"},
                                                               "P2": {"color": "yellow"}}}]}]}]
    jobs = panel("任务状态 / 账号范围", f"cloud_monitor_job_status{{{ACCOUNT}}} == 1", kind="table", x=14, width=10,
                 columns={"account": "账号", "job_id": "任务", "kind": "类型", "status": "状态"})
    jobs["fieldConfig"]["overrides"] += [{"matcher": {"id": "byName", "options": "状态"},
                                          "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                                         {"id": "mappings", "value": [{"type": "value", "options": {
                                                             "ok": {"text": "正常", "color": "green"},
                                                             "no_data": {"text": "无数据", "color": "orange"},
                                                             "insufficient_data": {"text": "统计不足", "color": "orange"},
                                                             "degraded": {"text": "估算不可用", "color": "orange"},
                                                             "error": {"text": "错误", "color": "red"},
                                                             "stale": {"text": "超时", "color": "red"},
                                                             "waiting": {"text": "待采集", "color": "blue"}}}]}]}]
    y += 8
    panel("采样距今时间", f"time() - cloud_monitor_resource_observed_timestamp_seconds{{{RESOURCE}}}",
          unit="s", minimum=0, description="按云端采样时间计算；包括已经过期、当前值已停止导出的序列。")
    panel("通知队列 / 全局", f"cloud_monitor_outbox_messages{{{TARGET}}}", x=12,
          legend="{{status}}", minimum=0, description="当前导出器对应通知发件箱的状态数量；不随账号或资源筛选变化。")
    y += 8
    freshness = panel("数据新鲜度", f"cloud_monitor_resource_fresh{{{RESOURCE}}}", kind="table", width=12,
                      columns={"account": "账号", "resource_name": "资源", "metric": "指标",
                               "Value": "新鲜度", "job_id": "任务", "dimension_id": "维度 ID"})
    freshness["fieldConfig"]["overrides"] += [{"matcher": {"id": "byName", "options": "新鲜度"},
                                               "properties": [{"id": "mappings", "value": [{"type": "value", "options": {
                                                   "0": {"text": "已过期", "color": "red"},
                                                   "1": {"text": "有效", "color": "green"}}}]},
                                                              {"id": "custom.cellOptions", "value": {"type": "color-text"}}]}]
    panel("上次任务执行距今 / 账号范围", f"time() - cloud_monitor_job_last_run_timestamp_seconds{{{ACCOUNT}}}",
          x=12, unit="s", minimum=0, legend="{{account}} / {{job_id}}")
    y += 8

    row("账号余额与资源有效期")
    panel("账号余额趋势 / CNY", metric("balance", 'unit="CNY"'), unit="currencyCNY", minimum=0,
          description="仅展示人民币序列，其他币种保留在全部指标明细中；每个账号独立展示。")
    expiry = panel("到期资源 / 按剩余天数排序", f"sort({metric_group(EXPIRY_METRICS)})", kind="table", x=12,
                   unit="suffix:天", limits=threshold(("red", None), ("orange", 7), ("green", 30)),
                   columns={"account": "账号", "resource_name": "资源", "resource_id": "资源 ID",
                            "product": "产品", "metric": "到期指标", "source": "来源",
                            "Value": "剩余天数", "region": "地域"})
    expiry["options"]["sortBy"] = [{"displayName": "剩余天数", "desc": False}]
    expiry["fieldConfig"]["overrides"] += [{"matcher": {"id": "byName", "options": "剩余天数"},
                                            "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                                           {"id": "color", "value": {"mode": "thresholds"}}]}]
    y += 8
    panel("资源剩余有效期", metric_group(EXPIRY_METRICS), unit="suffix:天",
          legend="{{account}} / {{resource_name}} / {{metric}} / {{source}} / {{region}} / {{unit}}")
    panel("账号余额 / CNY", metric("balance", 'unit="CNY"'), kind="table", x=12, unit="currencyCNY",
          columns={"provider": "云厂商", "account": "账号", "resource_name": "名称", "Value": "可用余额"})
    y += 8

    row("资源包与容量")
    panel("资源包剩余额度", metric("package_remaining_percent"), kind="bargauge", unit="percent", minimum=0,
          maximum=100, limits=threshold(("red", None), ("orange", 10), ("yellow", 20), ("green", 30)))
    panel("资源包使用率趋势", metric("package_used_percent"), x=12, unit="percent", minimum=0, maximum=100)
    y += 8
    for x, name, title, unit, maximum in [
        (0, "storage_used_percent", "存储空间使用率", "percent", 100),
        (8, "qps_ratio", "QPS 配额使用率", "percentunit", None),
        (16, "bandwidth_used_percent", "带宽使用率", "percent", 100),
    ]:
        panel(title, metric(name), x=x, width=8, unit=unit, minimum=0, maximum=maximum,
              description="qps_ratio 原值为比例，1 表示 100%。" if name == "qps_ratio" else "")
    y += 8

    row("短信")
    panel("短信日发送量较前 7 日均值增幅", metric("sms_growth_percent"), unit="percent",
          description="已完成自然日相对前 7 日均值的增幅；不是累计发送量，负值表示下降。统计日期不足或零基线无法计算时不生成指标，不补零。")
    panel("短信回执成功率", metric("sms_success_percent"), x=12, unit="percent", minimum=0, maximum=100,
          description="回执汇总任务的成功率；仅在回执采集任务已启用且有有效回执数据时显示。")
    y += 8

    row("RDS 性能 / 已启用任务")
    percentages = [
        ("db_cpu_percent", "CPU 使用率", 80),
        ("db_memory_percent", "内存使用率", 85),
        ("db_disk_used_percent", "磁盘空间使用率", 80),
        ("db_connections_used_percent", "连接数使用率", 80),
    ]
    for index, (name, title, warn) in enumerate(percentages):
        panel(title, metric(name), x=(index % 2) * 12, unit="percent", minimum=0, maximum=100,
              limits=threshold(("green", None), ("orange", warn), ("red", 90)))
        if index % 2:
            y += 8
    for index, (name, title, unit, warn) in enumerate([
        ("db_replication_delay_seconds", "复制延迟", "s", 3),
        ("db_row_lock_waits_per_second", "MySQL 行锁等待速率", "ops", 1),
        ("db_deadlocks_per_second", "PostgreSQL 死锁速率", "ops", 0.000001),
        ("db_deadlocks_last_hour", "MySQL 最近 1 小时死锁 / 预留", "short", 4),
        ("db_slow_queries_per_second", "慢查询速率", "ops", 10),
        ("db_cache_hit_percent", "缓存命中率", "percent", 95),
        ("db_long_running_queries", "PostgreSQL 超过 5 秒的 SQL", "short", 1),
    ]):
        limits = (threshold(("orange", None), ("green", 95)) if name == "db_cache_hit_percent"
                  else threshold(("green", None), ("orange", warn)))
        description = ""
        if name == "db_deadlocks_last_hour":
            description = "保留原始每小时死锁计数规则；当前原生 API 任务不采集此指标，空面板是预期情况。"
        elif name == "db_row_lock_waits_per_second":
            description = "云端累计行锁等待次数的增量除以采样间隔；与死锁指标分别展示。"
        elif name == "db_long_running_queries":
            description = "原生 PostgreSQL 性能任务中配置的超过 5 秒 SQL 数量。"
        panel(title, metric(name), x=(index % 2) * 12, unit=unit, minimum=0,
              maximum=100 if name == "db_cache_hit_percent" else None,
              limits=limits, description=description)
        if index % 2:
            y += 8
    y += 8

    row("业务平台查询健康")
    panel("平台查询成功状态 / 账号范围", metric("api_query_success", scope=ACCOUNT), kind="table",
          columns={"provider": "平台", "account": "账号", "job_id": "任务", "Value": "成功为 1"},
          description="按账号与任务显示，不随资源筛选变化。")
    panel("平台查询耗时 / 账号范围", metric("api_query_duration_seconds", scope=ACCOUNT),
          x=12, unit="s", minimum=0, description="按账号与任务显示，不随资源筛选变化。")
    y += 8
    panel("距上次成功查询 / 账号范围", f'time() - {metric("api_last_success_timestamp_seconds", scope=ACCOUNT)}',
          unit="s", minimum=0,
          description="首次成功前无数据；后续失败不会更新该时间，旧成功时间仍保留供排障。不随资源筛选变化。")
    panel("汇付响应验签异常 / 账号范围", metric("api_signature_failed", 'provider="huifu"', scope=ACCOUNT), x=12,
          minimum=0, maximum=1,
          description="1 表示已确认验签失败；网络失败不会被当成验签成功。不随资源筛选变化。")
    y += 8

    row("七牛云费用与资源")
    multi_legend = "{{account}} / {{resource_name}} / {{metric}} / {{scope}} / {{region}} / {{unit}}"
    panel("七牛现金与可用余额 / CNY", metric_group("qiniu_cash_balance|qiniu_available_balance", 'unit="CNY"'),
          unit="currencyCNY", legend=multi_legend)
    panel("七牛预估费用 / 按币种账期", metric("qiniu_estimated_cost"), kind="table", x=12,
          columns={"account": "账号", "billing_period": "账期", "Value": "预估费用", "unit": "币种"})
    y += 8
    panel("七牛月预算使用率", metric("qiniu_budget_used_percent"), unit="percent", minimum=0)
    panel("七牛当月资源包剩余比例", metric("qiniu_package_remaining_percent"), x=12,
          unit="percent", minimum=0, maximum=100)
    y += 8
    panel("七牛所选资源包剩余天数", metric("qiniu_package_expiry_days"), unit="suffix:天")
    panel("Kodo 标准存储容量", metric("qiniu_storage_bytes"), x=12, unit="bytes", minimum=0)
    y += 8
    panel("七牛 CDN 带宽", metric("qiniu_cdn_bandwidth_bps"), unit="bps", minimum=0)
    panel("七牛 CDN HTTP 5xx 比例", metric("qiniu_cdn_5xx_percent"), x=12, unit="percent", minimum=0,
          maximum=100, description="只对完整窗口且达到配置最小请求量的数据计算比例。")
    y += 8

    row("汇付子账户与出金")
    panel("汇付子账户可用余额 / CNY", metric("huifu_available_balance", 'unit="CNY"'), unit="currencyCNY")
    panel("汇付子账户冻结金额 / CNY", metric("huifu_frozen_balance", 'unit="CNY"'), x=12, unit="currencyCNY",
          description="冻结资金仅展示，存在冻结金额不等同于业务异常。")
    y += 8
    panel("汇付账户及清单内出金状态",
          metric_group("huifu_account_normal|huifu_settlement_failed|huifu_settlement_returned|huifu_settlement_overdue"),
          kind="table", columns={"account": "账号", "resource_name": "对象", "resource_id": "资源 ID",
                                "metric": "状态指标", "Value": "当前值"})
    panel("汇付本地 X.509 证书剩余天数", metric("huifu_certificate_expiry_days"), x=12, unit="suffix:天")
    y += 8

    row("爱签 OpenAPI 企业认证与套餐台账")
    panel("爱签企业认证状态", metric("company_verified", 'provider="asign"'),
          kind="table", columns={"account": "账号", "resource_id": "主体", "metric": "指标",
                                "source": "来源", "Value": "当前值"})
    panel("爱签套餐台账剩余比例", metric("package_remaining_percent", 'provider="asign",source="ledger"'),
          x=12, unit="percent", minimum=0, maximum=100)
    y += 8
    panel("爱签商业套餐台账剩余天数", metric("commercial_expiry_days", 'provider="asign",source="ledger"'),
          unit="suffix:天")
    panel("爱签套餐台账年龄", metric("ledger_age_seconds", 'provider="asign"'), x=12, unit="s", minimum=0)
    y += 8
    panel("爱签套餐台账剩余数量", metric("package_remaining", 'provider="asign",source="ledger"'), kind="table",
          width=24, columns={"account": "台账账号", "resource_id": "套餐", "Value": "剩余数量", "unit": "计费单位",
                   "source": "来源"})
    y += 8

    row("微信小程序额度、年审与质量")
    panel("微信关键 API 日剩余额度比例", metric("api_quota_remaining_percent", 'provider="wechat"'),
          unit="percent", minimum=0, maximum=100)
    panel("微信付费能力有效包剩余比例", metric("charge_active_remaining_percent", 'provider="wechat"'),
          x=12, unit="percent", minimum=0, maximum=100)
    y += 8
    panel("微信资源包及订阅剩余天数", metric_group("package_expiry_days|subscription_expiry_days", 'provider="wechat"'),
          unit="suffix:天", legend=multi_legend)
    panel("微信认证年审截止剩余天数", metric("annual_review_expiry_days", 'provider="wechat"'),
          x=12, unit="suffix:天", description="仅适用于已授权第三方平台，缺失截止字段不填零。")
    y += 8
    panel("微信资质及付费能力覆盖", metric_group("qualification_verified|annual_review_required|charge_active_package_count|subscription_active", 'provider="wechat"'),
          kind="table", columns={"account": "账号", "resource_name": "对象", "metric": "指标", "Value": "当前值"})
    panel("小程序昨日 JS 错误总次数", metric("js_error_total_occurrences", 'provider="wechat"'), x=12,
          minimum=0, description="按完整自然日统计 PV；不把各错误的 UV 相加为全局去重人数。")
    y += 8
    panel("小程序每日聚合性能耗时", metric("performance_cost_milliseconds", 'provider="wechat"'),
          unit="ms", minimum=0, legend="{{account}} / {{cost_time_type}}",
          description="cost_time_type 1/2/3 分别为启动、下载、初次渲染官方聚合耗时，不是 p95。")
    panel("小程序 JS 错误明细", metric_group("js_error_occurrences|js_error_affected_users", 'provider="wechat"'),
          x=12, kind="table", columns={"account": "账号", "resource_id": "错误标识", "metric": "指标",
                                       "Value": "次数或人数", "scope": "范围"})
    y += 8

    row("全部指标")
    panel("当前有效指标明细", f"cloud_monitor_resource_value{{{RESOURCE}}}", kind="table", width=24, height=12,
          description="保留原始指标值和单位，不将不同单位相加；过期的当前值不会出现在本表中。",
          columns={"provider": "云厂商", "account": "账号", "resource_name": "资源", "resource_id": "资源 ID",
                   "metric": "指标", "Value": "原始值", "unit": "单位", "region": "地域", "engine": "引擎",
                   "namespace": "命名空间", "metric_name": "云指标名", "scope": "范围", "job_id": "任务",
                   "dimension_id": "维度 ID", "metric_key": "序列键", "source": "来源",
                   "currency": "币种", "billing_period": "账期", "spu_id": "SPU", "package_id": "资源包 ID"})
    y += 12

    row("ECS 与 RDS 网络速率")
    network_metrics = "network_in_bps|network_out_bps"
    network_legend = "{{provider}} / {{account}} / {{resource_name}} / {{metric}} / {{region}} / {{dimension_id}}"
    for title, product, network_scope, description, x, width in [
        ("ECS 内网入出速率", "ecs", "intranet",
         "阿里云 ECS 内网收发速率，不含公网流量；源指标为 1 分钟采样。", 0, 12),
        ("ECS 总网卡入出速率", "ecs", "all",
         "华为云 ECS 全部网卡收发速率，可能包含公网映射流量，不等于纯内网流量；源指标为 5 分钟采样。", 12, 12),
        ("RDS 总网络入出吞吐", "rds", "all",
         "RDS 实例整体收发吞吐，无法按访问方区分内网与公网；源粒度按云厂商与引擎为 1 分钟或 5 分钟。", 0, 24),
    ]:
        panel(title, metric_group(network_metrics, f'product="{product}",scope="{network_scope}",unit="bit/s"'),
              x=x, width=width, unit="bps", minimum=0, legend=network_legend,
              limits=threshold(("green", None), ("blue", 50_000_000 if product == "rds" else 100_000_000),
                               ("orange", 100_000_000 if product == "rds" else 300_000_000),
                               ("red", 200_000_000 if product == "rds" else 800_000_000)),
              description=description + " network_in_bps 为入站，network_out_bps 为出站；直接展示速率值，不做 rate()。"
              " 阈值线对应 P2/P1/P0，实际告警持续时间按云厂商和产品规则配置执行。")
        if x == 12:
            y += 8
    y += 8

    row("内网估算质量")
    estimates = panel("内网估算可用性", f"cloud_monitor_network_estimate_available{{{RESOURCE}}}",
                      kind="table", width=24,
                      columns={"provider": "云厂商", "account": "账号", "job_id": "任务",
                               "resource_id": "资源 ID", "region": "地域", "Value": "估算状态",
                               "reason": "最近不可用原因"},
                      description="展示已记录配对成功或负差的资源。负差资源停止展示旧估算值；只有严格更新的有效入出站样本才恢复。云接口调用失败仍标为采集错误。")
    estimates["fieldConfig"]["overrides"] += [
        {"matcher": {"id": "byName", "options": "估算状态"}, "properties": [
            {"id": "mappings", "value": [{"type": "value", "options": {
                "0": {"text": "估算不可用", "color": "orange"},
                "1": {"text": "可用", "color": "green"},
            }}]},
            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
        ]},
        {"matcher": {"id": "byName", "options": "最近不可用原因"}, "properties": [
            {"id": "mappings", "value": [{"type": "value", "options": {
                "negative_difference": {"text": "总网络减公网得到负差", "color": "orange"},
            }}]},
        ]},
    ]

    def query_variable(name: str, label: str, query: str, *, multi: bool = True) -> dict:
        return {"name": name, "label": label, "type": "query", "datasource": DS,
                "query": {"query": query, "refId": f"variable-{name}"}, "definition": query,
                "refresh": 1, "sort": 1, "hide": 0, "multi": multi, "includeAll": multi,
                "allValue": ".*" if multi else None, "options": [],
                "current": {"selected": True, "text": "All", "value": "$__all"} if multi else {},
                "skipUrlSync": False}

    return {
        "id": None, "uid": "cloud-resource-monitor", "title": "云资源监控", "version": 1,
        "schemaVersion": 39, "editable": True, "graphTooltip": 1, "fiscalYearStartMonth": 0,
        "timezone": "Asia/Shanghai", "weekStart": "monday", "style": "dark", "tags": ["cloud-monitor", "云资源"],
        "description": "云资源告警、采集健康、费用、有效期、容量、RDS 性能及 ECS/RDS 网络速率。使用现有 Prometheus 数据源。",
        "refresh": "1m", "time": {"from": "now-24h", "to": "now"},
        "timepicker": {"refresh_intervals": ["30s", "1m", "5m", "15m", "1h"],
                       "time_options": ["1h", "6h", "24h", "3d", "7d", "200h"]},
        "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                                  "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                                  "name": "Annotations & Alerts", "type": "dashboard"}]},
        "links": [], "panels": panels,
        "templating": {"list": [
            {"name": "DS_PROMETHEUS", "label": "Prometheus", "type": "datasource", "query": "prometheus",
             "current": {}, "options": [], "refresh": 1, "hide": 0, "multi": False, "includeAll": False},
            query_variable("instance", "采集服务", "label_values(cloud_monitor_scheduler_alive, instance)", multi=False),
            query_variable("provider", "云厂商", f"label_values(cloud_monitor_job_status{{{TARGET}}}, provider)"),
            query_variable("account", "账号", f'label_values(cloud_monitor_job_status{{{TARGET},provider=~"${{provider:regex}}"}}, account)'),
            query_variable("resource", "资源", f"label_values(cloud_monitor_resource_observed_timestamp_seconds{{{ACCOUNT}}}, resource_id)"),
        ]},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check that committed JSON matches the generator.")
    args = parser.parse_args()
    rendered = json.dumps(build_dashboard(), ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != rendered:
            parser.exit(1, "Grafana dashboard needs rebuilding.\n")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
