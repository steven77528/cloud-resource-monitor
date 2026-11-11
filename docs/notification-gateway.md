# 独立通知服务

监控服务将告警事件通过 HTTP 提交通知服务，通知服务负责渠道选择、内容生成、供应商鉴权和实际发送。
阿里云与华为云的采集密钥仍在 `config/config.yaml`；短信、钉钉和邮件配置移到
`config/notifications/notifications.yaml`。通知服务不连接监控数据库，也不挂载采集账号配置。

```text
阿里云 / 华为云 -> 监控采集 -> MySQL 通知队列 -> 独立通知服务 -> 短信 / 钉钉 / 邮件
```

## 项目部署

通知服务与监控服务在同一个 `cloud-resource-monitor` 项目中维护，由项目的 Docker Compose 一起部署。
首次部署和日常更新使用完整项目，参见[项目部署说明](../README.md)。服务器已有的
`config/config.yaml`、`config/notifications/notifications.yaml`、`.env` 和 MySQL 数据需要保留。

## 查看日志与排查

在服务器原项目目录查看当前通知网关的日志：

```sh
cd /data/elk/cloud-resource-monitor
docker compose logs -f --tail=100 notification-gateway
```

也可用 `docker logs -f --tail=100 cloud-resource-monitor-notification-gateway-1` 查看指定容器。
Compose 命令使用服务名 `notification-gateway`；`docker logs` 使用容器名或容器 ID。
容器重建后，旧容器 ID 不再代表当前服务，日常排查优先使用 Compose 服务名。
`docker compose exec ... check` 的输出直接返回执行命令的终端，通常不会出现在服务的 `docker logs` 中。

旧版通知网关关闭了默认 HTTP 访问日志，又没有记录启动和通知处理结果，因此运行正常或发送失败时都可能没有日志。
当前版本将脱敏运行日志写入标准错误，由 Docker 收集：启动为 `gateway_started`，正常退出为
`gateway_stopped`，启动失败为 `gateway_startup_failed`，通知请求为 `gateway_request`。
`GET /healthz` 保持静默，且只表示进程存活，不能证明配置仍存在或供应商发送正常。

请求日志包含服务生成的 `request_id`、归一化后的 `endpoint`、`event_id`、`channel`、HTTP `status`、
处理 `result`、稳定的错误 `reason` 及必要时的配置 `field`。`event_id` 只有符合规范 UUID 时才记录，
其他值显示为 `-`；尚未确定的渠道也显示为 `-`。`result=accepted` 表示下游接受，
`skipped` 表示自动渠道关闭，`rejected` 表示请求未被接受。以下为虚构日志示例：

```text
gateway_request request_id=11d429dfb5a64134bfc28d3f9b7dfc86 endpoint=notify event_id=- channel=- status=503 result=rejected reason=config_file_missing field=config
gateway_request request_id=b02c8e69304341ed856e429414dbd90c3 endpoint=notify event_id=- channel=- status=401 result=rejected reason=authentication_failed field=-
gateway_request request_id=9b227fb98d6c4605bbfbe9f4ab094eddd endpoint=notify event_id=733cbd14-0213-4510-9152-02fa98994cbc channel=sms status=502 result=rejected reason=delivery_failed field=-
```

日志不记录 token、手机号、邮箱、通知正文、供应商 URL、原始请求路径或查询参数，也不输出原始异常文本。
`delivery_failed` 仅说明供应商发送失败，不能据此确定是平台鉴权、业务成功码还是网络问题。

### 配置文件不存在

如果检查返回 `FileNotFoundError` 或日志出现 `reason=config_file_missing`，先核对文件和挂载：

```sh
ls -ld config/notifications config/notifications/notifications.yaml
docker compose exec -T notification-gateway ls -l /app/config/notifications.yaml
```

当前 Compose 将宿主机 `./config/notifications` 目录只读挂载到容器 `/app/config`，所以宿主机
`config/notifications/notifications.yaml` 对应容器 `/app/config/notifications.yaml`。
构建上下文通过 `.dockerignore` 排除了整个 `config` 目录；重新构建镜像不会创建或恢复通知配置。
如果宿主机文件缺失，先恢复服务器原来的正确配置并保留凭据、接收人和开关，不能用空模板替代。
宿主机文件存在但旧容器中不存在时，用下一节的临时容器校验当前 Compose 挂载，校验成功后再重建网关。

### 更新网关代码

将更新的项目代码同步到服务器原目录，保留 `.env` 和现有配置。以下命令依次执行，上一条成功后再执行下一条：

```sh
docker compose build notification-gateway
docker compose run --rm --no-deps notification-gateway --config /app/config/notifications.yaml check
```

`check` 只读取并校验配置，不启动通知监听、不发送通知；成功输出 `{"valid": true}`。
检查通过后重建网关，并查看启动日志：

```sh
docker compose up -d --no-deps --force-recreate notification-gateway
docker compose logs -f --tail=100 notification-gateway
```

网关恢复运行后，监控服务已有的待发队列会按原重试规则提交实际通知。这些更新步骤不调用测试发送接口。
主监控和通知网关共用 `cloud-resource-monitor:1.0.0` 镜像标签；构建会更新标签指向，但不会自动替换已运行的容器。
旧容器在 `docker ps` 中显示镜像 ID 本身不代表镜像损坏。此次仅更新网关代码时无需重启监控服务或 MySQL。

## 旧版兼容：内置通知迁移

以下步骤仅适用于短信、钉钉和邮件凭据仍放在主配置 `notifications` 中的旧版部署。
已有 `notifications.gateway` 和独立通知配置时，无需再次迁移。在完整项目根目录执行迁移脚本，
脚本会保留已有钉钉和邮件凭据，关闭自动短信，
将短信配置改为供应商无关的 HTTP 接口，并生成服务间认证 token。它不会发送消息。

```sh
docker compose build
docker compose run --rm --no-deps --entrypoint python \
  -v "$PWD:/migration" -w /migration cloud-monitor \
  scripts/migrate_notifications.py
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose run --rm --no-deps notification-gateway --config /app/config/notifications.yaml check
docker compose up -d
```

迁移命令会创建新文件，需要对项目目录有写权限。若容器非 root 用户没有写权限，可在迁移命令中添加
`--user "$(id -u):$(id -g)"`。同样可以在已安装 PyYAML 的宿主机执行
`python scripts/migrate_notifications.py`。脚本不会覆盖已存在的独立通知配置，重复执行已迁移配置不会再次修改。
服务间 token 存在 `.env` 的 `NOTIFICATION_GATEWAY_TOKEN`，不要放入截图、日志或版本库。

## 配置边界

主配置只保留通知服务连接信息：

```yaml
notifications:
  gateway:
    url: http://notification-gateway:8081
    token: ${NOTIFICATION_GATEWAY_TOKEN}
    timeout_seconds: 20
```

独立通知配置的结构如下，`notifications` 中放各渠道的实际配置：

```yaml
version: 1
api:
  host: 0.0.0.0
  port: 8081
  token: ${NOTIFICATION_GATEWAY_TOKEN}
routes:
  P0: sms
  P1: dingtalk
  P2: email
notifications:
  sms:
    enabled: false
    provider: http
    url: ""
    encoding: json
    headers: {}
    content_field: content
    extra_body: {}
    success_json: {code: 0}
    templates:
      firing: "[{severity}] {summary} | {account} | {resource_name} | {metric}={value}{unit}"
      recovery: "[已恢复] {summary} | {account} | {resource_name} | {metric}={value}{unit}"
    max_content_length: 1000
    timeout_seconds: 15
  # dingtalk、email 在此填写；旧版迁移时脚本会保留原值，均可设置 enabled。
```

通知服务每次请求读取一份完整配置，只校验本次使用的渠道。
修改渠道 URL、凭据、接收人、模板、`enabled` 或 `routes` 后，下一次请求使用新值，无需重启监控服务。
Compose 挂载整个独立通知配置目录，因此编辑器用临时文件替换原文件也能被识别。
修改 `.env` 需要重建通知服务容器；若更改共享认证 token，还需要同步重建监控容器。
修改监听地址、端口或适配器代码时，只需要重建或重启通知服务。

### 图片通知模板

钉钉与邮件的横幅标题固定为“慧橙云能监控告警”，按 P0 严重红色、P1 警告黄色、P2 通知蓝色、恢复绿色展示。四张 PNG 位于 `cloud_monitor/assets/notification-banners/`，当前项目的 `notifications.dingtalk.banner_urls` 已填写四种状态的实际 HTTPS 图片直链。配置格式如下：

```yaml
banner_urls:
  P0: https://alerts.example.com/images/critical.png
  P1: https://alerts.example.com/images/warning.png
  P2: https://alerts.example.com/images/info.png
  recovery: https://alerts.example.com/images/recovery.png
```

以上地址仅为示例。当前使用的分享链接添加了 `?raw=1`，已确认无需登录即可返回 PNG。各状态可以分别填写；未配置的状态仍显示对应颜色的文字标记。恢复状态使用独立的绿色图片或绿色文字，不沿用原告警等级的图片。机器人安全关键词仍保留在文字正文中。

邮件将 PNG 作为 CID 图片随信附带，同时提供 HTML 和纯文本内容，不需要上传图片。短信保持文字，现有 P0 短信、P1 钉钉、P2 邮件默认路由和渠道开关不因图片模板发生变化。

图片通知无需新增公开网关接口，Compose 继续只映射宿主机 `127.0.0.1:18081`。本次图片地址仅更新当前项目，尚未同步到生产服务器，也未发送真实通知。将地址同步到服务器现有配置后，修改的 `banner_urls` 会在下一次通知请求自动生效，无需重启；应用模板代码则按[更新网关代码](#更新网关代码)重建通知网关。图片预览、完整配置位置与离线生成方法见[通知展示格式](notification-format.md)。

### 配置故障与渠道开关

某个渠道配置错误或发送失败会返回脱敏错误，由监控服务的 MySQL 队列重试；其他渠道的配置不会因此失效。
整份 YAML 语法错误、认证 token 错误或通知服务不可用，会暂时影响全部通知，采集仍继续并保留待发事件。
重试沿用原有次数与退避规则，达到最大次数后标记 `failed`，修复后可使用 `retry-failed` 命令。
正在发送的请求使用读取时的配置；不能撤回已经提交供应商的消息。

`enabled: false` 表示暂停该渠道的自动通知。通知服务处理该事件后，监控将记录标为 `skipped`，
告警状态仍保存，不会标记为已发送，也不会自动转发到其他渠道。已标为 `skipped` 的记录不会在重新启用后补发。
尚未被通知服务处理的待发事件，以及之后的新告警、等级变化或正常重复提醒，按实际处理时的配置决定渠道。
需要把 P0 临时发往钉钉时，可明确设置 `routes.P0: dingtalk`，告警等级仍是 P0。

## 第三方短信接口

默认适配普通 HTTP POST，不绑定阿里云。可配置字段：

| 字段 | 含义 |
| --- | --- |
| `url` | 短信平台实际发送接口；公网使用 HTTPS |
| `encoding` | `json` 或 `form` |
| `headers` | 平台要求的鉴权等请求头；Content-Type 根据 encoding 自动设置 |
| `content_field` | 平台接收短信正文的字段名，例如 `content` 或 `text` |
| `extra_body` | 手机号、apikey 等固定请求参数；不能与正文字段重名 |
| `success_json` | 响应 JSON 必须匹配的顶层字段；例如 `{code: 0}`，数值与字符串严格区分 |
| `templates.firing` | 触发及重复告警正文模板 |
| `templates.recovery` | 恢复通知正文模板 |
| `max_content_length` | 最长正文字符数，超长报错，不静默截断 |

HTTP 必须为 2xx，非空 `success_json` 还需要匹配业务成功码。设为 `{}` 时只检查 HTTP 状态。
禁止自动跟随重定向，以免鉴权头转发到其他地址。请求只发送一次，重试由监控队列负责。
供应商私有签名、加密或特殊多步骤协议需要在通知服务内增加适配，监控侧的事件接口保持不变。

### 云片示例

已核对云片[单条发送接口官方文档](https://www.yunpian.com/official/document/sms/zh_CN/domestic_single_send)。
使用 POST 表单，正文名为 `text`，`code` 为整数 0 表示平台接受成功。下面只替换 `notifications.sms`：

```yaml
sms:
  enabled: false
  provider: http
  url: https://sms.yunpian.com/v2/sms/single_send.json
  encoding: form
  headers: {}
  content_field: text
  extra_body:
    apikey: "填写云片 API Key"
    mobile: "填写接收手机号"
  success_json: {code: 0}
  templates:
    firing: "【已审核签名】{summary}，资源：{resource_name}，当前值：{value}。"
    recovery: "【已审核签名】{summary}已恢复，资源：{resource_name}。"
  max_content_length: 1000
  timeout_seconds: 15
```

该接口仅支持单个手机号，不要直接填写逗号分隔的多个号码。
手机号、签名与整段内容格式均须符合供应商要求，示例文本不代表已经通过审核。
通用正文接口不能绕过供应商的签名、模板审核或限流。其他第三方接口按其文档修改上述字段即可；
现有阿里云、华为云 SDK 适配仍可在通知服务中选择，详见 [渠道配置参考](notifications.md)。

## 单独测试

当前自动短信关闭且 URL 为空，因此不会发送短信。以后填好第三方配置后，可以保持 `sms.enabled: false`，
先通过通知服务的测试接口发送一条，成功后再启用自动短信。

服务器本机调用下列接口，Bearer token 使用 `.env` 的 `NOTIFICATION_GATEWAY_TOKEN`。
手动测试内容原样传给短信接口，不套用触发/恢复模板；签名也应包含在输入内容中。

```sh
curl -X POST http://127.0.0.1:18081/notifications/sms/test \
  -H 'Authorization: Bearer 替换为通知服务token' \
  -H 'Content-Type: application/json' \
  --data-raw '{"content":"【已审核签名】短信接口测试"}'
```

同样支持 `/notifications/dingtalk/test`、`/notifications/email/test`，请求体都是 `{"content":"测试内容"}`。
这些是实际发送接口，调用时可能产生短信费用。接收人和供应商只能从配置读取，请求不能临时覆盖。
内容最长 1000 字符；每个渠道的测试接口每 60 秒最多调用一次，失败也计入冷却，并且不会自动重试。

| HTTP 状态 | 含义 |
| --- | --- |
| 200，`status: accepted` | 下游已接受，不代表手机或邮箱最终收到 |
| 200，`status: skipped` | 自动通知渠道被关闭；手动测试不使用此分支 |
| 400 / 413 / 415 | 请求字段、大小或格式错误 |
| 401 | Bearer token 错误或未提供 |
| 429 | 测试调用过于频繁，按 Retry-After 等待 |
| 502 | 下游发送失败或业务成功条件不满足 |
| 503 | 所选渠道或通知服务配置不完整 |

也保留监控服务的 `/notifications/sms/test` 转发入口（宿主机端口 18080），
它使用主配置 `api.admin_token`，需至少 24 字符；推荐直接调用独立通知服务测试，减少与采集的关联。

## 通知事件接口

`POST /notify` 使用同一 Bearer token，请求体为 `{"event": {...}}`。
事件包含 `id`、`severity`、`phase`、`summary`、`provider`、`account`、`resource_id`、`resource_name`、
`metric`、`value`、`unit`、`observed_at`；现有 `dimensions`、`rule_id` 和 `created_at` 也会保留。
通知服务根据自身的 `routes` 选择渠道，监控侧不携带短信供应商、Webhook 或 SMTP 凭据。

健康检查为 `GET /healthz`，只证明服务进程可用。通知服务中途重启、响应超时或供应商批量部分成功，
均可能出现供应商已接受但监控队列尚未记为成功的情况，后续重试可能重复发送；事件 ID 保持不变。
