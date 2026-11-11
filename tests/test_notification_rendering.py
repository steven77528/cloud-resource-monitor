from copy import deepcopy
from html import escape

import pytest

from cloud_monitor.notification_banners import BANNER_CID
from cloud_monitor.notification_rendering import (
    BRAND_TITLE, COLORS, MARKERS, dingtalk_markdown, email_html, plain_body, present,
)


STATE_CASES = [
    ("P0", "firing", "P0", "严重"),
    ("P1", "firing", "P1", "警告"),
    ("P2", "firing", "P2", "通知"),
    ("P0", "recovery", "recovery", "恢复"),
    ("P1", "recovery", "recovery", "恢复"),
    ("P2", "recovery", "recovery", "恢复"),
]


@pytest.fixture
def event():
    return {"id": "private-event-id", "phase": "firing", "severity": "P1", "provider": "aliyun",
            "account": "aliyun-example", "resource_id": "25709250", "resource_name": "api.example.com",
            "metric": "expiry_days", "value": 11.613487277349538, "unit": "days",
            "summary": "云资源或套餐包临近到期", "observed_at": "2026-09-09T10:16:33.699237+00:00",
            "dimensions": {"product": "ssl", "domain": "api.example.com"}}


@pytest.mark.parametrize("severity,phase,color,marker", [
    ("P0", "firing", COLORS["P0"], MARKERS["P0"]),
    ("P1", "firing", COLORS["P1"], MARKERS["P1"]),
    ("P2", "firing", COLORS["P2"], MARKERS["P2"]),
    ("P0", "recovery", COLORS["recovery"], MARKERS["recovery"]),
    ("P1", "recovery", COLORS["recovery"], MARKERS["recovery"]),
    ("P2", "recovery", COLORS["recovery"], MARKERS["recovery"]),
])
def test_severity_color_and_recovery_override(event, severity, phase, color, marker):
    event.update(severity=severity, phase=phase)
    assert present(event).color == color
    assert f"background:{color}" in email_html(event)
    markdown = dingtalk_markdown(event)
    assert markdown["title"] == "慧橙云能监控告警"
    assert markdown["text"].startswith(f"### **{marker} 慧橙云能监控告警")
    for content in (present(event).title, present(event).status, markdown["text"],
                    email_html(event), plain_body(event)):
        assert severity not in content
    assert event["severity"] == severity


def test_notification_palette_matches_requested_colors():
    assert COLORS == {"P0": "#c62828", "P1": "#f2c200", "P2": "#1677d2", "recovery": "#218739"}
    assert MARKERS["P2"] == "\U0001f535"


@pytest.mark.parametrize("severity,phase,key,label", STATE_CASES)
def test_dingtalk_banner_selects_state_and_keeps_event_details(event, severity, phase, key, label):
    event.update(severity=severity, phase=phase)
    urls = {state: f"https://images.example.test/{state}.png" for state in ("P0", "P1", "P2", "recovery")}
    markdown = dingtalk_markdown(event, banner_urls=urls)
    assert markdown["title"] == BRAND_TITLE
    assert markdown["text"].splitlines()[0] == f"![{BRAND_TITLE} · {label}]({urls[key]})"
    assert markdown["text"].count("![") == 1
    assert f"**{'已恢复' if phase == 'recovery' else '告警触发'} {event['summary']}**" in markdown["text"]
    assert "**云平台/账号：** aliyun / aliyun-example" in markdown["text"]
    assert "12 天" in markdown["text"]
    assert "2026-09-09 18:16:33" in markdown["text"]


@pytest.mark.parametrize("urls", [None, {}, {"P1": ""}, {"P0": "https://images.example.test/critical.png"}])
def test_missing_banner_url_keeps_readable_text_fallback(event, urls):
    markdown = dingtalk_markdown(event, banner_urls=urls)
    assert markdown["text"].startswith(f"### **{MARKERS['P1']} {BRAND_TITLE} · 警告**")
    assert "![" not in markdown["text"]
    assert "12 天" in markdown["text"]


def test_recovery_does_not_reuse_warning_image_when_recovery_url_missing(event):
    event["phase"] = "recovery"
    url = "https://images.example.test/warning.png"
    markdown = dingtalk_markdown(event, banner_urls={"P1": url})
    assert markdown["text"].startswith(f"### **{MARKERS['recovery']} {BRAND_TITLE} · 恢复**")
    assert "已恢复" in markdown["text"]
    assert url not in markdown["text"]


def test_keyword_stays_verbatim_after_first_line_banner_and_url_delimiters_are_encoded(event):
    keyword = "cloud_alert_[production]"
    url = "https://images.example.test/banner(1)[warning].png?version=2&name=%E4%BA%91"
    markdown = dingtalk_markdown(event, keyword, {"P1": url})
    expected_url = "https://images.example.test/banner%281%29%5Bwarning%5D.png?version=2&name=%E4%BA%91"
    assert markdown["text"].splitlines()[0] == f"![{BRAND_TITLE} · 警告]({expected_url})"
    assert markdown["text"].splitlines()[-1] == keyword
    assert markdown["text"].count(keyword) == 1


@pytest.mark.parametrize("severity,phase,key,label", STATE_CASES)
def test_email_banner_has_matching_state_alt_and_inline_content_id(event, severity, phase, key, label):
    event.update(severity=severity, phase=phase)
    html = email_html(event)
    assert f'<img src="cid:{BANNER_CID}"' in html
    assert f'alt="{BRAND_TITLE} · {label}"' in html
    assert f"background:{COLORS[key]}" in html
    assert html.count("<img ") == 1
    assert "12 天" in html
    assert "2026-09-09 18:16:33" in html
    assert '<img src="http' not in html


def test_email_banner_escapes_source_and_does_not_take_alt_text_from_event(event):
    source = 'cid:banner" onerror="bad()&<test>'
    event["summary"] = '<img src="https://images.example.test/injected.png">'
    html = email_html(event, source)
    assert f'src="{escape(source, quote=True)}"' in html
    assert f'alt="{BRAND_TITLE} · 警告"' in html
    assert html.count("<img ") == 1
    assert escape(event["summary"]) in html
    assert ' onerror="bad()' not in html


def test_all_fields_are_formatted_without_changing_event(event):
    original = deepcopy(event)
    html, markdown, plain = email_html(event), dingtalk_markdown(event)["text"], plain_body(event)
    for content in (html, markdown, plain):
        assert "2026-09-09 18:16:33" in content
        assert "12 天" in content
        assert "SSL 证书 / api.example.com (25709250)" in content
        assert "aliyun / aliyun-example" in content
        assert "11.613" not in content
        assert "事件编号" not in content and "private-event-id" not in content
    for label in ("云平台/账号", "资源", "指标", "采集时间"):
        assert f"**{label}：**" in markdown
        assert f'font-weight:700;color:#313843;line-height:1.7;">{label}</th>' in html
    assert markdown.startswith("### **")
    assert event == original


@pytest.mark.parametrize("value,expected", [(2.5, "3"), (2.49, "2"), (-2.5, "-3"),
                                           (-0.1, "0"), (0, "0"), (99.99, "100")])
def test_rounding_is_half_up_not_bankers(event, value, expected):
    event.update(metric="bandwidth_used_percent", value=value, unit="%")
    assert dict(present(event).rows)["指标"] == f"带宽使用率：{expected}%"


@pytest.mark.parametrize("value,expected", [(0.016667, "大于 0 且小于 1"), (0, "0"), (2.5, "3")])
def test_positive_deadlock_rate_is_not_displayed_as_zero(event, value, expected):
    event.update(metric="db_deadlocks_per_second", value=value, unit="count/s")
    assert dict(present(event).rows)["指标"] == f"数据库死锁速率：{expected} 次/秒"
    assert event["value"] == value


def test_postgres_api_long_query_label_uses_configured_five_seconds(event):
    event.update(metric="db_long_running_queries", value=1, unit="count",
                 dimensions={"long_query_seconds": 5})
    assert "当前执行超过 5 秒的 SQL 数" in plain_body(event)
    assert "30 秒" not in plain_body(event)


def test_mysql_row_lock_alert_is_named_independently_from_deadlocks(event):
    event.update(metric="db_row_lock_waits_per_second", value=1.2, unit="count/s",
                 summary="MySQL 行锁等待频繁，持续 5 分钟")
    assert dict(present(event).rows)["指标"] == "MySQL 行锁等待速率：1 次/秒"
    assert "死锁" not in plain_body(event)
    assert event["value"] == 1.2
    event.update(phase="recovery", value=0.8)
    assert "大于 0 且小于 1 次/秒" in plain_body(event)


@pytest.mark.parametrize("timestamp,expected", [
    ("2026-09-09T20:59:59.999999Z", "2026-09-10 04:59:59"),
    ("2026-09-10T04:59:59+08:00", "2026-09-10 04:59:59"),
    ("2026-09-09T13:59:59-07:00", "2026-09-10 04:59:59"),
    ("invalid", "时间不可用"),
])
def test_timestamp_conversion_preserves_instant_and_drops_subseconds(event, timestamp, expected):
    event["observed_at"] = timestamp
    assert dict(present(event).rows)["采集时间"] == expected


def test_recovery_health_message_uses_readable_task_and_no_job_id(event):
    event.update(metric="collection_failed", phase="recovery", value=0, unit="bool",
                 resource_id="aliyun-slb-bandwidth", resource_name="ALB 负载均衡指标采集",
                 summary="云资源采集失败，请检查权限和接口")
    for content in (email_html(event), plain_body(event), dingtalk_markdown(event)["text"]):
        assert "云资源采集恢复正常" in content
        assert "ALB 负载均衡指标采集" in content
        assert "采集状态：正常" in content
        assert "aliyun-slb-bandwidth" not in content
        assert "collection_failed" not in content


def test_old_queued_health_event_does_not_show_job_id(event):
    event.update(metric="collection_failed", resource_id="old-task-id", resource_name="old-task-id")
    assert dict(present(event).rows)["资源"] == "云资源采集任务"


def test_markup_from_cloud_names_cannot_inject_html_or_markdown_links(event):
    event.update(summary='<img src=x onerror="bad()">', resource_name="[click](https://example.test)")
    event["dimensions"] = {}
    html, markdown = email_html(event), dingtalk_markdown(event)["text"]
    assert '<img src=x' not in html + markdown
    assert '&lt;img' in html + markdown
    assert "[click](https://example.test)" not in markdown
    assert r"\[click\](https://example.test)" in markdown


def test_resource_id_is_not_duplicated_when_no_name_available(event):
    event.update(resource_name="", resource_id="cert-123", dimensions={"product": "ssl"})
    assert dict(present(event).rows)["资源"] == "SSL 证书 / cert-123"


def test_keyword_is_present_verbatim_in_raw_markdown(event):
    keyword = "cloud_alert"
    event["summary"] = keyword
    assert keyword in dingtalk_markdown(event, keyword)["text"]


@pytest.mark.parametrize("namespace,dimensions,expected", [
    ("acs_alb", {"product": "oss"}, "ALB 负载均衡 / gateway"),
    ("acs_slb_dashboard", {"product": "oss"}, "CLB 负载均衡 / gateway"),
    ("SYS.VPC", {"bandwidth_id": "bandwidth-1"}, "公网带宽 / gateway"),
    ("SYS.ELB", {}, "ELB 负载均衡 / gateway"),
    ("acs_ecs_dashboard", {"product": "oss"}, "ECS 云服务器 / gateway"),
    ("acs_rds_dashboard", {"product": "oss"}, "RDS 数据库 / gateway"),
    ("SYS.ECS", {}, "ECS 云服务器 / gateway"),
    ("SYS.RDS", {}, "RDS 数据库 / gateway"),
])
def test_namespace_overrides_legacy_default_product(event, namespace, dimensions, expected):
    event.update(resource_name="gateway", resource_id="resource-1",
                 dimensions={**dimensions, "namespace": namespace})
    assert dict(present(event).rows)["资源"] == expected + " (resource-1)"


@pytest.mark.parametrize("value,expected", [
    (0, "0 bit/s"), (0.5, "0.5 bit/s"), (0.001, "大于 0 且小于 0.01 bit/s"),
    (999, "999 bit/s"), (1000, "1 Kbit/s"), (1250000, "1.25 Mbit/s"),
    (2500000000, "2.5 Gbit/s"), (1000000000000, "1 Tbit/s"),
    ("NaN", "不可用 bit/s"),
])
def test_network_notification_formats_bits_per_second_without_mutating_raw_event(event, value, expected):
    event.update(metric="network_in_bps", value=value, unit="bit/s",
                 dimensions={"product": "ecs", "scope": "intranet"})
    original = deepcopy(event)
    assert dict(present(event).rows)["指标"] == f"内网入站速率：{expected}"
    for content in (plain_body(event), email_html(event), dingtalk_markdown(event)["text"]):
        assert expected in content
    assert event == original


@pytest.mark.parametrize("product,scope,metric,expected", [
    ("ecs", "intranet", "network_out_bps", "内网出站速率"),
    ("ecs", "intranet_estimated", "network_in_bps", "内网入站速率（估算）"),
    ("rds", "intranet_estimated", "network_out_bps", "内网出站速率（估算）"),
    ("ecs", "all", "network_in_bps", "总网卡入站速率"),
    ("rds", "all", "network_out_bps", "总网络出站速率"),
    ("ecs", "", "network_in_bps", "网络入站速率"),
])
def test_network_notification_preserves_collection_scope(event, product, scope, metric, expected):
    event.update(metric=metric, value=1000000, unit="bit/s",
                 dimensions={"product": product, "scope": scope})
    assert dict(present(event).rows)["指标"] == f"{expected}：1 Mbit/s"
