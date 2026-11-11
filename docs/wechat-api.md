# 微信小程序采集与告警

**微信 AppSecret、access_token 或 authorizer_access_token 仅由 [独立中转服务](../../cloud-resource-relay/README.md) 或业务后端保管。中转只验证权限、注入鉴权并转发原始响应；目标、分页、额度与性能计算、指标和告警全部在监控。** 在线查询使用 [V2 中转协议](collection-relay.md)，真实凭据留待自行配置部署。

官方能力核查日期为 2026-09-11。配置片段位于 `config/integrations/wechat.yaml`；新账号和 5 个任务默认关闭。主配置通过 `monitoring_files` 加载后沿用现有调度、MySQL、告警、通知网关及 Prometheus/Grafana。

## 已实现的范围

| 任务 kind | 数据与计算 | 告警口径 |
| --- | --- | --- |
| `api_quota` | 每个明确配置的 CGI 路径的日总额度、已用、剩余和剩余百分比 | 剩余 <=20% 提醒、<=10% 升级，示例要求连续 2 次 |
| `charge_packages` | 按 SPU 拉全资源包分页，逐包保存总量/用量/余量/状态/到期；同 SPU 当前有效包汇总 | 有效包余量 <=20%/10%，到期 <=30/7 天，明确监控的 SPU 没有有效包时提醒 |
| `charge_packages` 的 `subscription` 商品 | 订阅开始/截止时间、当前是否生效 | 订阅到期 <=30/7 天或当前未生效；不计算资源包余量比例 |
| `annual_review` | 资质认证状态、是否需要年审、年审截止剩余天数 | 应认证但未认证、需要年审、截止 <=30/7 天 |
| `js_errors` | 北京时间昨日完整日窗口，全分页采集每个错误 hash 的 PV/UV，并统计错误分组数和 PV 合计 | 示例按全日错误次数阈值告警；各错误 UV 不相加，未输出“全局去重人数” |
| `performance` | 解析官方 JSON 字符串，选择最新完整日期的启动/下载/初次渲染聚合耗时 | 示例只对启动耗时 >=3000 ms 提醒；值不是 p95，也不是分钟实时性能 |

上述阈值都是可调整的业务示例，不代表微信官方阈值。JS 错误按固定完整日的数据时间保存，相同日期重复查询不会充当多个连续告警样本；恢复需新的完整日健康数据。每个错误 hash 的明细用于排查，默认告警使用账号级 PV 合计，避免某个错误次日消失却没有对应恢复样本。

## 中转端令牌与授权参考

可信中转服务与业务服务端统一管理微信凭据，复用业务令牌时必须在中转侧校验所属应用和失效时间。以下为中转端可复用的缓存格式，禁止将缓存挂载或传给监控端。普通小程序使用：

```json
{"access_token":"业务系统写入的令牌","expires_at":1790000000,"appid":"实际小程序AppID"}
```

第三方平台代调用使用：

```json
{"authorizer_access_token":"业务系统写入的授权令牌","expires_at":1790000000,"appid":"被授权小程序AppID"}
```

`expires_at` 为绝对 Unix 秒，示例值只是格式演示；业务程序应写入真实失效时间。`appid` 字段必填，必须与中转端绑定的目标应用相同；由业务凭据管理程序按实际令牌所属小程序写入，不应人为改写来绕过校验。官方额度文档要求“查询的接口属于小程序，则需要用小程序的 access_token”，但额度和资源包响应不包含可核对的 AppID，因此缓存必须提供这项来源绑定。原适配器每轮重读文件，剩余有效期 <=30 秒报 `token_cache_expired`，不会自行刷新令牌；业务程序在中转端采用原子替换更新并限制读取权限。

监控 Compose 只挂载 `config/relay` 中的中转专用令牌，原 `config/credentials` 挂载已移除。中转转发厂商原始 JSON，不返回业务令牌或签名秘密；微信业务错误码和 JS 明细由监控原适配器处理，指标、日志和告警不保存原始错误全文或堆栈。鉴权/转发失败使用 [中转协议](collection-relay.md) 的有限原因代码。

两端公开身份声明须一致，普通小程序设置 `token_type: access_token`。第三方代调用设置如下，业务 token 文件只在中转配置：

```yaml
token_type: authorizer_access_token
third_party_authorized: true
permission_sets: [18, 30]
```

上述配置是接入方对已取得授权的声明，不会申请权限或绕过官方校验。资源包、JS/性能的第三方代调用要求权限集 18；认证年审任务必须使用第三方令牌、明确授权以及权限集 30。仅持有 AppID/AppSecret 的普通小程序不能启用年审 API。监控检查公开声明与任务适用性，中转还校验实际业务令牌和授权身份。

官方年审文档“调用方式”为 POST，而示例仍写 GET。监控 `options.collector.method` 默认 POST，按官方调试工具或实际 SDK 确认后可配置 GET；中转只允许内置目录中该只读基本信息接口的方法与字段，不接受任意方法或 URL。

## 配置步骤

1. 在中转绑定微信应用及业务令牌，为监控签发只允许指定账号/操作的专用令牌，并配置 CGI/SPU 等资源访问范围。
2. 监控 `options.collector.cgi_paths` 使用 `/cgi-bin/...` 等 CGI 路径，不含域名、查询串或令牌；`/sns/` 类接口不受此额度 API 支持。所选 CGI 须属于中转 `scope.cgi_paths`。
3. 监控 `options.collector.products` 按实际购买内容填写 `spu_id`、商品类型及单位，SPU 须属于中转 `scope.spu_ids`；次数和字节不能混算，资源额度不代表人民币余额。
4. 监控账号填写公开 `appid`、授权方式声明及 `relay`，任务保留固定 `operation` 和 `collector` 查询参数，不配置业务 token 文件。第三方授权及权限集齐全后才启用年审。
5. 中转服务联调完成后按需开启账号和任务，执行项目离线配置检查后再部署。中转协议不能自行申请或扩大微信官方权限。

当前有效包指 `status=1` 且 `startTime <= 当前时间 < endTime`。默认不输出历史已失效包，避免全部旧包反复告警；确实要追踪某个历史包时，在该 SPU 的 `track_expired_package_ids` 中填入包 ID。未来生效的包可显示到期与额度，但不会提前计入有效余量。若配置 SPU 无有效包，只输出实际的有效包数量 0，不伪造有效总量/余量 0。

所有页面取完才产生一批可保存的指标；重复包 ID、分页总数变化、空页但尚未达到总数、超过 `max_pages` 等均使整次任务失败，不会返回部分汇总。`detailList[].all/used` 以整数计算，先做差/汇总再转换为 Prometheus 数值，避免 64 位值在相减前丢失精度；原始十进制数字字符串保存在明细 `dimensions.raw_*` 中。分母 0 不输出百分比。已废弃的 `effectiveAll` 和计费阶段语义变化的顶层 `all` 不用于资源包分母。

## 指标与数据时间

指标通过既有 `cloud_monitor_resource_value{provider="wechat",metric="..."}` 暴露，可按账号和资源筛选。资源 ID 包含 CGI、SPU/包 ID、错误 hash 或性能类型；日期只保存在维度内，不参与指标身份，因此跨日可延续趋势与恢复状态。同一 SPU 的单位参与身份，变更单位不会把不兼容额度串成同一条时序。

| 指标 | 单位/含义 |
| --- | --- |
| `api_quota_daily_limit` / `api_quota_used` / `api_quota_remaining` | 次数；官方当天直接值 |
| `api_quota_remaining_percent` | 百分比，剩余/日总额度 |
| `package_total` / `package_used` / `package_remaining` | 配置的 SPU 原始额度单位 |
| `package_status` / `package_expiry_days` / `package_remaining_percent` | 官方状态 1/2/3、到期天数、有效包剩余百分比 |
| `charge_active_package_count` | 当前有效包个数 |
| `charge_active_total` / `charge_active_used` / `charge_active_remaining` | 同 SPU 当前有效包汇总额度 |
| `charge_active_remaining_percent` | 当前有效包剩余/总额度百分比 |
| `subscription_expiry_days` / `subscription_active` | 订阅剩余天数、当前生效 1/未生效 0 |
| `qualification_verified` / `annual_review_required` / `annual_review_expiry_days` | 认证 1/0、是否需年审 1/0、年审截止天数 |
| `js_error_occurrences` / `js_error_affected_users` | 每个错误 hash 的单日 PV/UV |
| `js_error_group_count` / `js_error_total_occurrences` | 单日错误列表条数、PV 合计；都不是全局去重 UV |
| `performance_cost_milliseconds` | 官方每日聚合毫秒数；`cost_time_type` 1 启动、2 下载、3 初次渲染 |

日额度/资源包/基本信息是查询时状态，`observed_at` 为取数时间。JS 错误、性能数据的 `observed_at` 为来源完整日的结束时间，同时在维度记录 `data_date` 和 `collected_at`。性能只用已结束日期，默认最多接受滞后 2 天的数据；没有完整日、数据过旧、字段缺失、权限错误都会走独立采集异常，不补零。JS/性能任务的 `max_age_seconds`、`max_gap_seconds` 已为日数据设置较长窗口，调整采集日偏移时应同步评估这些窗口。

这里未实现官方付费告警事件接收、事件与轮询去重，也未实现近 7 日基线自动异常判断。官方用量/到期推送可作为后续补充，不能认为当前通知网关已经接收微信事件。认证年审不是付费能力到期；付费资源包也不是云开发账单或微信支付账户余额。

## 官方依据与验证边界

- [API 调用额度](https://developers.weixin.qq.com/miniprogram/dev/server/API/openApi-mgnt/api_getapiquota.html)
- [购买资源包用量](https://developers.weixin.qq.com/miniprogram/dev/server/API/charge/api_getusagedetail.html)
- [小程序基本信息与年审](https://developers.weixin.qq.com/doc/oplatform/openApi/miniprogram-management/basic-info-management/api_getaccountbasicinfo.html)
- [JS 错误列表](https://developers.weixin.qq.com/miniprogram/dev/server/API/operation/api_getjserrlist.html)
- [性能数据](https://developers.weixin.qq.com/miniprogram/dev/server/API/operation/api_getperformance.html)

实现按公开官方文档核对，并使用离线响应 fixture 验证分页、单位、时间窗口、令牌/权限、安全错误和异常数据；未使用真实微信凭据调用业务 API，也未发送真实告警。
