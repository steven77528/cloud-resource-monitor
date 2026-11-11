# 汇付斗拱只读监控

**汇付私钥、公钥与在线请求签名/验签仅在 [独立中转服务](../../cloud-resource-relay/README.md) 处理。余额筛选、订单监控清单、SLA、指标和告警全部在监控端；公开 X.509 证书也由监控本地读取。** 在线查询使用 [V2 中转协议](collection-relay.md)，真实凭据留待自行配置部署。

配置入口为 `config/integrations/huifu.yaml`，账号及三个任务默认关闭。在线账号保留公开 `huifu_id` 和 `relay`；任务 `options.collector` 保存子账户、单位和订单清单等原有采集参数。中转只允许余额和授权订单的只读查询，不发起支付、退款、出金。商户关系、产品权限、子账户、币种与金额换算须在实际联调时确认。

## 中转端接入和验签

在可信中转服务配置 `sys_id`、`product_id`、`huifu_id`，由服务端秘密存储提供商户 RSA 私钥及汇付平台公钥。原有适配器支持 PEM 或 Base64 DER 内容，RSA 至少 2048 位。当前监控 Compose 不再挂载这些文件；私钥不能出现在中转响应、明细、指标或日志。

中转按官方 V2 规则对请求 `data` 第一层字段按 ASCII 排序，紧凑 UTF-8 JSON 使用 RSA PKCS#1 v1.5 / SHA256 签名；字符串内的 JSON 保持原样。中转先用汇付公钥验签，监控再检查原始响应 `resp_code="00000000"` 及请求日期/流水绑定，避免误接旧响应。错误只保存安全原因代码，不保存原始返回描述。

## 余额与子账户

`balance` 调用 `POST https://api.huifu.com/v2/trade/acctpayment/balance/query`，`req_date` 和 `acct_date` 使用北京时间当天。官方 `acctInfo_list` 是 JSON 字符串，解析后按配置的 `acct_id` 和 `acct_type` 精确选取，校验 `huifu_id`。选定账户缺失或重复均为采集异常。

| 指标 | 数据来源 | 单位 |
| --- | --- | --- |
| `huifu_available_balance` | `avl_bal` | 配置的币种 |
| `huifu_balance` | `balance_amt` | 配置的币种 |
| `huifu_frozen_balance` | `frz_bal` | 配置的币种 |
| `huifu_account_normal` | `acct_stat=N` 时为 1，其余已知状态为 0 | bool |
| `api_query_success` | 整次查询及字段校验成功为 1，失败为 0 | bool |
| `api_query_duration_seconds` | 本地测量整次任务查询耗时 | 秒 |
| `api_last_success_timestamp_seconds` | 上次成功时间，失败时不更新 | Unix 秒 |
| `api_signature_failed` | 确认验签失败为 1，完整成功为 0；网络失败不伪造验签成功 | bool |

金额先以 Decimal 解析，再乘 `amount_multiplier`，最后转换为当前指标系统的数值。MySQL 明细保留原始金额字符串及 `source_amount_unit`。必须填写已确认的来源单位，并将 `amount_unit_confirmed` 设置为 true；不能将其他平台的分/元换算直接照搬。资源身份为 `huifu_id:acct_type:acct_id`，告警身份额外区分币种。

模板每 10 分钟查询，低余额连续两次触发；API 连续三次失败触发 P1，验签失败立即 P1。余额 100/500 是部署前必须核对的示例阈值。冻结金额只展示，不默认对 `frz_bal>0` 告警。正常账户外的已知状态 C/F/D 可独立告警。一般采集异常使用原有 P2 路由。

## 可选出金查询

监控 `settlements` 任务读取业务维护的 `config/integrations/huifu-orders.json`，以原子替换更新，模板要求 900 秒内更新。监控逐条请求查询，中转 `scope.orders` 仅检查原日期/流水是否被授权，不读取跟踪清单、不计算 SLA。两端需同步所需交易的授权。示例（时间、流水均需换成业务真实值）：

```json
{
  "updated_at": "2026-09-11T09:00:00+08:00",
  "orders": [
    {
      "resource_id": "order-monitor-alias-1",
      "resource_name": "待处理出金",
      "org_req_date": "20260911",
      "org_req_seq_id": "actual-original-request-id",
      "created_at": "2026-09-11T08:00:00+08:00"
    }
  ]
}
```

调用 `POST /v2/trade/settlement/query`，使用原交易日期、流水及当前账号商户号。签名验证通过后，分别输出 `huifu_settlement_failed`（F）、`huifu_settlement_returned`（Y）、`huifu_settlement_overdue`（P 且超配置 SLA），不能把查询成功理解为交易成功。`huifu_tracked_settlements` 是清单条数，不能作为全量出金统计。业务系统须持续提供待处理清单，并保留已完成条目直至至少两次健康查询完成告警恢复；移除条目不会自动恢复原告警。

新鲜的空清单保留 `huifu_tracked_settlements=0`，但本轮没有调用汇付 API，因此不生成 API 健康成功、最后成功时间或验签成功指标，也不会据此恢复原有 API 或验签告警。采集器每轮重置并记录 `query_performed`，只有实际接口请求及完整校验通过后才能确认 API 成功；清单读取、配置或网络失败仍由采集异常监控报告。

## 可选本地证书

`local_certificate` 由监控本地执行，不经过中转。`options.collector.certificate_file` 指向 `/app/config/integrations/huifu-certificate.pem`，另配置 `resource_id` 和 `resource_name`，不需要 `operation`。文件只含公开 PEM X.509 证书，不能含私钥；监控解析 `notAfter` 为 `huifu_certificate_expiry_days`。普通 RSA 密钥不包含有效期。仅解析证书不产生汇付 API 查询成功或验签恢复。

## 验证与官方依据

离线检查：`python -m pytest -q tests/test_huifu.py tests/test_external_integration.py`。测试使用临时生成的两对 RSA 密钥和离线签名响应，不连接商户账号。

填写配置后可运行 `python -m cloud_monitor --config config/config.yaml check --live`，该命令离线检查配置，不调用中转或读取厂商私钥。实际联调使用 `diagnose --job huifu-balance`，会请求中转并触发真实只读查询，但不写 MySQL、不发通知。汇付转发响应必须有 `signature_verified=true`，监控继续验证业务字段；无法验签时直接失败。此次开发没有执行真实账号联调。

官方文档：[余额查询](https://paas.huifu.com/docs/api/#/jyjs/api_jyjs_yuexxcx)、[V2 加签验签](https://paas.huifu.com/open/doc/guide/#/api_v2jqyq)、[出金查询](https://paas.huifu.com/docs/api/#/jyjs/qx/api_cjjycx)。2026-09-11 复核官方页面已补充账户类型与状态枚举；金额单位仍要求商户确认。
