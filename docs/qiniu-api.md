# 七牛云官方 API 采集与告警

**七牛 AK/SK 仅在 [独立中转服务](../../cloud-resource-relay/README.md) 保存，中转只进行签名、只读授权和 HTTP 转发。以下目标、分页、换算与指标计算全部由监控端执行，在线请求使用 [V2 中转协议](collection-relay.md)。** 真实凭据留待自行配置部署。

下列协议依据 2026-09-11 核对的七牛官方文档，签名采用官方 Python SDK `qiniu==7.18.0`。监控端原适配器构造未签名查询并解析中转返回的厂商 JSON，鉴权留在中转。尚未用实际七牛账号联调；中转确认凭据权限，监控按业务确认资源目标和可用额度字段。

## 已接入指标

| 任务 kind | 指标 metric | 口径与单位 |
| --- | --- | --- |
| `balance` | `qiniu_cash_balance`、`qiniu_present_balance`、`qiniu_credit_line`、`qiniu_unpaid_money` | 现金余额、赠送余额、授信、未支付金额，原整数除以 100000000，保留 CNY/USD 等币种 |
| `balance` | `qiniu_available_balance` | 仅在明确配置 `available_balance_field` 和 `available_balance_confirmed: true` 后采集 |
| `billing_snapshot` | `qiniu_estimated_cost` | 账期累计预估费用，金额单位同响应 currency，附实际账期和快照截止时间 |
| `billing_snapshot` | `qiniu_budget_used_percent` | 同币种当前账期金额 / 月预算 × 100；月初返回上月快照时不生成当前月预算比例 |
| `resource_packages_month` | `qiniu_package_month_total`、`qiniu_package_month_used`、`qiniu_package_month_remaining` | 当月可用总量、已用量、剩余，单位按 `respack_unit` 保留 |
| `resource_packages_month` | `qiniu_package_remaining_percent` | `month_remain / total_surplus × 100`，分母 0 时不生成比例 |
| `resource_packages` | `qiniu_package_expiry_days`、`qiniu_package_unavailable` | 所选资源包的有效期和不可用状态；未生效、已用完、已过期或时间范围外视为不可用 |
| `resource_packages` | `qiniu_package_data_age_seconds` | 所选包 `data_update_time` 距采集时刻的秒数，超过配置上限时整项采集报异常 |
| `kodo_usage` | `qiniu_storage_bytes`、`qiniu_object_count` | 目标 bucket 标准存储容量与文件数；容量是时点值，不是累计值，也不是磁盘使用率 |
| `cdn_usage` | `qiniu_cdn_flow_bytes`、`qiniu_cdn_bandwidth_bps` | 5 分钟完整窗口的监控流量 / 带宽；按域名和 china/oversea 分开 |
| `cdn_status` | `qiniu_cdn_requests`、`qiniu_cdn_5xx_requests`、`qiniu_cdn_5xx_percent` | 同一完整窗口总请求数、5xx 数及百分比；仅总请求数达到 `minimum_requests` 才生成比例 |

金额先通过 Decimal 换算，原始整数和原始单位保留在明细 dimensions；进入现有 Metric、MySQL 和 Prometheus 的数值使用有限浮点数，因此展示极大整数时可能受双精度格式限制，明细中的 `raw_value` 保留原值。金额身份包含 currency，资源包月概览身份包含计费项目、区域、时段及单位，不跨单位合并。当前费用和预算指标使用稳定的账号、币种身份，月份仅保存在 `billing_period` 中；跨月的新快照覆盖最新值，历史明细仍保留所属账期，正常的新月预算数据可以按既有恢复次数结束上月告警。

这些指标进入原有定时采集、明细存储、规则引擎、通知网关及 Prometheus 指标出口。告警去重、持续时间和恢复策略沿用现有引擎。任务失败、无返回记录、源数据陈旧分别使用现有采集状态，不补成业务值 0。采集器不主动发送消息。

指标统一标记 `source=official_api`。资源包当月概览的显示名称组合为“计费项目 / 区域 / 可用时段 / 单位”，让面板可以直接区分不同适用范围的额度；这只调整显示名称，原有资源及告警身份保持不变。

## 监控端配置

参见 `config/integrations/qiniu.yaml`，账号及任务的启用状态以现有配置为准。主配置通过 `monitoring_files` 引入该文件。填写账号 `relay`、任务固定 `operation` 及 `collector` 采集参数，核对业务规则再按需启用。监控端不接受七牛 AK/SK，也不通过 Compose 传入相关环境变量或密钥目录。

## 监控目标与换算

以下查询目标、预算、单位和分页参数放在监控任务 `options.collector`。中转只配置相应操作的资源访问范围，例如 `scope.buckets` 或 `scope.domains`。财务和资源包列表是账号范围查询，中转不筛选返回记录，监控选定目标不会缩小该接口授权范围。

可用额度文档字段表为 `available_balance`，示例为 `balance`，不能自动回退。默认只采集其他明确的财务字段；确认实际响应后，在监控 balance 任务中设置：

```yaml
options:
  operation: qiniu-balance
  source_max_age_seconds: 7200
  collector:
    available_balance_field: available_balance
    available_balance_confirmed: true
```

如果实际账号确认使用 `balance`，只改此字段值。配置字段未出现在响应中时采集失败，不生成 0 余额。短信、钉钉和邮件的可用性依赖现有通知配置；本次不会启用被关闭的渠道。

余额不足只使用 `qiniu-available-low`：CNY 可用余额 ≤500 元为 P1、≤100 元为 P0。现金余额与可用余额来自官方接口的不同字段，数值可能不同；两者同时按相同阈值告警会对同一充值风险重复提醒，因此已移除 `qiniu-cash-low`，`qiniu-balance.rules` 仅绑定可用余额规则。现金余额仍照常采集并可在看板展示。

升级时需同时更新配置和采集服务代码。服务启动会清理已删除或已解绑业务规则的历史告警状态，取消其 `pending`、`failed` 通知，不把停用规则当作余额恢复，不发送恢复通知；采集失败规则 `collection-error` 仍按原流程处理。具体上线步骤见 [七牛余额告警去重](server-update.md#七牛余额告警去重)。

资源包有效期任务必须配置 `packages: [{order_hash: ..., po_id: ...}]` 明确业务所依赖的包。所有列表翻页完成后再匹配这些 ID，历史未选中包不会产生到期告警。所选包消失、重复、源数据过旧时记录采集异常。业务已切换新资源包时应更新目标；不要无限期保留旧包。月概览接口按官方聚合粒度返回当月数量，不提供对应单一 `po_id`，不能将月概览余额强行分配给某一个订单。

资源包列表和月概览从第 1 页开始，以空页或不足 `page_size` 条为结束，每页最大 200。重复页、重复身份和到达 `max_pages` 仍未结束均报异常，避免只采到前几页却输出看似完整的数量。

## 时间与告警边界

- 费用快照参数使用北京时间零点，08:00 后查询当天快照；08:00 前使用同月前一天的已发布快照。每月 1 日 08:00 前没有允许查询的已发布日期，记录 `source_not_ready`，建议让该每日任务在 08:00 后运行。
- 快照截止日不计入费用，每月 1 日的快照属于上月。代码保存 `billing_period`、`snapshot_cutoff` 和 `source_time`，不将上月累计金额套入当月预算。币种不匹配直接报异常；月初没有当月预算样本时也不伪造 0，旧告警等待后续有效样本确认恢复。
- 资源包 `data_update_time` 默认最多陈旧 172800 秒；监控端在 `options.collector.max_source_age_seconds` 配置原始数据时效，并以任务 `source_max_age_seconds` 校验指标来源时间。中转不判断业务数据年龄。超过上限不会继续用旧数据触发新业务告警。
- Kodo 只查询当天的 5 分钟数据。CDN 查询昨天至今天，覆盖跨零点的完整窗口。两者均保守跳过距当前不足 10 分钟的点，预留 5 分钟窗口加 5 分钟上报延迟；默认最大源数据年龄 1800 秒。Kodo 在零点后最初几分钟可能无完整当天数据，会记录未就绪。
- Kodo/CDN Metric 的 `observed_at` 使用官方数据点时刻，重复返回同一点不会推进持续告警计时。CDN status 每个域名单独请求，避免把聚合响应误标为单域名数据。
- `minimum_requests` 默认 100；低请求量或总量为零时保留请求数，不生成 5xx 比例，已有比例告警需后续足量有效窗口确认恢复。示例规则为 5xx 比例 >1% 持续 600 秒，具体阈值需按业务调整。
- 现金余额低不必然代表停服，授信账户应结合可用额度和结算规则处理。未支付金额仅展示，示例不把所有欠费都当作停服告警。

## 当前范围

未实现月结算单对账、每日费用差额和费用预测；现有快照明细可用于后续按账期分析，不能跨月直接相减。未实现资源包扣减详情核对、低频/归档 Kodo 汇总、CDN 证书管理和 CDN 基线学习。CDN 监控流量不是计费流量，不能乘单价冒充账单。

## 官方依据与验证

- [财务 API](https://developer.qiniu.com/af/10420/financial-external-api-documentation)：余额、预估快照、资源包当月概览及列表。
- [管理凭证](https://developer.qiniu.com/kodo/1201/access-token)：finance / Kodo 使用 Qiniu 签名。
- [Kodo 容量](https://developer.qiniu.com/kodo/3908/statistic-space)、[文件数](https://developer.qiniu.com/kodo/3914/count)：单位及时间粒度。
- [CDN 用量](https://developer.qiniu.com/fusion/13365/fusion-api-usage-stats)、[运营统计](https://developer.qiniu.com/fusion/13366/fusion-api-analytics)、[鉴权](https://developer.qiniu.com/fusion/13360/fusion-api-auth-guide)：QBox 签名，domains 字符串 / 数组的接口差异。

离线运行：`.venv/Scripts/python.exe -m pytest tests/test_qiniu.py -q`。测试使用固定虚构凭据、官方响应结构、可注入网络传输，并对官方 SDK 生成的 Qiniu/QBox 签名作独立 HMAC 比对，不访问生产账号、不调用付费操作、不发送通知。
