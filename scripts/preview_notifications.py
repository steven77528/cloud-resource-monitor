"""Generate offline email/Markdown examples using fictional resources; never send."""

import argparse
from html import escape
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_monitor.notification_banners import BANNER_FILES, banner_bytes, banner_key  # noqa: E402
from cloud_monitor.notification_rendering import (  # noqa: E402
    BRAND_TITLE, dingtalk_markdown, email_html, present,
)


def dingtalk_preview(event, image_src):
    alert = present(event)
    rows = "".join(f'<p><strong>{escape(label)}：</strong>{escape(value)}</p>' for label, value in alert.rows)
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>钉钉通知预览</title><style>'
        'body{margin:0;padding:12px;background:white;color:#202733;'
        'font:14px Arial,"Microsoft YaHei",sans-serif;letter-spacing:0}'
        'article{max-width:600px;margin:auto}img{display:block;width:100%;height:auto}'
        'h1{font-size:16px;line-height:1.7;margin:18px 0 12px}'
        'p{font-size:14px;line-height:1.8;margin:12px 0}h1,p{overflow-wrap:anywhere}'
        '</style></head><body><article>'
        f'<img src="{escape(image_src, quote=True)}" alt="{BRAND_TITLE}" width="1200" height="220">'
        f'<h1>{escape(alert.title)}</h1>{rows}</article></body></html>'
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("preview"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    banners = args.output / "banners"
    banners.mkdir(exist_ok=True)
    for key, filename in BANNER_FILES.items():
        (banners / filename).write_bytes(banner_bytes(key))
    sample_urls = {key: f"https://alerts.example.com/banners/{filename}" for key, filename in BANNER_FILES.items()}
    base = {
        "id": "offline-preview-event", "provider": "aliyun", "account": "aliyun-example",
        "phase": "firing", "resource_id": "cert-example", "resource_name": "api.example.com",
        "metric": "expiry_days", "value": 11.613487277349538, "unit": "days",
        "summary": "云资源或套餐包临近到期", "observed_at": "2026-09-09T10:16:33.699237+00:00",
        "dimensions": {"product": "ssl", "domain": "api.example.com"},
    }
    examples = [
        ("严重", {**base, "severity": "P0", "value": 2.6}),
        ("警告", {**base, "severity": "P1"}),
        ("通知", {**base, "severity": "P2", "metric": "collection_failed", "value": 1, "unit": "bool",
                "resource_id": "aliyun-slb-bandwidth", "dimensions": {},
                "resource_name": "ALB 负载均衡指标采集（配置范围：生产入口 (alb-example)）",
                "summary": "云资源采集失败，请检查权限和接口"}),
        ("已恢复", {**base, "severity": "P2", "phase": "recovery", "metric": "collection_failed",
                   "resource_id": "aliyun-slb-bandwidth", "value": 0, "unit": "bool", "dimensions": {},
                   "resource_name": "ALB 负载均衡指标采集（配置范围：生产入口 (alb-example)）",
                   "summary": "云资源采集失败，请检查权限和接口"}),
    ]
    fragments, markdown = [], []
    for index, (label, event) in enumerate(examples):
        key = banner_key(event)
        image_src = f"banners/{BANNER_FILES[key]}"
        html = email_html(event, image_src)
        filename = f"email-{index + 1}.html"
        (args.output / filename).write_text(html, encoding="utf-8")
        ding_filename = f"dingtalk-{index + 1}.html"
        (args.output / ding_filename).write_text(dingtalk_preview(event, image_src), encoding="utf-8")
        fragments.append(
            f'<section><div class="section-title"><h2>{escape(label)}</h2>'
            f'<nav><a href="{image_src}">PNG 图片</a><a href="{filename}">邮件</a>'
            f'<a href="{ding_filename}">钉钉</a></nav></div>'
            f'<iframe title="{escape(label)}钉钉预览" src="{ding_filename}"></iframe></section>'
        )
        markdown.append(dingtalk_markdown(event, banner_urls=sample_urls)["text"])
    overview = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>通知预览</title><style>'
        'body{margin:0;padding:24px;background:#f4f6f8;color:#202733;'
        'font:14px Arial,"Microsoft YaHei",sans-serif;letter-spacing:0}'
        'h1{font-size:22px;margin:0 0 24px;line-height:1.5}h2{font-size:14px;margin:0}'
        '.section-title{display:flex;gap:12px;align-items:center;justify-content:space-between;'
        'flex-wrap:wrap;margin-bottom:12px}nav{display:flex;gap:16px}a{color:#1669ad}'
        'main{max-width:1400px;margin:auto;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px}'
        'iframe{width:100%;height:370px;border:0;background:white;display:block}'
        '@media(max-width:720px){body{padding:12px}main{grid-template-columns:minmax(0,1fr)}}'
        f'</style></head><body><h1>{BRAND_TITLE}</h1><main>' + "".join(fragments) + '</main></body></html>'
    )
    (args.output / "index.html").write_text(overview, encoding="utf-8")
    (args.output / "dingtalk.md").write_text("\n\n---\n\n".join(markdown) + "\n", encoding="utf-8")
    print(str((args.output / "index.html").resolve()))


if __name__ == "__main__":
    main()
