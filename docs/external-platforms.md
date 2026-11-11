# 七牛云、汇付、爱签与微信小程序接入

四平台指标口径最初按 2026-09-11《官方 API 监控指标核查与调整建议》接入；2026-09-15 已在同级 `../cloud-resource-relay` 提供 [独立中转服务](../../cloud-resource-relay/README.md)。中转仅保管厂商凭据、检查只读请求授权、签名/注入鉴权及转发原始响应。指标解析、分页、金额与额度计算、存储和告警全部留在本监控项目，接口见 [V2 中转协议](collection-relay.md)。两个项目独立构建部署，不互相导入源码。保留现有账号、任务和通知开关，实际启用状态以当前配置为准；厂商凭据由独立中转管理。

采集继续使用原有 MySQL 明细/最新样本、告警升级/恢复/去重、通知网关、Prometheus `/metrics` 与 Grafana。MySQL 保留 30 天，Prometheus 200h 的既有设置不变。中转服务独立于通知网关，不能用通知网关 token 代替中转专用令牌。

## 当前交付范围

| 平台 | 监控端保留的指标与计算口径 | 配置和接口说明 |
| --- | --- | --- |
| 七牛云 | 现金及确认后的可用余额、费用快照及月预算、资源包当月余量/到期、Kodo 标准容量和文件数、CDN 流量/带宽/5xx | `config/integrations/qiniu.yaml`、[七牛说明](qiniu-api.md) |
| 汇付斗拱 | 精确子账户可用/冻结/总余额、账户状态、清单内出金状态、监控本地公开证书到期；在线查询由中转签名与验签 | `config/integrations/huifu.yaml`、[汇付说明](huifu-api.md) |
| 爱签 | 经中转的 OpenAPI 企业认证、OpenAPI 历史结果兼容；明确来源的本地套餐余量/商业到期台账 | `config/integrations/asign.yaml`、[爱签说明](asign-api.md) |
| 微信小程序 | 关键 API 日额度、付费资源包及订阅、第三方授权条件下的认证年审、昨日 JS 错误、每日聚合性能 | `config/integrations/wechat.yaml`、[微信说明](wechat-api.md) |

用户已确认爱签业务使用 **OpenAPI**。原有 `getCompUser` 协议已与提供的官方 Python SDK 做过离线核对。私钥仅在中转加载，监控配置公开 APPID 和企业选择条件，中转按授权检查后签名转发；原始响应由监控解析。保留独立的官方 SDK 结果文件接入方式。当前采用 [OpenAPI 企业认证与套餐台账](asign-api.md#当前已确认的接入方式)，SaaS 接入已移除。爱签现金余额、签署套餐量及商业有效期没有已确认的公开查询接口，台账指标均标明 `source=ledger`。

补充核查发现爱签 OpenAPI 的[昨日账单接口](https://web.asign.cn/platform/openDoc/docDetail?mid=billyesterday)，可返回 T-1 账单文件，需开通账单推送权限。公开页面未给出 Excel 列定义，尚不能认定可查询套餐余额/到期；此接口尚未并入采集器，详见 [官方账单获取途径](asign-api.md#官方账单获取途径)。

本次没有接入七牛月结账单对账、日累计费用差分、CDN 在用证书发现，微信官方消息事件回调或近七日自动基线。这些可选项没有用推测字段或不完整查询充当实现。微信轮询使用现有告警去重机制；JS UV 只按错误分组展示，不跨组求和。

## 配置和凭据

主配置 `monitoring_files` 已增加四个 `integrations/*.yaml`。加载器只允许片段追加 `accounts/jobs/rules`，不允许覆盖服务、数据库或通知设置，重复 ID 仍报错。保留现有账号、任务和通知开关，四平台及阿里云、华为云、RDS 的实际启用状态以当前配置为准。

Compose 使用两个相关的只读目录挂载：

- `./config/integrations:/app/config/integrations:ro`：平台配置、台账/结果、订单监控清单及公开 X.509 证书文件，不放私钥。
- `./config/relay:/app/config/relay:ro`：每账号独立的中转专用令牌。只允许服务器端配置的监控操作，不放厂商密钥、业务 token、订单清单或证书。目录内文件默认被 Git 忽略，配置目录整体不进入 Docker 镜像。

容器以 UID 10001 运行，中转令牌和本地台账文件要允许该用户读取。轮换中转令牌或更新结果/台账时建议原子替换文件，目录挂载会读取最新版本。不要在终端输出真实凭据。

四平台在线账号保留必要公开应用/商户身份及 `relay`；厂商秘密或业务 token 文件只在中转配置。任务 `options` 包含固定 `operation`、可选 `source_max_age_seconds` 和 `collector`，原采集目标、预算、单位、分页和时间窗口都放在 `collector`。中转只用 scope 限制资源范围，内置目录验证具体只读 URL、方法和参数，不执行监控计算。中转失败或协议异常均记录采集失败，不回退厂商直连。爱签本地文件模式及汇付公开证书检查继续本地执行。

## 数据与告警口径

每个采集值保存资源身份、单位、来源时间及安全的明细字段。监控端从中转返回的厂商原始 JSON 精确处理金额和大整数，再生成有限数值指标；仅原适配器允许的维度进入 MySQL 明细，原始响应不整份写入日志或告警。Prometheus 使用双精度数值。币种、SPU、子账户、不兼容套餐单位不能合并。到期指标分别区分资源包、商业台账、微信认证年审与监控本地 X.509 证书。

查询失败、目标缺失、无权限、结构异常、过旧数据生成独立采集异常，不补业务零值。API 类任务额外保存 `api_query_success`、`api_query_duration_seconds`、`api_last_success_timestamp_seconds`；汇付增加 `api_signature_failed`。这些是本地探测值，`source=local_probe`，不代表供应商业务交易成功或短信已送达。查询耗时覆盖一个任务内的全部查询与解析。汇付出金清单为空时仅更新跟踪数量，不产生 API 查询成功或验签恢复样本。

`api_last_success_timestamp_seconds` 失败时不更新；即使超过普通新鲜度窗口也继续导出，以便显示距上次成功的时间，直到最新样本表按明细保留规则清理。它是时间标记，不是有效业务值；其 `resource_fresh` 仍照常体现年龄。其余过期业务值继续消失。

规则新增可选 `for_samples`，默认为 1。指定后要求连续 N 个不同观测时间的有效样本命中，且同时满足 `for_seconds`；重复源时间不计数，正常样本、采样间隔过长或新平台采集失败会打断连续计数。中途失败不会把已有业务告警视为恢复。状态和失败断点持久化，重启不能让相隔失败的低余额样本凑成连续两次。

`collection_error_samples` 控制一个任务连续多少次采集失败触发原有 P2 采集异常，默认 1；汇付/部分微信任务设为 3。正常恢复仍遵循原配置 `recovery_samples`，告警按 P0/P1/P2 走现有短信/钉钉/邮件路由。新增任务不会自动开启已经关闭的通知渠道。模板中的金额、预算、错误次数和耗时阈值是业务待确认值。

完整日统计和费用快照按来源时间/账期处理，不作为分钟实时指标。七牛快照每月首日属于上月，缺少当前月合法数据时不伪造预算 0；得到新月正常数据后可恢复旧月预算告警。删除配置目标、移除订单或变更单位不会自动产生该旧资源的恢复通知，变更前应核对现有活动告警。

## Grafana 和验证

重新导入 `grafana/cloud-resource-monitor.json`，保留 UID 可更新原面板。新增平台查询健康、七牛费用/资源、汇付子账户/出金、爱签 OpenAPI 认证/套餐台账、微信额度/年审/质量分区；仍按现有数据源、采集服务、平台、账号和资源筛选。各平台专用到期值也参与总览“最近到期”。当前静态 HTML 是原基础面板的演示布局，实际新增分区以 JSON 为准。

开发验证命令均不调用实际业务接口、不发通知：

```sh
python -m pytest -q
python -m cloud_monitor --config config/config.yaml check
python scripts/build_grafana_dashboard.py --check
python scripts/check_observability.py
```

最后一项需要 Docker 和已缓存的 `prom/prometheus:v3.13.0` 镜像，校验容器断网运行，不自动拉取镜像。也可指定已有版本，例如 `python scripts/check_observability.py --image prom/prometheus:v3.10.0`。

`check --live` 也不调用 API，只额外校验已启用任务、中转参数及适用条件。实际 `diagnose --job ...` 会请求中转服务；服务端可据此调用只读平台 API，必须完成中转部署和授权后再执行。它不会创建数据库或发送告警；中转端必须限制操作权限，禁止资金交易等非监控操作。开发验证使用离线模拟响应，不启动生产调度。

2026-09-11 最终验证：1021 项测试通过，包括新建临时 MySQL 8.4 容器上的数据库集成测试；Ruff 通过；69 条面板/变量查询通过 Prometheus 3.10.0 的 promtool 校验。Python 3.12 镜像构建成功，容器断网、只读挂载配置并使用虚拟数据库/网关环境值执行 `check --live` 通过。爱签另与用户提供的真实 SDK 做了三组离线签名对照。保留两条既有阿里云 SDK TLS 弃用提示；未调用实际业务接口、发送通知或部署到生产服务器。

2026-09-14 中转隔离验证：`python -m pytest --ignore=tests/test_mysql.py` 离线通过 1156 项，`ruff check .` 通过，实际配置离线检查通过（9 个账号、61 个任务、55 条规则）。保留两条既有阿里云 TLS 弃用提示。本次没有运行 MySQL 集成测试、调用真实中转/厂商接口、发送通知或启动生产调度。

## 部署步骤

将本项目改动同步到服务器原目录。保留服务器现有 `.env`、主配置凭据与通知配置，以及实际端口、网络和反向代理设置。主配置继续通过四个 `monitoring_files` 路径加载集成片段。Compose 移除七牛 AK/SK 环境传入及整个 `config/credentials` 挂载，改为只读挂载 `config/relay`；台账、订单监控清单与公开证书从 `config/integrations` 读取。无需为此修改现有 Prometheus 抓取地址。同步 V2 集成配置及 `options.collector`，并保持当前账号、任务和通知开关；实际启用状态以当前配置为准：

```sh
cd /data/elk/cloud-resource-monitor
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose up -d --no-deps --force-recreate cloud-monitor
docker compose logs --tail=100 cloud-monitor
```

最后两条属于实际部署，运行后会恢复原生产调度及通知。此处只提供操作步骤，开发测试不执行它们。无需删除 MySQL 卷或重建通知网关。先按 [中转 README](../../cloud-resource-relay/README.md) 填写真实配置并部署独立中转，再按 [凭据迁移步骤](collection-relay.md#部署与凭据迁移) 配置监控账号、令牌和固定操作，完成真实只读联调后按需启用。配置变更不会回填旧 Prometheus 历史。

无真实凭据时，可在中转项目运行 `python scripts/integration_check.py --monitor-project ../cloud-resource-monitor`，验证本地 IP 和端口上的真实 HTTP 客户端/服务端和四平台适配器的模拟上游响应；追加 `--scheme https` 可验证原 TLS 链路。该测试使用临时配置，不更改本项目的账号、任务、通知或厂商凭据。它验证对接实现，不代表生产厂商账号已完成联调。
