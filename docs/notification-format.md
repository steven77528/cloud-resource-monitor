# 通知展示格式

## 展示效果

通知顶部使用一张 PNG 横幅，固定标题为 **慧橙云能监控告警**，右侧显示状态标签。下方继续使用可复制的文字显示告警内容、云平台/账号、资源、指标和采集时间。

| 等级或状态 | 图片标签 | 主色 | 项目内图片 |
| --- | --- | --- | --- |
| P0 | 严重 | 红色 `#c62828` | [critical.png](../cloud_monitor/assets/notification-banners/critical.png) |
| P1 | 警告 | 黄色 `#f2c200` | [warning.png](../cloud_monitor/assets/notification-banners/warning.png) |
| P2 | 通知 | 蓝色 `#1677d2` | [info.png](../cloud_monitor/assets/notification-banners/info.png) |
| 已恢复 | 恢复 | 绿色 `#218739` | [recovery.png](../cloud_monitor/assets/notification-banners/recovery.png) |

恢复状态优先于原等级：P0、P1、P2 的恢复均使用绿色“恢复”图片；原等级仍保留在事件中，用于渠道路由和追溯。P2 通知使用蓝色，绿色仅表示恢复。

钉钉的通知标题与正文、邮件的主题与正文（含纯文本版本）不显示 `[P0]`、`[P1]`、`[P2]` 前缀，保留“严重”“警告”“通知”“恢复”中文标签，以及“告警触发”“已恢复”状态。此展示调整不改变内部告警等级、通知路由、告警规则、恢复行为或短信供应商模板。

邮件使用随信附带的 CID 图片，不需要公网图片地址，同时保留 HTML 与纯文本备选内容。钉钉使用 Markdown 图片语法，需要客户端能够直接访问的 HTTPS 图片链接；不使用 `data:` 内嵌图片或 HTML 颜色标签。

当前项目的通知配置已填写四张已上传图片的 HTTPS 直链，分享链接通过 `?raw=1` 返回图片。钉钉尚未配置某个状态的图片链接时，该状态使用对应颜色标记和固定品牌标题；告警详情仍可正常阅读。默认路由仍是 P0 短信、P1 钉钉、P2 邮件，短信不能展示图片。

文字详情标题以及“云平台/账号”“资源”“指标”“采集时间”字段名加粗。邮箱收件列表里的主题字重由邮箱客户端及已读状态决定，不能通过邮件内容强制控制。

时间按 `Asia/Shanghai` 换算，格式固定为 `yyyy-MM-dd HH:mm:ss`。例如 `2026-09-09T10:16:33.699237+00:00` 显示为 `2026-09-09 18:16:33`。

指标数值四舍五入后显示为整数，例如 `11.613487277349538 days` 显示为“剩余有效期：12 天”。采集健康指标显示“采集状态：正常/异常”。这些都是展示处理，原始数据精度、阈值判断和告警频率保持不变。

通知正文不再展示事件编号。内部队列仍保留事件身份用于去重、重试和恢复，不需要数据库迁移。

## 钉钉图片地址

四种状态的实际地址已填写在 `config/notifications/notifications.yaml` 的 `notifications.dingtalk.banner_urls` 中，现有 Webhook、加签、关键词和启用设置保持不变。以后更换图片时更新对应地址即可。下列仅为格式示例，不是当前配置中的实际地址：

```yaml
notifications:
  dingtalk:
    banner_urls:
      P0: https://alerts.example.com/images/critical.png
      P1: https://alerts.example.com/images/warning.png
      P2: https://alerts.example.com/images/info.png
      recovery: https://alerts.example.com/images/recovery.png
```

上传后的文件名和目录可以任意，每个值都填写对应图片的完整 URL。当前使用的分享链接需在末尾添加 `?raw=1`，使其直接返回 PNG，而非文件预览网页。可以只填写已上传状态的地址；未填写的状态使用文字回退。恢复图片未填写时使用绿色恢复文字，不会沿用原告警的红色或黄色图片。

钉钉正文首行形如 `![慧橙云能监控告警 · 警告](https://alerts.example.com/images/warning.png)`。配置的机器人安全关键词仍以原文保留在正文中，图片内的文字不能代替关键词校验；资源名称等动态详情会进行 Markdown 转义。图片链接应能直接返回 PNG，无需登录；配置校验只检查 URL 格式，不代替上传后对实际访问情况的检查。

图片直接随项目保存，不要求开放通知网关。服务器已有图片存储或静态网站均可使用；当前不修改公网入口，也不开放网关的通知发送接口。

## 资源名称

- 阿里云证书优先显示详情接口的 `Common` 域名，其次是列表 `CommonName`、可用的 `Sans`、证书名称、证书 ID。复用已有接口返回，不增加 API 请求；证书 ID 保留在名称后，便于定位。
- 常规指标优先根据监控命名空间显示 ALB、CLB、OSS、华为云 ELB、公网带宽等资源类型。资源名称使用已有 `resource_name`，缺少名称时显示真实资源 ID。
- 采集健康告警显示中文任务类型和配置资源范围，例如“ALB 负载均衡指标采集（配置范围：生产入口 (alb-example)）”。这表示采集任务覆盖的资源范围，不表示已确认其中某一个资源发生故障。
- 旧队列中只有任务 ID 的采集健康事件显示“云资源采集任务”；重新采集产生的新事件会带有详细名称。历史已发送的邮件和钉钉消息不会被修改。
- 如果云监控数据本身没有实例昵称，可在原有 `jobs[].options.metrics[]` 条目填写 `resource_name`，通知就会使用该名称。不会为了美化通知每分钟增加资源列表查询。

## 项目内实现

通知展示与采集逻辑已整合在同一个项目中，相关职责如下：

| 文件 | 职责 |
| --- | --- |
| `cloud_monitor/assets/notification-banners/*.png` | 四张随项目分发的固定品牌横幅 |
| `cloud_monitor/notification_rendering.py` | 统一消息展示、颜色、时间和整数格式 |
| `cloud_monitor/notifiers.py` | 发送钉钉 Markdown、邮件 CID 图片及 HTML/纯文本双格式 |
| `cloud_monitor/providers/aliyun.py` | 使用证书接口返回的域名作为可读名称 |
| `cloud_monitor/service.py` | 为采集健康事件生成中文任务及资源范围 |

项目包含回归测试、[通知样式预览](../preview/index.html)和[离线预览脚本](../scripts/preview_notifications.py)，运行下方预览命令可重新生成展示样例。预览使用虚构域名、资源 ID 和账号，不包含真实资源或凭据。

调整展示格式时直接修改项目内对应实现。服务器更新保留已有配置、`.env` 和任务开关；关闭的华为云证书任务不会因样式变化自动启用。

## 服务器部署

本次图片地址仅已写入当前项目，尚未同步到生产服务器，也未发送真实通知。将 `banner_urls` 的四项地址更新到服务器现有配置后，网关在下一次通知请求时自动读取，无需重启服务。应用图片通知模板代码时，按照[项目部署说明](../README.md)同步项目改动并保留现有配置，然后在服务器项目根目录依次执行：

```sh
docker compose build notification-gateway
docker compose run --rm --no-deps notification-gateway --config /app/config/notifications.yaml check
docker compose up -d --no-deps --force-recreate notification-gateway
docker compose logs --tail=50 notification-gateway
```

`check` 只校验配置，不发送通知。上述重建步骤用于更新通知模板代码，采集服务和 MySQL 无需重启。网关恢复运行后，已有待发队列仍按原规则发送实际通知。仅补充或调整 `banner_urls` 无需执行重建步骤，下一次通知请求自动读取新配置。

## 验证范围

验证使用离线样例与模拟通知接口，不发送真实短信、钉钉或邮件，不启动生产采集调度。需要检查四张 PNG、恢复状态优先级、各状态缺少 URL 时的回退、Markdown 转义、机器人关键词及邮件 CID/纯文本内容。

邮件预览使用实际 HTML 模板；钉钉预览展示四种图片与文字组合，并提供实际生成的 Markdown。预览中的 HTTPS 域名是占位示例；当前配置的四个实际地址已确认可无需登录返回 PNG，最终图片加载、行距和字体仍由钉钉客户端决定，尚未通过真实通知确认。

官方格式依据：[钉钉机器人消息类型](https://open.dingtalk.com/document/development/robot-message-type)。

在项目中重新生成离线预览：

```sh
python scripts/preview_notifications.py --output preview
```

预览脚本会在项目 `preview` 目录生成图片副本、邮件与钉钉预览页面、索引和 `dingtalk.md`。样例仅使用虚构资源，不读取实际发送凭据，也不向外部发送通知。
