# 爱签数据采集与告警

**爱签 OpenAPI 私钥或 appKey 仅由 [独立中转服务](../../cloud-resource-relay/README.md) 保管，中转负责鉴权和只读请求转发。企业认证状态解析、查询状态记录、指标计算、台账及告警全部由监控端处理。** 在线查询使用 [V2 中转协议](collection-relay.md)，真实凭据留待自行配置；本地套餐台账及历史结果兼容模式继续保留。

本模块对应浙江爱签数字科技有限公司（asign.cn），仅支持 OpenAPI 企业认证、OpenAPI 历史结果兼容及本地套餐台账。SaaS 接入已移除。`config/integrations/asign.yaml` 中保留的账号和任务默认禁用；企业认证与套餐台账可分别启用。

## 当前已确认的接入方式

用户已确认当前业务使用 **OpenAPI**。企业认证采用 `asign-openapi` 账号和 `asign-company-verification` 任务：监控填写公开 `app_id`、`relay` 和 `options.collector.selector`，中转保管私钥并用 `scope.company_selectors` 授权允许查询的企业。查询成功状态、耗时、最后成功时间由监控随任务记录；这不是完整签署业务链路探测。

套餐余量和商业到期继续使用 `asign-ledger` 账号及 `asign-package-ledger` 任务，补齐真实台账后可与 OpenAPI 同时运行。台账在监控本地读取，不经过中转，不需要厂商凭据。

保留账号、任务和通知渠道的原有开关；中转服务部署联调和真实台账准备完成后，再按需启用对应账号及任务。Grafana 展示 OpenAPI 企业认证和本地套餐台账，在线查询状态另在平台查询健康分区展示。

## 已实现的接入范围

| 任务类型 | 获取方式 | 指标与告警口径 |
| --- | --- | --- |
| `company_verification` | 经中转按官方 Python SDK 协议 POST OpenAPI `/user/getCompUser`，监控解析原始响应 | 精确匹配目标企业，只有 `userType=1` 且 `status=1` 表示已认证；查询失败单独告警 |
| `company_verification_snapshot` | 可选兼容入口：读取现有业务系统通过官方 OpenAPI SDK 生成的结果文件 | 与原生查询相同的企业身份校验，附来源及时效校验；不作为默认接入方式 |
| `package_ledger` | 读取人工维护或业务系统维护的套餐台账 | 剩余数量、比例、商业到期天数；来源明确标记 `ledger`，不冒充官方套餐余额 API |

公开文档没有核实到套餐余额和商业套餐到期查询接口，相关监控继续使用明确标注来源的本地台账。企业 `identifyTime` 是认证时间，不生成认证到期指标。

## 官方账单获取途径

2026-09-11 进一步核查确认，官方 OpenAPI 有[昨日账单接口](https://web.asign.cn/platform/openDoc/docDetail?mid=billyesterday)，文档更新时间为 2025-01-07。接口地址为 `https://{host}/partner/bill/yesterday`，正式 OpenAPI 域名为 `oapi.asign.cn`。文档描述是“获取接入商 T-1 账单详情，需开启账单推送权限”，请求业务参数为空 JSON `{}`。

成功响应 `code=100000`，`data` 返回 `fileName`、`md5`、`size`、`base64Data`、`fileType`，示例文件为 `用量信息.xlsx`。这确认了官方账单文件获取能力；公开页面没有列出 Excel 内部列结构，因此尚不能确认文件包含套餐总量、剩余份数、商业到期日或完整金额口径。

官方错误码 `101050` 要求联系商务开启账单推送权限，`101051` 要求联系商务开启费用计价，`101052` 表示账单信息下载失败。需要由厂商确认当前接入商/应用具备的权限及对应计价条件，不能仅凭已有 OpenAPI 签署权限认定账单接口可用。

本项目尚未实现该账单采集器。本次只核查公开文档，未调用真实账单接口。下一步可向爱签获取权限说明和脱敏账单样例/列定义，再确定日用量、费用或套餐指标；T-1 用量不能直接当作当前实时套餐余额。完整依据见 [套餐与账单接口核查](asign-package-api-verification.md)。

## OpenAPI 与套餐台账一起接入

OpenAPI 企业认证、套餐余量和商业到期可以同时运行，共用现有采集服务、MySQL、Prometheus、Grafana 与通知网关。企业认证通过中转查询；套餐余量和商业到期来自同一个本地台账任务。在线查询的厂商凭据与企业查询授权在中转配置，查询目标和状态判断在监控配置。

在 `config/integrations/asign.yaml` 填写以下内容：

| 账号/任务 | 必填内容 | 同时启用的开关 |
| --- | --- | --- |
| `asign-openapi` | 公开 `app_id`、`relay.url`、`relay.token_file`，中转令牌仅允许企业认证查询 | 账号 `enabled: true` |
| `asign-company-verification` | `operation: asign-company-verification`；`collector` 下配置 `selector` 和本地别名 `resource_id`，选择条件须属于中转授权范围 | 任务 `enabled: true` |
| `asign-ledger` | `ledger_file` 指向实际台账，台账 `account_id` 与本账号 ID 一致 | 账号 `enabled: true` |
| `asign-package-ledger` | `package_ids` 对应台账套餐 ID；按更新频率设置 `source_max_age_seconds` | 任务 `enabled: true` |

台账模板是 [asign-package-ledger.example.json](../config/integrations/asign-package-ledger.example.json)。填写真实更新时间、剩余数量、总量及商业到期日期后，另存为主机 `config/integrations/asign-package-ledger.json`；路径与现有配置一致。模板用 `null` 和占位时间，不能直接当成有效数据。`total` 未知时应删除该字段，仍能展示剩余数量，但没有剩余比例告警；`expires_at` 未知时应删除该字段，也不会产生到期提醒。要同时启用余量比例和商业到期监控，必须填写这两个真实值。不要把示例文件配置成正式数据源。

现有示例的采集与告警为：

| 监控项 | 采集频率 | 默认告警 |
| --- | --- | --- |
| OpenAPI 企业认证 | 每天 | 成功查询且企业未认证为 P1；查询失败单独产生采集异常 |
| 套餐余量 | 每小时读取台账 | 剩余比例 <=20% 为 P2，<=10% 为 P1 |
| 商业套餐到期 | 与余量同一任务 | 剩余 <=30 天 P2，<=7 天 P1 |
| 台账数据过旧 | 与余量同一任务 | 默认超过 48 小时未更新，产生采集异常，不补零或恢复旧业务告警 |

两个任务分别维护状态。OpenAPI 接口失败不影响套餐台账采集，台账过旧也不影响企业认证查询。恢复遵循主服务 `recovery_samples`，通知仍按现有路由和渠道开关处理；启用采集不会自动开启短信。

回填后，在服务器原项目目录先执行配置检查，成功后重启采集服务：

```sh
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose restart cloud-monitor
docker compose logs --tail=100 cloud-monitor
```

这里假设已部署包含爱签采集器的镜像；首次升级代码按 [四平台部署步骤](external-platforms.md#部署步骤) 构建并重建采集容器。`check --live` 不请求业务接口、不发通知；重启后会按已启用配置实际采集。

重新导入 `grafana/cloud-resource-monitor.json` 并保留原 UID。平台选择 `asign`，账号多选 `asign-openapi` 与 `asign-ledger`，资源选择全部，可以一起查看企业认证状态、套餐剩余数量及单位、剩余比例、商业到期和台账年龄。平台查询健康分区展示在线查询成功、耗时及距上次成功时间；这些指标不代表完整签署业务链路健康。也可按账号、资源单独筛选；台账未提供总量时，剩余比例显示无数据。

## OpenAPI 企业认证

用户提供了从官方资源下载页取得的 `pythonsdk.zip`。已按其中 `HttpUtils.py` 的 `HttpUtils.doPOST` 协议核对 `https://oapi.asign.cn/user/getCompUser`，测试环境为 `https://prev.asign.cn`。签名在中转执行，原始 JSON 的企业匹配与认证状态解析在监控执行。运行不依赖 SDK ZIP。

监控端账号配置：

```yaml
id: asign-openapi
provider: asign
enabled: false
api_mode: openapi
app_id: ''
relay:
  url: http://192.0.2.10:18443/v1/monitor/collect
  token_file: /app/config/relay/asign-openapi.token
  timeout_seconds: 20
```

中转端使用已在爱签 OpenAPI 开放平台配置的应用 ID 和对应 RSA 私钥。已核实的 OpenAPI 签名适配器支持 PEM（PKCS#8 或 RSA PKCS#1）或 SDK `appKey` 使用的 Base64 DER，暂不接受需要交互口令的加密私钥；独立中转的实际密钥配置以其中转部署说明为准。私钥存放在中转端秘密存储；监控 Compose 不再挂载厂商密钥目录。监控仅持有 `relay.token_file` 指定的受限中转 Bearer 令牌，该令牌不用于厂商鉴权。不要使用 SDK 演示私钥，也不要将真实私钥打印、提交到代码库或写入指标。

监控 `asign-company-verification` 任务的 `options.collector.selector` 只能选择 `account`、`creditCode`、`companyName` 之一，建议使用应用内稳定的企业 `account`；中转 `scope.company_selectors` 必须允许该精确选择条件。`collector.resource_id` 是面板和告警的本地别名，不能直接使用法人身份信息。中转联调后按需启用，默认每天查询一次。

响应必须满足 `code=100000`、目标企业唯一匹配、`userType=1`、`status` 为整数 0 或 1。`status=0` 产生企业未认证告警，`status=1` 为已认证；没有记录、个人用户、重复企业记录、错误码和字段缺失均是采集失败，不冒充未认证状态。指标 `source=official_api`、`observed_at` 为本次查询时间。只保存认证状态和本地别名，不保存响应中的姓名、身份证号、银行卡号。`identifyTime` 不用于推算商业有效期。

### 已核实的 SDK 协议

- 业务 JSON：`json.dumps(selector, sort_keys=True, separators=(',', ':'), ensure_ascii=False)`。
- 时间戳：与该 SDK 一致，`str(round(current_unix_seconds * 1000) + 60000)`。
- 待签内容：业务 JSON + 该 JSON 的 UTF-8 MD5 小写十六进制 + APPID + 时间戳。
- 签名：RSA PKCS#1 v1.5 / SHA1，Base64 后作为 `sign` 请求头。SDK 注释写 SHA256，但执行代码实际使用 `SHA1.new`；实现以执行代码为准。这是兼容官方协议，不代表将其用作其他系统的新签名方案。
- POST multipart 字段：`appId`、`bizData`、`timestamp`、`Content-Type=application/octet-stream`、`type=application/octet-stream`。请求头使用 multipart encoder 生成的真实 boundary。

中转使用 `cryptography` 与 `requests-toolbelt` 生成签名和 multipart，以校验证书和主机名的 HTTPS 只读请求访问厂商，并设置超时、禁止重定向。监控到中转可以使用已授权的字面 IP、显式端口和固定路径 HTTP，也可使用校验证书和主机名的 HTTPS；两种方式都保留 Bearer 令牌和身份绑定校验。监控不执行 SDK 中创建用户、认证扣费、创建合同或签署合同操作。该 SDK 未展示响应签名字段，因此这里不声称存在响应验签。

提供的 ZIP SHA256：`f38dad1103f02a7da8e40d144287cede123503fedbdea73e4de925f6854cb0db`。已使用临时生成的 RSA 测试密钥、固定时间，并模拟 SDK 的 `requests.post`，对比实际 `HttpUtils.doPOST` 与本实现：账号、中文企业名称、信用代码 3 组的签名字节、multipart 字段、URL 和时间戳完全一致。未使用示例密钥或真实凭据，未请求爱签业务 API；实际账号权限及响应仍需部署后联调确认。

## 可选：官方 SDK 历史结果兼容

只有需要继续读取已有离线结果时才使用 `kind: company_verification_snapshot` 和独立账号 `api_mode: openapi_snapshot`，账号配置用于来源校验的非秘密 `app_id`、`endpoint` 和 `sdk_result_file`。该模式不会调用 API，不需要私钥或 token；不能把它和在线中转任务共用同一个账号模式。默认接入片段只配置在线中转和套餐台账任务。

业务系统将以下结构原子写入主机 `config/integrations/asign-openapi-result.json`。`response` 为官方查询结果，建议写入前只保留查询匹配所需的 `account`、`creditCode`、`companyName`、`userType`、`status` 字段，去除姓名、身份证号、银行卡号。

```json
{
  "version": 1,
  "app_id": "YOUR_OPENAPI_APP_ID",
  "endpoint": "https://oapi.asign.cn/user/getCompUser",
  "collected_at": "2026-09-11T12:00:00+08:00",
  "response": {
    "code": 100000,
    "data": [{"account": "YOUR_COMPANY_ACCOUNT", "userType": 1, "status": 1}]
  }
}
```

这只是文件格式示例，时间和状态不能照搬作为生产记录。`collected_at` 必须是该次成功官方查询的实际时间且带时区，不能用文件更新时间代替。校验同时检查应用 ID、正式/测试地址、记录唯一性和企业类型，防止拿另一个应用或个人用户数据代替企业认证状态。

快照任务的目标选择与原生任务相同，另可配置 `source_max_age_seconds`，默认 172800 秒；过期、未来时间、无记录、业务错误均产生采集异常。指标 `observed_at` 保留 SDK 实际采集时间，维度 `source=official_sdk_snapshot`，不会声称是当前时刻重新查询的结果。需要该兼容入口告警时，规则 `match.source` 应设为 `official_sdk_snapshot`，原生示例规则保持 `official_api`。

## 套餐台账

配置独立 `api_mode: ledger` 账号，指定 `ledger_file`，不需要 OpenAPI 私钥或中转令牌。主机文件 `config/integrations/asign-package-ledger.json` 的格式如下：

```json
{
  "version": 1,
  "account_id": "asign-ledger",
  "updated_at": "2026-09-16 14:00:00",
  "packages": [
    {
      "id": "signatures-main",
      "unit": "signatures",
      "remaining": "150",
      "total": "1000",
      "expires_at": "2026-10-01 00:00:00"
    }
  ]
}
```

示例值不是实际套餐数据。`account_id` 必填且必须等于配置中的台账账号 ID，例如 `asign-ledger`；用于防止误挂另一个账号或合同的同名套餐文件。必须与业务合同或官方后台核对数量、计费单位、扣减规则及有效期。`unit` 使用明确的 ASCII 单位名，例如 `signatures`，不能默认所有产品都按签署份数计费。来源、产品类型和单位参与指标身份，修改单位不会串联原单位的告警样本。`remaining` 必填，`total` 和 `expires_at` 可选；缺少总量不计算剩余比例，总量为 0 也不除零。可按 `package_remaining` 为特定产品添加数量阈值规则。

`updated_at` 填最近实际核对台账的时间，`expires_at` 填商业套餐的实际到期时间。两者都可以直接使用 `YYYY-MM-DD HH:MM:SS`，例如 `2026-09-16 14:00:00`；日期和时间之间也可使用 `T`。无时区值固定按北京时间（UTC+8）解释并转换为内部 UTC，不读取宿主机或 Docker 的本地时区。带显式偏移的 ISO 时间（如 `2026-09-16T14:00:00+08:00`）、以 `Z` 结尾的 UTC 时间和 Unix 秒时间戳仍然兼容。这项无时区兼容仅适用于爱签套餐台账的这两个字段；OpenAPI 结果文件的 `collected_at` 等其他来源仍遵循原有时区规则。

`options.package_ids` 明确选择仍需保障的套餐，防止所有历史过期套餐持续报警。过期台账整个任务失败，不继续当作实时余额。当前计算的商业剩余天数使用查询时刻，原始更新时间转换为 UTC 后保留为 `source_updated_at`，台账年龄另有 `ledger_age_seconds` 指标。官方后台续费后应更新台账，恢复告警仍需满足主服务的恢复样本要求。

## 指标及验证

所有指标进入现有 MySQL 明细/最新快照、告警引擎和 Prometheus exporter，不另开采集服务。Prometheus 保留时长仍由既有 Prometheus 的 200h 配置决定。

| 指标 | 单位 | 来源 |
| --- | --- | --- |
| `company_verified` | bool | official_api；可选结果文件模式为 official_sdk_snapshot |
| `package_remaining` | 配置的套餐单位 | ledger |
| `package_remaining_percent` | percent | ledger |
| `commercial_expiry_days` | days | ledger |
| `ledger_age_seconds` | seconds | ledger |

离线测试：`.venv/Scripts/python.exe -m pytest tests/test_asign.py`。测试使用模拟官方响应、临时生成的 RSA 密钥及临时文件，覆盖 OpenAPI RSA-SHA1/multipart/中文编码、私钥格式和轮换、企业身份/来源绑定、台账时效及敏感字段隔离；中转授权、身份绑定和原始响应解析另由中转适配器测试覆盖。不调用真实业务 API，不发送通知。

官方依据（2026-09-11 核对）：[OpenAPI SDK 指南](https://web.asign.cn/platform/openDoc/initialize)、[企业信息](https://web.asign.cn/platform/openDoc/docDetail?mid=getCompUser)、[SDK 下载](https://web.asign.cn/platform/tools/resourceDownload)。
