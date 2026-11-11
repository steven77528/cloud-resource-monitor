"""Presentation for group and email alerts; raw event values remain unchanged."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
import re
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .notification_banners import BANNER_CID, banner_key


SHANGHAI = ZoneInfo("Asia/Shanghai")
BRAND_TITLE = "慧橙云能监控告警"
STATUS_LABELS = {"P0": "严重", "P1": "警告", "P2": "通知", "recovery": "恢复"}
COLORS = {"P0": "#c62828", "P1": "#f2c200", "P2": "#1677d2", "recovery": "#218739"}
MARKERS = {"P0": "\U0001f534", "P1": "\U0001f7e1", "P2": "\U0001f535", "recovery": "\U0001f7e2"}
METRIC_NAMES = {
    "expiry_days": "剩余有效期", "balance": "账户余额", "balance_amount": "账户余额",
    "package_remaining_percent": "资源包剩余比例", "package_used_percent": "资源包使用率",
    "storage_used_percent": "存储使用率", "bandwidth_used_percent": "带宽使用率",
    "network_in_bps": "网络入站速率", "network_out_bps": "网络出站速率",
    "qps_ratio": "请求量相对基线", "sms_growth_percent": "短信发送量增长率",
    "sms_success_percent": "短信送达成功率", "collection_failed": "采集状态",
    "db_cpu_percent": "数据库 CPU 使用率", "db_memory_percent": "数据库内存使用率",
    "db_disk_used_percent": "数据库磁盘使用率", "db_connections_used_percent": "数据库连接数使用率",
    "db_replication_delay_seconds": "数据库复制延迟", "db_deadlocks_last_hour": "最近一小时死锁次数",
    "db_deadlocks_per_second": "数据库死锁速率",
    "db_row_lock_waits_per_second": "MySQL 行锁等待速率",
    "db_slow_queries_per_second": "慢查询速率", "db_cache_hit_percent": "数据库缓存命中率",
    "db_long_running_queries": "当前执行超过 30 秒的 SQL 数",
    "api_query_success": "平台查询成功状态", "api_query_duration_seconds": "平台查询耗时",
    "api_signature_failed": "响应验签失败", "api_last_success_timestamp_seconds": "上次查询成功时间",
    "huifu_available_balance": "汇付可用余额", "huifu_balance": "汇付账户余额",
    "huifu_frozen_balance": "汇付冻结金额", "huifu_account_normal": "汇付账户正常状态",
    "huifu_settlement_failed": "汇付出金失败", "huifu_settlement_returned": "汇付出金退汇",
    "huifu_settlement_overdue": "汇付出金处理超时", "huifu_certificate_expiry_days": "本地证书剩余有效期",
    "qiniu_cash_balance": "七牛现金余额", "qiniu_available_balance": "七牛可用额度",
    "qiniu_estimated_cost": "七牛账期预估费用", "qiniu_budget_used_percent": "七牛月预算使用率",
    "qiniu_package_remaining_percent": "七牛资源包当月剩余比例",
    "qiniu_package_expiry_days": "七牛所选资源包剩余有效期",
    "qiniu_package_unavailable": "七牛所选资源包不可用", "qiniu_cdn_5xx_percent": "七牛 CDN 5xx 比例",
    "company_verified": "爱签企业实名认证状态", "commercial_expiry_days": "套餐台账剩余有效期",
    "package_remaining": "套餐剩余量", "ledger_age_seconds": "套餐台账年龄",
    "api_quota_remaining_percent": "微信 API 日剩余额度比例",
    "charge_active_remaining_percent": "微信有效资源包剩余比例",
    "charge_active_package_count": "微信有效资源包数量", "package_expiry_days": "微信资源包剩余有效期",
    "subscription_active": "微信订阅有效状态", "subscription_expiry_days": "微信订阅剩余有效期",
    "qualification_verified": "微信资质认证状态", "annual_review_required": "微信年审需求",
    "annual_review_expiry_days": "微信认证年审截止剩余天数",
    "js_error_total_occurrences": "小程序昨日 JS 错误总次数",
    "performance_cost_milliseconds": "小程序每日聚合性能耗时",
}
UNITS = {"days": "天", "CNY": "元", "ratio": "倍", "bool": "", "seconds": "秒",
         "count": "次", "count/s": "次/秒", "ms": "毫秒", "milliseconds": "毫秒", "percent": "%"}
PRODUCT_NAMES = {
    "ssl": "SSL 证书", "domain": "域名", "ecs": "ECS 云服务器", "rds": "RDS 数据库",
    "oss": "OSS", "slb": "负载均衡", "sms": "短信服务", "obs": "OBS", "account": "账号余额",
    "huifu_account": "汇付子账户", "huifu_settlement": "汇付出金",
    "commercial_package": "商业套餐台账", "company_verification": "企业认证", "api": "平台 API",
}
NAMESPACE_NAMES = {
    "acs_alb": "ALB 负载均衡", "acs_alb_dashboard": "ALB 负载均衡",
    "acs_slb_dashboard": "CLB 负载均衡", "acs_nlb": "NLB 负载均衡",
    "acs_oss_dashboard": "OSS Bucket", "acs_vpc_eip": "EIP 弹性公网 IP",
    "acs_ecs_dashboard": "ECS 云服务器", "acs_rds_dashboard": "RDS 数据库",
    "SYS.ECS": "ECS 云服务器", "SYS.RDS": "RDS 数据库",
    "SYS.ELB": "ELB 负载均衡", "SYS.OBS": "OBS Bucket",
}


def _line(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _integer(value: Any) -> str:
    if value is None or value == "":
        return "-"
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "不可用"
        rounded = number.to_integral_value(rounding=ROUND_HALF_UP)
        return "0" if rounded == 0 else format(rounded, "f")
    except (InvalidOperation, ValueError):
        return _line(value)


def _observed(value: Any) -> str:
    try:
        observed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return observed.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return "时间不可用"


def _bit_rate(value: Any) -> str:
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "不可用 bit/s"
        units = ("bit/s", "Kbit/s", "Mbit/s", "Gbit/s", "Tbit/s")
        index = 0
        while abs(number) >= 1000 and index < len(units) - 1:
            number /= 1000
            index += 1
        if 0 < number < Decimal("0.01"):
            return f"大于 0 且小于 0.01 {units[index]}"
        rounded = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{format(rounded, 'f').rstrip('0').rstrip('.') or '0'} {units[index]}"
    except (InvalidOperation, ValueError):
        return f"{_integer(value)} bit/s"


def _resource(event: dict[str, Any]) -> str:
    name, resource_id = _line(event.get("resource_name")), _line(event.get("resource_id"))
    if event.get("metric") == "collection_failed":
        return name if name and name != resource_id else "云资源采集任务"
    dimensions = event.get("dimensions")
    dimensions = dimensions if isinstance(dimensions, dict) else {}
    product = _line(dimensions.get("product"))
    kind = _line(dimensions.get("kind"))
    namespace = _line(dimensions.get("namespace"))
    resource_type = NAMESPACE_NAMES.get(namespace, PRODUCT_NAMES.get(product, ""))
    if namespace == "SYS.VPC":
        resource_type = "公网带宽" if dimensions.get("bandwidth_id") else "EIP 弹性公网 IP"
    if not resource_type:
        resource_type = {"certificates": "SSL 证书", "rds_expiry": "RDS 数据库",
                         "ecs_expiry": "ECS 云服务器", "slb_expiry": "负载均衡"}.get(kind, "")
    if product == "ssl" and dimensions.get("domain"):
        name = _line(dimensions["domain"])
    name = name or resource_id or "未指定资源"
    base_name = name
    if resource_type and resource_type.casefold() not in name.casefold():
        name = f"{resource_type} / {name}"
    if resource_id and resource_id != base_name:
        name += f" ({resource_id})"
    return name


@dataclass(frozen=True)
class AlertPresentation:
    title: str
    heading: str
    status: str
    color: str
    foreground: str
    rows: tuple[tuple[str, str], ...]


def present(event: dict[str, Any]) -> AlertPresentation:
    severity = _line(event.get("severity")) or "P2"
    phase = event.get("phase")
    status = "已恢复" if phase == "recovery" else "通知测试" if phase == "test" else "告警触发"
    summary = _line(event.get("summary")) or "云资源监控"
    metric = _line(event.get("metric"))
    if metric == "collection_failed" and phase == "recovery":
        summary = "云资源采集恢复正常"
    metric_name = METRIC_NAMES.get(metric, metric or "未指定指标")
    if metric in {"network_in_bps", "network_out_bps"}:
        dimensions = event.get("dimensions") or {}
        direction = "入站" if metric == "network_in_bps" else "出站"
        if dimensions.get("scope") == "intranet_estimated":
            metric_name = f"内网{direction}速率（估算）"
        elif dimensions.get("scope") == "intranet":
            metric_name = f"内网{direction}速率"
        elif dimensions.get("scope") == "all":
            prefix = "总网络" if dimensions.get("product") == "rds" else "总网卡"
            metric_name = f"{prefix}{direction}速率"
    if metric == "db_long_running_queries":
        dimensions = event.get("dimensions") or {}
        seconds = _integer(dimensions.get("long_query_seconds", 30))
        metric_name = f"当前执行超过 {seconds} 秒的 SQL 数"
    value = _integer(event.get("value"))
    if metric in {"db_deadlocks_per_second", "db_row_lock_waits_per_second"}:
        try:
            raw_rate = Decimal(str(event.get("value")))
            if raw_rate.is_finite() and 0 < raw_rate < 1:
                value = "大于 0 且小于 1"
        except InvalidOperation:
            pass
    unit = UNITS.get(_line(event.get("unit")), _line(event.get("unit")))
    if metric == "collection_failed" and value in {"0", "1"}:
        measurement = "正常" if value == "0" else "异常"
    elif _line(event.get("unit")) == "bit/s":
        measurement = _bit_rate(event.get("value"))
    else:
        measurement = value + (("" if unit == "%" else " ") + unit if unit else "")
    color = COLORS["recovery"] if phase == "recovery" else COLORS.get(severity, COLORS["P2"])
    rows = (
        ("云平台/账号", f"{_line(event.get('provider'))} / {_line(event.get('account'))}"),
        ("资源", _resource(event)),
        ("指标", f"{metric_name}：{measurement}"),
        ("采集时间", _observed(event.get("observed_at"))),
    )
    return AlertPresentation(f"{status} {summary}", summary, status, color,
                             "#302800" if color == COLORS["P1"] else "#ffffff", rows)


def plain_body(event: dict[str, Any]) -> str:
    alert = present(event)
    return "\n".join([alert.title, *(f"{label}：{value}" for label, value in alert.rows)])


def _markdown(value: str) -> str:
    return re.sub(r"[\\`*_\[\]]", lambda match: "\\" + match[0], escape(value, quote=False))


def dingtalk_markdown(event: dict[str, Any], keyword: str = "",
                      banner_urls: dict[str, str] | None = None) -> dict[str, str]:
    alert = present(event)
    key = banner_key(event)
    label = STATUS_LABELS[key]
    url = (banner_urls or {}).get(key)
    if url:
        # Encode Markdown delimiters in an otherwise validated HTTPS image URL.
        image_url = quote(url, safe=":/?=&%+;,@!~$'*-._")
        lines = [f"![{BRAND_TITLE} · {label}]({image_url})"]
    else:
        lines = [f"### **{MARKERS[key]} {BRAND_TITLE} · {label}**"]
    lines.append(f"**{_markdown(alert.title)}**")
    lines.extend(f"**{label}：** {_markdown(value)}" for label, value in alert.rows)
    # The robot checks its configured keyword in the raw Markdown payload.
    if keyword and keyword not in "\n\n".join(lines):
        lines.append(_line(keyword))
    return {"title": BRAND_TITLE, "text": "\n\n".join(lines)}


def email_html(event: dict[str, Any], banner_src: str = f"cid:{BANNER_CID}") -> str:
    alert = present(event)
    rows = "".join(
        '<tr><th scope="row" align="left" valign="top" '
        'style="width:104px;padding:12px 0;border-bottom:1px solid #e8ebef;'
        'font-weight:700;color:#313843;line-height:1.7;">' + escape(label) + '</th>'
        '<td valign="top" style="padding:12px 0;border-bottom:1px solid #e8ebef;'
        'color:#202733;line-height:1.7;overflow-wrap:anywhere;word-break:break-word;">'
        + escape(value) + '</td></tr>' for label, value in alert.rows
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        '<body style="margin:0;padding:16px 8px;background:#ffffff;'
        'font-family:Arial,\'Microsoft YaHei\',sans-serif;font-size:14px;letter-spacing:0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
        'style="max-width:640px;margin:0 auto;border-collapse:collapse;table-layout:fixed;">'
        f'<tr><td style="padding:0;background:{alert.color};">'
        f'<img src="{escape(banner_src, quote=True)}" '
        f'alt="{BRAND_TITLE} · {STATUS_LABELS[banner_key(event)]}" width="640" '
        'style="display:block;width:100%;max-width:640px;height:auto;border:0;'
        f'color:{alert.foreground};font-size:18px;font-weight:700;"></td></tr>'
        '<tr><td style="padding:20px 24px 4px;color:#202733;">'
        '<p style="margin:0 0 6px;font-size:13px;line-height:1.6;font-weight:700;">'
        + escape(alert.status) + '</p>'
        '<h1 style="margin:0;font-size:18px;line-height:1.6;font-weight:700;'
        'overflow-wrap:anywhere;word-break:break-word;">' + escape(alert.heading) + '</h1></td></tr>'
        '<tr><td style="padding:8px 24px 16px;">'
        '<table cellpadding="0" cellspacing="0" width="100%" '
        'style="border-collapse:collapse;table-layout:fixed;font-size:14px;">'
        + rows + '</table></td></tr></table></body></html>'
    )
