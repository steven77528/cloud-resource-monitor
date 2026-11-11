# 四平台鉴权中转接口

七牛云、汇付、爱签和微信小程序通过独立中转服务保管厂商 AK/SK、私钥及业务 token。**中转只负责鉴权和转发，指标解析、分页、计算、存储及告警全部保留在本监控项目。** 中转返回厂商原始 JSON，原有四平台适配器在监控端继续处理业务数据。

独立服务位于同级 `../cloud-resource-relay`，部署见 [中转 README](../../cloud-resource-relay/README.md)。两端各自包含运行所需代码，可以分别部署，通过 IP 和端口直接 HTTP 通信，也支持原有 HTTPS，不相互导入源码。保留现有账号、任务和通知开关，实际启用状态以两端当前配置为准；本次没有执行生产厂商联调。

## 职责边界

| 内容 | 监控项目 | 中转项目 |
| --- | --- | --- |
| 厂商 AK/SK、私钥、业务 token | 不持有 | 保管并按官方协议注入请求 |
| 公开 AppID、商户 ID、查询目标 | 配置及使用 | 绑定身份并校验访问范围 |
| 官方只读 URL、方法和参数 | 原适配器构造未签名请求 | 按固定接口目录严格验证后签名转发 |
| 汇付请求签名、响应验签 | 读取中转验签结果 | 执行签名与验签 |
| 分页、时间窗口、清单读取、金额换算 | 原适配器执行 | 不执行 |
| 指标、来源时间、阈值、告警、MySQL | 继续使用现有实现 | 不包含 |
| 本地台账、SDK 历史结果、公开证书 | 本地读取 | 不经过中转 |

中转客户端必须获得显式的操作授权，并同时满足 `provider + account + operation + kind` 映射及资源 scope。只读接口目录不包含支付、退款、出金、签署、设置修改或令牌刷新等业务写操作。不允许任意域名、重定向、额外请求头、厂商凭据或鉴权字段透传。

部分官方接口本身只支持账号级查询，例如七牛财务/资源包列表和汇付余额。中转原样返回这些接口的账号范围数据，不按监控目标筛选响应。监控仅选择少量包或子账户不等于凭据获得了逐条记录授权；访问范围需在部署时明确接受，必要时使用独立厂商账号。

## 监控端配置

以七牛 Kodo 为例：

```yaml
accounts:
  - id: qiniu-example
    provider: qiniu
    enabled: false
    relay:
      url: http://192.0.2.10:18443/v1/monitor/collect
      token_file: /app/config/relay/qiniu-example.token
      timeout_seconds: 30

jobs:
  - id: qiniu-kodo-standard
    account: qiniu-example
    kind: kodo_usage
    enabled: false
    interval_seconds: 600
    max_age_seconds: 1800
    rules: []
    options:
      operation: qiniu-kodo-standard
      source_max_age_seconds: 1800
      collector:
        buckets: [CHANGE_ME_BUCKET]
        max_source_age_seconds: 1800
```

在线任务 `options` 包含固定操作名 `operation`、可选来源时效 `source_max_age_seconds` 和 `collector`。原有目标、预算、单位、金额倍率、分页、时间窗口及其他采集参数全部放在 `collector`。这些由监控适配器生成具体只读查询；中转不接收整个监控配置，也不处理阈值、预算或业务计算。

监控账号可保留必要公开身份：爱签 `app_id` 和 `api_mode: openapi`、汇付 `huifu_id`、微信 `appid/token_type/third_party_authorized/permission_sets`。它们必须与中转绑定一致。在线账号不得含厂商 AK/SK、私钥、AppSecret、业务 token 或其文件路径。`relay.token_file` 是单独的受限中转令牌，不是厂商凭据；爱签 `ledger` 和 `openapi_snapshot` 账号为本地文件模式，不配置中转。

`relay.url` 可使用 `http://实际IP:端口/v1/monitor/collect`，HTTP 必须填写字面 IPv4/IPv6 地址、显式端口和该固定路径；IPv6 使用 `[地址]:端口`。示例 `192.0.2.10` 是文档占位地址，需要替换。此模式无需域名或证书，但 token 和响应会明文传输，应使用可信网络并限制来源 IP。也可继续使用 HTTPS 中转接口，客户端仍验证证书及主机名；中转到厂商的请求始终使用 HTTPS。

两种地址均禁止 URL 用户信息、查询串、fragment 和重定向。超时最大 120 秒，应比单次中转上游等待时间留出网络余量。一个监控任务可能分页或查询多个目标，每次上游 HTTP 查询分别经过中转。

令牌文件每次请求重读，支持原子替换轮换。Compose 只读挂载 `config/relay`，不挂载厂商密钥目录。监控和中转共享的认证秘密仅为对应中转令牌；不要复制中转 `config/secrets` 或完整账号配置。

## 中转授权配置

在中转 `config/config.yaml` 为在线采集配置客户端、厂商账号和允许操作。内部 `account` 指向厂商凭据，`monitor_account` 绑定本项目账号。爱签仅使用 OpenAPI 企业认证操作，`monitor_account` 应绑定 `asign-openapi`；套餐台账在监控本地读取。每个客户端只能使用 `clients[].operations` 显式列出的操作。

| 操作 | 中转 scope |
| --- | --- |
| `qiniu-kodo-standard` | `buckets` 列表 |
| `qiniu-cdn-usage`, `qiniu-cdn-status` | `domains` 列表 |
| `asign-company-verification` | `company_selectors` 精确选择条件列表 |
| `wechat-api-quota` | `cgi_paths` 列表 |
| `wechat-charge-packages` | `spu_ids` 列表 |
| `huifu-settlements` | `orders` 中精确原交易日期/流水 |
| 其余支持的在线 kind | `{}`，仅限绑定账号及内置只读接口 |

scope 只决定最多能查询哪些资源；监控 `collector` 决定实际查哪些资源及如何计算。中转没有 `options`、`source_max_age_seconds`、预算或 SLA 配置。

每客户端默认每分钟 60 次转发，按 HTTP 请求计数，分页会消耗多次配额。操作 `minimum_interval_seconds` 默认为 0，允许连续分页；单次上游 `timeout_seconds` 默认 20 秒、最大 120 秒。需要正间隔或调整限流时，应核对现有任务的分页和多目标调用量。

## V2 HTTP 请求

固定入口保留 `POST /v1/monitor/collect`，JSON 协议版本改为整数 `2`，与中转本地配置的 `version: 1` 分开。请求头使用 `Authorization: Bearer <中转专用令牌>`、`Content-Type: application/json`、`Accept: application/json`。

请求含六个身份字段及 `request`。后者仅允许 `method`、`url`、`body`，body 必须为对象或 null。URL 为原适配器构造的未签名官方 HTTPS URL；中转验证其属于该操作的内置接口并注入真实鉴权。请求不能有任意 headers、厂商 token、签名或秘密字段。

```json
{
  "version": 2,
  "request_id": "00112233445566778899aabbccddeeff",
  "provider": "wechat",
  "account": "wechat-example",
  "operation": "wechat-api-quota",
  "kind": "api_quota",
  "request": {
    "method": "POST",
    "url": "https://api.weixin.qq.com/cgi-bin/openapi/quota/get",
    "body": {"cgi_path": "/cgi-bin/message/subscribe/send"}
  }
}
```

`request_id` 每次新生成，格式为 32 位小写十六进制 UUID。一次请求只转发一次上游查询；中转不追加分页、缓存统计或生成指标。严格校验重复字段、未知字段、端点、方法、参数和授权范围，不能将此接口当成通用代理。

## 响应

成功响应逐项回显六个身份字段，附加 `status: "ok"`、`signature_verified`、`response`。不回显 `request`，不含 `metrics` 或 `query_performed`。下面使用虚构额度值：

```json
{
  "version": 2,
  "request_id": "00112233445566778899aabbccddeeff",
  "provider": "wechat",
  "account": "wechat-example",
  "operation": "wechat-api-quota",
  "kind": "api_quota",
  "status": "ok",
  "signature_verified": null,
  "response": {
    "errcode": 0,
    "errmsg": "ok",
    "quota": {"daily_limit": 1000, "used": 100, "remain": 900}
  }
}
```

`response` 为厂商原始 JSON，HTTP 成功不表示厂商业务码成功。监控原适配器验证业务码、字段、目标身份、分页完整性、时间与单位，再生成现有指标。厂商响应可能包含业务信息，监控继续只把原实现允许的指标及维度写入 MySQL/Prometheus，不把整份响应记录到日志或告警。

汇付成功响应必须先在中转通过厂商签名验证，此时 `signature_verified=true`，其他平台为 null。验签失败返回 `signature_invalid`，不会作为业务结果进入监控。余额低、交易失败或企业未认证等正常查询结果仍交由监控判断。

错误响应只含六个身份字段、`status: "error"` 和有限原因代码，不携带厂商秘密、URL、原始错误页面或堆栈。例如：

```json
{
  "version": 2,
  "request_id": "00112233445566778899aabbccddeeff",
  "provider": "wechat",
  "account": "wechat-example",
  "operation": "wechat-api-quota",
  "kind": "api_quota",
  "status": "error",
  "reason": "permission_denied"
}
```

未授权、签名失败、限流或上游不可达分别产生受限原因代码。认证或 HTTP 格式错误可直接返回拒绝状态。两端不兼容的协议版本会失败，不尝试回退直连。

## 本地任务与告警

汇付 `huifu-local-certificate` 继续使用原任务 ID，但不产生中转请求，监控直接读取 `config/integrations/huifu-certificate.pem` 中的公开 X.509 证书。该文件不能包含私钥；普通 RSA 私钥没有有效期。配置为 `options.collector.certificate_file/resource_id/resource_name`，不需要 `operation`。

汇付订单监控清单放在监控端 `config/integrations/huifu-orders.json`。监控读取更新时间、跟踪交易并计算 SLA；中转只按 `scope.orders` 判断原交易身份是否被授权查询。新鲜空清单只生成跟踪数量 0，不发送 HTTP 请求，不产生 API 成功或验签恢复样本。

爱签 `ledger` 台账及 `openapi_snapshot` 历史文件模式不需要厂商秘密，继续监控本地读取。来源年龄、重复样本、连续越界、恢复通知、失败队列、Prometheus 和 Grafana 都由原监控实现处理。中转失败不补业务零值、不刷新上次成功时间、不自动恢复已有告警。

## 部署与迁移

1. 在 `../cloud-resource-relay/config/config.yaml` 填写厂商身份、秘密文件、客户端授权及资源范围。当前占位配置全部关闭，升级时保留已填内容，不能用示例覆盖真实凭据。
2. 生成专用中转令牌，按 [中转部署说明](../../cloud-resource-relay/README.md) 独立启动中转，通过 `http://实际IP:18443/healthz` 检查健康状态，无需域名或证书。若选用 HTTPS，准备受信任证书，健康检查继续验证证书及主机名。
3. 在监控四平台配置填写公开身份、中转地址和令牌路径，将原采集目标/预算/单位/分页放在任务 `options.collector`，并保证目标属于中转 scope。账号、任务和通知当前开关保持原值，准备完成后再按需启用。
4. 监控 Compose 继续移除七牛 AK/SK 环境传入和 `config/credentials` 挂载，保留 `config/relay` 只读令牌挂载。本地台账、订单清单和公开证书使用已有 `config/integrations` 挂载。
5. 本次不删除或撤销既有秘密。若旧监控主机曾存储真实厂商凭据，在业务依赖确认、迁移完成后按维护流程轮换并清理不再需要的副本。仅改配置不会消除旧容器环境或历史备份。
6. 执行离线检查，再按服务器部署步骤更新容器。Compose 挂载或环境有变化时需要重建，单纯 restart 不会移除旧容器原有环境。生产启动会恢复监控调度及通知，开发验证不执行。

```sh
python -m cloud_monitor --config config/config.yaml check
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
```

上述命令不请求中转、不调用厂商、不发送通知。部署后的 `diagnose --job ...` 会通过中转调用真实只读接口，只有准备实际联调时才执行。

## 本地 HTTP / HTTPS 联调

在中转项目安装测试依赖后执行：

```sh
python -m cloud_resource_relay --config config/config.yaml check
python scripts/integration_check.py --monitor-project ../cloud-resource-monitor
python scripts/integration_check.py --monitor-project ../cloud-resource-monitor --scheme https
```

联调默认使用本地 IP 和临时端口上的真实 HTTP 中转；`--scheme https` 另行验证 TLS 链路，保留证书及主机名校验。两种模式均使用现有监控客户端和四平台实际监控适配器，中转只签名/验签及转发模拟厂商 JSON，解析计算在监控端。临时 CA、证书和令牌不安装到全局、不写入正式配置；不会连接真实厂商、业务 MySQL、通知渠道或生产调度。脚本读取两端源码是开发联调依赖，部署的两个项目仍完全独立。
