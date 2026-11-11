# 云资源到期、额度与余额监控

> 2026-11-11 脱敏交付：本地配置中的账号凭据、平台应用身份字段、通知令牌及邮箱收发地址已置空。运行前须重新填写 `.env`、`config/config.yaml`、`config/integrations/` 和 `config/notifications/notifications.yaml` 中所需字段，并配置中转令牌文件；账号、任务和渠道的启用开关保持原状。详见[脱敏交付说明](docs/sanitized-delivery.md)。

Python 3.11+ 独立服务，支持阿里云、华为云，以及七牛云、汇付斗拱、爱签和微信小程序的中转采集入口，使用 MySQL 保存采集数据、告警状态、通知队列和短信回执。阿里云、华为云使用受限采集身份；其余四平台的厂商 AK/SK、私钥、AppSecret 和业务令牌由可信中转服务保管，监控只配置中转地址及受限令牌。交付包含带 MySQL 8.4 的 `docker-compose.yml`。原 Excel 保持不变。

已提供 `/metrics`，把 MySQL 中的最新有效采集值、任务状态和告警状态暴露给现有 Prometheus；Grafana 使用可编辑的自定义面板。MySQL 明细默认保留 **30 天**，Prometheus 时序数据按要求保留 **200 小时**，抓取频率继承你现有的 Prometheus 配置。接入步骤见 [Prometheus 与 Grafana](docs/prometheus-grafana.md)，面板文件为 [cloud-resource-monitor.json](grafana/cloud-resource-monitor.json)，可先打开 [面板离线预览](preview/grafana.html) 查看布局。

2026-09-15 已在同级目录提供 [独立中转服务](../cloud-resource-relay/README.md)。**中转只负责厂商鉴权、只读接口授权和原始请求转发；四平台指标解析、分页、金额/额度计算、存储及告警全部保留在本监控项目。** 两端通过 [V2 中转协议](docs/collection-relay.md) 通信，独立构建部署，没有源码运行依赖。保留现有账号、任务和通知开关，实际启用状态以当前配置为准；厂商凭据由独立中转管理。未配置中转时不会回退直连。接入范围见 [四平台接入说明](docs/external-platforms.md)。爱签 OpenAPI 企业认证经中转获取原始响应，套餐台账及汇付公开证书仍在监控本地读取，见[当前接入方式](docs/asign-api.md#当前已确认的接入方式)。高德地图仍不纳入。ECS 支持有效期与独立网络速率采集；MySQL、PostgreSQL 的 [RDS 性能采集](docs/rds-performance.md) 不需要业务数据库账号。

爱签仅保留 OpenAPI 企业认证、OpenAPI 历史结果兼容和本地套餐台账，SaaS 接入及其专用配置、规则和面板已移除。已提供待填写的台账 JSON 模板，Grafana 分别展示企业认证与套餐台账指标。台账的 `updated_at` 和 `expires_at` 可按 `YYYY-MM-DD HH:MM:SS` 填写，无时区时间固定按北京时间（UTC+8）处理，不受宿主机或 Docker 时区影响；显式时区及 Unix 秒仍兼容，详见[套餐台账](docs/asign-api.md#套餐台账)。

已进一步核实爱签提供[官方昨日账单接口](docs/asign-api.md#官方账单获取途径)，需开通权限并取得账单列定义后再接入；当前尚未实现，不能把 T-1 账单等同于实时套餐余额或商业有效期查询。

数据库性能配置在 `config/rds-performance.yaml`，通过主配置的 `monitoring_files` 加载。当前已启用 `aliyun-hycen` 两台、`aliyun-yuanzhu` 五台 MySQL 的性能、行锁等待和慢查询任务；PostgreSQL 两项仍为关闭且待填实例的模板。有效期任务由主配置独立管理，RDS 网络任务在 `config/network-traffic.yaml` 自动发现各账号下受支持的实例。**只填写账号 AccessKey 不会自动创建 CPU、内存、连接等性能任务**；新增实例时需在性能配置中填写该账号的实例 ID、引擎和地域，并同步补充慢查询任务清单。现有 yuanzhu 五台实例及排查方法见 [RDS 配置和开关](docs/rds-performance.md#配置和开关)。

MySQL 行锁等待采用累计次数差分，默认 >=1 次/秒持续 5 分钟触发 P1，与其它原生性能指标合并请求，不增加调用量。原 MySQL 死锁 >3 次/小时规则保留但未绑定采集任务。PostgreSQL 死锁采用速率 >0 持续 3 分钟、长 SQL 采用执行超过 5 秒的 SQL 数 >=1 持续 5 分钟，均触发 P1。完整指标、单位和阈值见 [RDS 性能监控](docs/rds-performance.md)。

2026-10-12 已确认民用数据库性能采集连续失败的原因：云 API 的 `innodb_row_lock_waits` 返回过 `826.9`，旧版强制要求整数，导致每轮回看 15 分钟时反复拒绝同一个历史点。当前版本接受非负、有限的小数累计值，以十进制精度先差分再转换为速率，不取整或补零；负数、非有限值、非法格式仍报错，计数下降和漏点仍中断差分。现场证据、最小服务器更新和只读复查步骤见 [RDS 行锁采集报错排查](docs/troubleshooting-collection.md#2026-10-12-rds-行锁采集失败)。

2026-10-13 凌晨 `aliyun-mysql-cloudmonitor` 单轮失败已通过服务器日志确认：阿里云 CMS 请求返回 HTTP `503` / `ServiceUnavailable`，下一轮成功，连续两次成功后发出恢复通知。当前版本为 RDS 云监控错误详情增加白名单内的 API 名称、异常类型、云错误码和真实 HTTP 状态，便于区分服务不可用、权限、限流及传输异常，不增加调用或改变告警次数。时间线、请求编号及更新步骤见 [RDS 云监控单轮 503 排查](docs/troubleshooting-collection.md#2026-10-13-rds-云监控单轮-503)。

2026-10-19 已修正短信日发送量统计不足被误报为采集失败的问题。`QuerySendStatistics` 所有分页成功，但缺少计算所需日期，或前七日均值为零而昨日有发送量时，任务显示 `insufficient_data`（统计不足），不补零、不生成增长率、不触发权限/接口失败通知。完全空的成功响应仍为 `no_data`；接口和数据格式错误仍为采集失败。历史采集失败告警可按默认连续两次正常查询恢复，历史短信业务告警继续保留，旧增长率当前值立即隐藏。项目 Grafana 面板同步显示橙色“统计不足”。本次改动维护在本地项目中，服务器应用步骤见 [2026-10-19 短信排查](docs/troubleshooting-collection.md#2026-10-19-短信统计不足)。

短信增长率仅在最近持久化任务状态为 `ok` 且样本仍新鲜时作为当前值导出；统计不足、无数据或真实接口错误都会隐藏旧值，直到新的完整有效统计返回。统计不足后再遇到接口失败也不会重新显示此前增长率。华为内网估算在任务为 `ok` 或 `degraded` 时，仅导出仍新鲜且可用的资源；有限负差资源的双向旧值立即隐藏，真实采集错误或任务陈旧时隐藏该估算任务的全部当前值。其余任务保留“接口错误时可展示尚未过期旧样本”的现有策略。

2026-10-20 新增 [ECS 与 RDS 网络速率监控](docs/network-traffic.md)，配置在 `config/network-traffic.yaml`。阿里云 ECS 使用原生内网指标，阿里云 RDS MySQL 仍明确展示总网络吞吐；华为云 ECS、受支持的 MySQL/PostgreSQL RDS 任务在 2026-10-27 当前版本直接替换为内网估算。ECS 初始 P2/P1/P0 线为 100/300/800 Mbps，RDS 为 50/100/200 Mbps，原数值阈值与持续窗口不变，均要求持续超限。华为两个任务统一每 300 秒查询，不新增业务数据库账号，账号和通知开关保持现有配置。ECS 自动发现另需 `ces:metrics:list` 只读权限。

2026-10-23 起，ECS 网络速率支持**按实例清单选择采集、展示和告警**：在 `config/network-traffic.yaml` 对应账号的 ECS 任务中填写 `options.instances`，每台填写 `resource_id`、`region` 和可选 `resource_name`，入站与出站共用清单。2026-10-26 当前项目配置已移除 `huawei-ecs-network` 的空清单，恢复华为云在账号/任务指定地域自动发现；两个阿里云 ECS 任务保留各自清单，本地未填写的 `instances: []` 仍表示暂不采集任何 ECS、不调用网络云 API。修改配置后重新创建采集容器生效。移出清单的实例停止导出当前网络数据，旧网络告警及未发送重试会在启动时清理；历史曲线和已发送记录保留。RDS 继续原有自动发现。配置示例及账号对应关系见 [只采集指定 ECS](docs/network-traffic.md#只采集指定-ecs)，华为停采恢复见 [恢复华为 ECS 自动发现](docs/server-update.md#2026-10-26-恢复华为-ecs-自动发现)。

同日已根据生产只读诊断修复华为云 RDS 单位兼容：CES 的 `rds004_bytes_in` 实际返回 `byte/s`，旧实现只接受 KiB/s 一类单位而报 `CES network rate has an unexpected unit`。现按每个数据点的实际单位换算为 bit/s，字节每秒乘 8、KiB/s 一类乘 8192；未知或缺失单位仍拒绝。该报错不需要补 IAM 权限，已有网络监控部署只更新采集服务，配置、告警阈值和现有看板不变；见 [最小修复步骤](docs/server-update.md#华为云-rds-单位兼容修复)。

2026-10-27 服务器只读核查确认：用户在 `cn-east-3` 项目补充 `vpc:publicIps:list` 后，38 个 EIP 中 36 个按公网地址唯一关联到全部 33 台 ECS、3 台 RDS。抽查 6 台实例，总量与公网双向序列共有 154 个同时间戳匹配点，差值均非负。当前项目据此实现 `scope=intranet_estimated`，直接替换原华为两个任务、四条业务规则与运行明细四图，未保留一套并行总量告警。采集时统一使用已闭合的 300 秒平均窗口，`内网入站估算 = 总入站 - 全部关联 EIP 公网入站`，出站同方向相减；每个方向及全部源时间戳必须严格一致。缺失、关联冲突、陈旧或负差时隐藏，不补零或把负数截成零。NAT/负载均衡等未经过已关联 EIP 的公网路径仍是估算边界，最小权限及现场证据见 [核查记录](docs/huawei-network-scope-audit.md) 和 [补充权限](docs/huawei-permissions.md#eip-关联的补充查询权限)。该版本已于当日 18:19:52 至 18:20 左右部署到原服务器，采集与网关健康，正式日志两轮均成功采集 ECS 66、RDS 6 个双向样本；部署、最终核验进度及回滚见 [华为内网估算替换](docs/server-update.md#2026-10-27-华为内网估算直接替换)。

2026-10-30 排查确认 `prometheus` 的 14:10 出站估算为负，原因是同一个五分钟聚合标签下，ECS 单个原始样本与 EIP 五个分钟样本均值缺少统计窗口可比性的保证，并非实际内网流量为负。已在 `192.168.120.28` 部署质量处理，保留现有数据源和 `300/average`：只有有限负差单列为 `degraded`（估算不可用），不再因此触发 `collection-error`，也不补零。受影响资源的无效源时间持久化，必须由同一资源更晚的有效双向配对样本确认恢复；不可用期间保留已有业务告警，并中断超限候选和恢复连续性。API、缺字段、非有限值、时间不齐、陈旧和映射错误仍为真实采集错误。Prometheus 指标 `cloud_monitor_network_estimate_available` 展示已有有效配对点或负差记录的资源；`/readyz` 可接受 `degraded`，仍拒绝真实错误。通用 Grafana 可用性表文件已同步，线上现用看板尚未应用该表；部署核验和回滚见 [上线记录](docs/server-update.md#2026-10-30-华为内网估算质量处理)，原因与处理边界见 [负差专项核查](docs/huawei-network-negative-difference-research.md) 和 [估算可用性](docs/network-traffic.md#估算可用性)。

2026-11-03 排查确认近期失败/恢复邮件主要来自华为 ECS 网络数据间歇性陈旧，以及阿里云接口短时异常。11 月 2 日共记录 96 封邮件，其中采集失败/恢复 90 封，华为 ECS 占 60 封。保留日志中的华为逐资源失败均指向共同源数据超过 15 分钟；当次独立复查 33 台均正常，未发现监控发送队列失败重试。按用户确认，已于当天 09:59 在原服务器将华为 ECS 网络、八项阿里云数据库采集和阿里云负载均衡带宽共十项任务改为连续两次失败才提醒，仍连续两次健康后恢复。新鲜度校验、任务/渠道开关及凭据不变，仅重启原采集服务。历史同窗回放邮件由 99 封降至 20 封，约减少 80%，不代表云端数据延迟已修复；见 [排查证据](docs/troubleshooting-collection.md#2026-11-03-失败与恢复邮件反复出现) 与 [部署和回滚](docs/server-update.md#2026-11-03-采集失败连续两次确认)。

应用 2026-10-27 内网范围替换配置时，旧总流量业务活动告警、持续候选和待发送/失败重试消息会在启动时清理，包括未发送恢复消息，不生成迁移恢复通知。健康告警、已发送记录与历史采样保留；新的内网业务告警从有效估算源样本重新累计。后续估算负差不属于这类迁移，不清除已有业务活动告警。阿里云 RDS 当前账号指标元数据未发现内外网分项，不能仅改标题当作内网。后续对两个账号 7 台 RDS 的只读地址/EIP 探测全部成功，无缺失权限项：5 个公网 IP 在各自账号/地域可见的 EIP 清单均未匹配，2 台未返回公网地址，仍未取得可独立测量并相减的公网流量；当前无需追加查询权限，继续展示总量。完整证据及接口权限边界见 [阿里云 RDS 核对](docs/aliyun-rds-network-scope-audit.md)。

2026-10-27 补充研究确认可从**客户端 ECS 的 VPC 流日志**，按 RDS 私网 IP 和实际端口统计被覆盖客户端的双向内网网络流量；全部访问方覆盖、日志质量及口径验证通过前，不能将其作为整台实例的完整内网量。DAS 及 RDS MySQL 数据库代理未找到可直接使用的内外网字节分项；原生公网端点尚无新增可用吞吐接口证据。自有 EIP / 专用 TCP 入口可以测经过入口的公网访问，但需要连接路径改造和口径校准。流日志 / SLS 有开通条件与费用；本轮未开通服务或改动运行监控。方案比较、官方来源和验证顺序见 [还有哪些获取办法](docs/aliyun-rds-network-scope-audit.md#补充研究还有哪些获取办法)。

新增网络任务后，API 量取决于资源数与关联 EIP 数。华为内网估算每轮 `2N + 2E` 次 CES 查询，每实例恰好一个 EIP、五分钟调度时约 35,712 次/月，另计目标发现、关联资源/EIP 列表和名称分页。运行 `python scripts/estimate_api_usage.py config/config.yaml` 离线估算；资源数未知或 EIP 数未验证时输出最低量、公式和不完整状态。旧 89,280 与 133,920 次/月属于新增网络采集前的场景，不能作为当前总量，历史核算见 [API 用量审计](docs/current-api-usage-audit.md)。

本次 Grafana 替换目标是 **云业务与开发 · 运行明细**（`cloud-business-runtime-v1`）的华为 ECS/RDS 入站、出站四图，面板 ID 为 8/9/12/13。保留已有 MB/s 单位、三档参考线和各图独立时间控件，标题改为“内网…（估算）”，查询只选择 `scope=intranet_estimated`。用 `scripts/update_huawei_runtime_intranet.py` 更新同一看板最新导出，当前 JSON 为 [运行明细](grafana/cloud-business-runtime-v1.json)。原问题看板 `cloud-business-dev-v1` 的历史追加/拆分脚本仍只用于该旧布局，不用于此次运行明细替换；维护说明见 [现有看板网络分区](docs/grafana-business-network.md)。采集器、规则及通知展示同步应用步骤见 [服务器更新](docs/server-update.md)。

同日修正该看板「告警明细」把网络指标显示成“其他监测内容”的问题：保留原来的级别、账号、资源类型、资源名称、告警内容五列，在告警内容中展示具体指标和最近有效值，网络速率使用 Mbps，缺失或过期值不补零。本次仅修正看板，无需更新采集服务、生产配置或通知网关；说明见 [告警明细](docs/grafana-alert-details.md)。

网络告警内容使用简短格式，例如“RDS 内网出站（估算）：272.21 Mbps；达到 200 Mbps，持续 5 分钟”。阈值和持续时间按网络配置中的规则 ID、当前级别生成，阿里云与华为云 ECS 的持续时间分别展示；阿里云 RDS 继续标明总量口径。生产调整规则后，需用同一份实际配置重新生成并保存告警表映射。

华为云 ECS 图例沿用“实例名称（ID）”：CES 自动发现后，通过 ECS `ListServersDetails` 按地域批量补齐名称，默认缓存 300 秒，历史 UUID 标签不回写。需同步依赖 `huaweicloudsdkecs==3.1.213` 及只读权限 `ecs:cloudServers:list`。内网估算还通过完整 ECS/EIP 清单校验绑定，显式实例清单也需要详情列表权限；名称缺失可回退为 ID，关联查询无权限或关系不明确则不输出估算。原名称修复背景见 [ECS 实例名称修复](docs/server-update.md#华为云-ecs-实例名称修复)。

七牛余额不足统一按可用余额提醒：仅保留 `qiniu-available-low`，CNY 可用余额 ≤500 元触发 P1、≤100 元触发 P0；现金余额继续采集展示。升级后，采集服务启动会清理已删除或已解绑业务规则的旧告警状态，取消其待发送与失败重试消息，不产生恢复通知；采集失败告警保留。需更新配置及采集代码并替换 `cloud-monitor` 容器，详见 [七牛余额告警去重](docs/server-update.md#七牛余额告警去重)。本地修改不代表已部署到生产。

容器、Kafka 和 Nacos 四张现有资源明细看板的数据展示修正，维护在 [资源明细看板排查与更新](docs/grafana-detail-data.md)：容器版本按实例展示文本标签，Kafka 进度变化补齐子查询采样间隔，Nacos 区分无样本、无效耗时与未暴露的异常指标。对应 JSON 保留原 UID、筛选器和独立时间控件，更新时应用到原看板并保存；本次仅调整 Grafana，无需重建镜像、重启采集服务或增加云权限。

## 项目目录与维护方式

**本目录 `cloud-resource-monitor` 是完整项目，也是后续修改的唯一入口。** 原始实现和历次更新已合并在这里，不需要再按顺序覆盖任何旧更新包。后续直接修改本目录，不再生成新的更新压缩包。

```text
cloud-resource-monitor/
  cloud_monitor/                     # 采集、告警、数据库、API 与独立通知服务
    assets/notification-banners/     # 严重、警告、通知、恢复四张 PNG 横幅
  config/
    config.yaml                      # 云账号、原有任务、服务配置
    rds-performance.yaml             # RDS 性能、行锁等待与云监控补充指标
    network-traffic.yaml             # ECS 实例清单、RDS 自动发现、网络速率及三档告警线
    integrations/                    # 七牛、汇付、爱签、微信的独立账号/任务/规则
    relay/                           # 仅中转专用令牌，不放厂商密钥或业务 token
    notifications/notifications.yaml # 短信接口、钉钉、邮件和等级路由
  docs/                              # 中文接口、权限、额度、部署与排查说明
    policies/                        # 两家云的自定义权限 JSON
  prometheus/                        # 现有 Prometheus 抓取配置与仅暴露指标的代理示例
  grafana/cloud-resource-monitor.json # 可导入、可调整的 Grafana 自定义面板
  scripts/                           # API 用量估算、通知预览、旧通知配置迁移
  tests/                             # 自动化测试，含可选 MySQL 集成测试
  preview/                           # 虚构资源生成的离线通知与 Grafana 面板预览
  .env                               # MySQL 密码、通知服务 token 等环境配置
  Dockerfile
  docker-compose.yml                 # 采集服务 + 独立通知服务 + MySQL 8.4
  pyproject.toml
```

运行所需源码、配置、依赖声明和部署文件均在本目录内，不依赖上级 `work` 目录或历史更新目录。现有密钥、任务开关和通知设置按当前配置保留。目录中的真实配置包含敏感信息，不要公开分享整个项目。

用户单独要求的中转服务位于 `../cloud-resource-relay`，拥有自己的依赖、Docker Compose 和厂商凭据目录，两项目可以分别部署运行。本监控项目不读取、导入或挂载中转源码和秘密目录；四平台业务凭据只由中转保管，中转专用令牌是双方共享的唯一认证秘密。公开应用/商户标识、查询目标和全部监控逻辑仍在本项目。中转部署步骤和 17 项只读操作模板见 [中转 README](../cloud-resource-relay/README.md)。

监控到中转可直接使用 `http://实际IP:18443/v1/monitor/collect`，无需域名或证书。四平台配置中的 `192.0.2.10` 是文档占位 IP，部署时替换成中转服务器实际 IP；两端仍配置同一个专用 token。HTTP 会明文传输 token 和响应，应部署在可信网络并限制来源 IP。原 HTTPS 地址仍受支持且校验证书，中转到厂商仍使用 HTTPS。

## 部署

先按 [阿里云权限配置](docs/aliyun-permissions.md) 和 [华为云权限配置](docs/huawei-permissions.md) 创建专用采集用户、配置自定义权限，再回填该用户的访问密钥。文档附有 JSON 策略文件和控制台操作步骤。多个云账号需要在各自账号下授权；告警短信的发送凭据与采集凭据可以分别配置。

在本项目目录执行。首次部署时检查 `.env` 中的 MySQL 应用密码、root 密码以及 `NOTIFICATION_GATEWAY_TOKEN`（至少 24 字符的随机值）；检查 `config/config.yaml` 中的云账号密钥、实际 regions 和任务开关。华为云 RDS 与 CES 还需要地区对应的 `project_ids`。短信、钉钉和邮件配置在 `config/notifications/notifications.yaml`，通过独立通知服务发送；当前短信关闭，可留到后续测试。当前项目已采用独立通知配置；只有从旧版内置通知配置升级时才需执行 [通知配置迁移](docs/notification-gateway.md)。

```sh
docker compose build
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose run --rm --no-deps notification-gateway --config /app/config/notifications.yaml check
docker compose up -d
docker compose logs -f --tail=100 cloud-monitor
```

`check --live` 只检查采集配置和通知服务连接参数，不调用云 API，也不发通知。通知服务的 `check` 检查已启用渠道。首次启动会按配置采集，并通过已启用渠道发送实际告警。后续按各任务的 `interval_seconds` 调度，重启后立即重新检查。

默认基础镜像为官方 `python:3.12-slim`。镜像访问受限的环境可通过 `docker compose build --build-arg PYTHON_IMAGE=你的镜像仓库/python:3.12-slim` 使用内部镜像。

Compose 将配置文件只读挂载，MySQL 数据持久化到 `mysql-data` 命名卷。应用等待 MySQL 健康后启动，自动创建表，以非 root 用户运行，配置自动重启、日志轮转及 `/healthz` 健康检查。MySQL 只在 Compose 内部网络开放，不映射宿主机端口；应用默认只监听宿主机 `127.0.0.1:18080`。应用使用 MySQL 命名锁保证同一数据库只有一个调度实例，连接丢失会停止采集并退出，交给 Docker 重启。

```sh
curl http://127.0.0.1:18080/healthz
docker compose exec cloud-monitor python -m cloud_monitor --config /app/config/config.yaml status
```

后续更新直接同步完整项目目录中的改动到服务器项目目录，保留服务器现有 `.env`、云账号与通知凭据。仅更新通知模板或通知网关时，按[网关单独更新步骤](docs/notification-gateway.md#更新网关代码)只重建 `notification-gateway`；同时更新采集服务时，在同一目录执行：

```sh
docker compose build
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose run --rm --no-deps notification-gateway --config /app/config/notifications.yaml check
docker compose up -d
```

不需要先删除旧镜像；构建会更新同名镜像标签，`up -d` 会按镜像与配置变化重建对应容器。只修改主监控或 RDS YAML 配置时，执行 `docker compose restart cloud-monitor`；只修改通知渠道配置时，下一次通知请求自动加载。修改 `.env` 后使用 `docker compose up -d` 重建受影响的容器。

仅更新上述 RDS 采集修复时，将当前项目的代码改动同步到服务器原项目目录，保留现有 `.env`、账号、资源、任务和通知开关，然后在该目录执行：

```sh
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml diagnose --job aliyun-mysql-civil-performance
docker compose up -d --no-deps --force-recreate cloud-monitor
docker compose logs --tail=100 cloud-monitor
```

`diagnose` 使用新镜像调用只读云 API，不写监控数据库、不发送通知，要求服务器上的目标任务和账号已启用。确认输出 `status: ok` 后替换运行容器；正常服务继续按服务器现有开关调度和通知。该修复只需更新 `cloud-monitor`，无需重新构建通知网关或变更 MySQL 数据卷。

2026-10-20 网络监控上线需要单独上传 `config/network-traffic.yaml`，在生产主配置追加 `monitoring_files` 引用，并在原 Compose 中追加该文件的只读挂载；不要覆盖生产主配置、`.env` 或通知配置。采集与通知网关共用新镜像，本次先更新网关、确认健康，再更新采集服务，无需重启 MySQL。Windows PowerShell / SFTP 上传清单、上线前只读诊断和配置连同镜像的回滚步骤，见 [最新代码与服务器更新](docs/server-update.md)。

网络监控首发已完成、仅遇到华为云 RDS `byte/s` 单位报错时，按 [单位兼容修复](docs/server-update.md#华为云-rds-单位兼容修复) 上传两个源码文件及 `pyproject.toml`，再重建采集服务；当前源码包含 ECS 名称查询，需要同步 SDK 依赖。RDS 单位修复本身无需重复追加配置、修改权限或重新导入 Grafana。

MySQL 初始化账号环境变量只对首次创建空数据卷生效，已有数据库改密码需要先在 MySQL 修改账号，再同步 `.env`。保持服务器原有项目目录、Compose 项目名与 `mysql-data` 数据卷，才能继续使用原历史数据。`docker compose down` 保留数据；不要使用 `down -v`，除非需要清空全部历史状态。Docker 的 `unhealthy` 状态本身不会触发 `restart`，仍需宿主机监控或编排系统处理持续健康检查失败。

## 配置

| 配置位置 | 需要填写的内容 |
| --- | --- |
| `.env` | MySQL 库名、应用账号、应用密码、root 初始化密码、`NOTIFICATION_GATEWAY_TOKEN` |
| `service.database` | 默认连接 Compose 的 MySQL；也可回填现有 MySQL 地址、端口、库名及凭据 |
| `accounts` | 两家云独立 AK/SK、地区、账号标识；华为云的项目 ID |
| `notifications.gateway` | 主监控配置中的独立通知服务地址、认证 token、超时 |
| `notifications.sms` | 默认使用 HTTP 短信接口：地址、请求头、内容字段、额外参数和成功判定；可对接第三方供应商 |
| `notifications.dingtalk` | 群机器人 Webhook、加签 secret、匹配安全设置的关键词；可选 `banner_urls` 配置四种状态的 HTTPS 图片地址 |
| `notifications.email` | SMTP 地址、SSL/STARTTLS、用户名、授权码、收发件人 |
| `rules` | 各账号余额阈值、到期天数、使用率及持续时间 |
| `jobs[].alerting` | 按证书域名、资源 ID 或资源包 ID 选择哪些资源参与告警；未配置时保持全部告警，详见 [按资源配置告警](docs/resource-alerts.md) |
| `jobs[].options.metrics` | 资源 ID、精确监控维度、指标名称、预算/上限及单位换算 |
| `jobs[].options.product_code` | 阿里云短信/SLB 资源包的实际商品代码，不能用业务服务名替代 |
| `config/rds-performance.yaml` | RDS 原生性能、批量云监控指标、数据库性能告警阈值与独立任务开关 |
| `config/network-traffic.yaml` | ECS 实例清单、RDS 自动发现、双向网络速率、P0/P1/P2 初始阈值和持续时间 |
| `config/integrations/*.yaml` | 四平台公开身份、中转地址、固定操作名及 `options.collector` 原采集参数；台账、订单监控清单、公开证书在此目录本地读取，账号和任务启用状态以当前配置为准 |
| `config/relay/` | 按账号保存只允许固定监控查询的中转令牌，只读挂载；不保存四平台厂商凭据或业务 token |
| `api` | 可选状态查询 token，以及短信回执回调 token |
| `api.metrics` | 指标接口默认启用；可选 `enabled` 和独立 Bearer `token`，详见指标接入文档 |
| `service.retention_days` | MySQL 历史明细保留天数，默认 30；不控制 Prometheus 保留期 |

当前配置已包含回填内容，不是全空模板。需要补充资源信息或回执接入的任务继续保留关闭；填完必需字段后再启用。只有启用账号下的启用任务会调度。`check --live` 会拒绝已启用但缺少指标、维度或预算的任务。

`monitoring_files` 可追加包含 `accounts`、`jobs`、`rules` 的相对 YAML 文件，不能覆盖服务或通知配置。新规则可使用 `for_samples` 要求连续有效样本数，重复源时间不计数；新平台采集中途失败会打断业务指标的连续计数，但不自动恢复已有告警。详见 [四平台数据与告警口径](docs/external-platforms.md#数据与告警口径)。

上表 `notifications.sms/dingtalk/email` 位于独立的 `config/notifications/notifications.yaml`；主配置的
`notifications.gateway` 只保存网关地址、认证 token 和超时。通知服务默认监听本机 `127.0.0.1:18081`，
没有 MySQL 或采集账号依赖。修改渠道配置、接收人和 `routes` 后，下次请求自动生效，不必重启监控。
通知服务故障时，监控继续采集并保留待发送队列。接口格式、第三方短信示例、测试与迁移步骤见
[独立通知服务](docs/notification-gateway.md)。
查看网关日志使用 `docker compose logs -f --tail=100 notification-gateway`；日志为空、配置文件缺失与网关单独更新步骤见
[通知网关日志与排查](docs/notification-gateway.md#查看日志与排查)。

额度使用率 = 原始值 × `multiplier` ÷ `limit` × 100；`qps_ratio` 不乘 100。`limit` 必须与换算后的值使用相同单位。阿里云与华为云 API 返回的单位、周期和指标维度不完全相同，详见各厂商文档。对象存储容量没有统一硬上限，配置的是实际采购额度或你确定的预算，不会自动把任意数当作容量上限。

支持 `${ENVIRONMENT_VARIABLE}` 引用；Docker 使用时还需要在 Compose `environment` 中传入相应变量。四平台中转令牌使用 `relay.token_file`，每次请求重读文件；Compose 已移除七牛 AK/SK 环境变量及 `config/credentials` 挂载。原有凭据文件不会被自动删除，迁移检查见 [中转部署与凭据迁移](docs/collection-relay.md#部署与凭据迁移)。配置文件已加入 `.gitignore`，Docker 构建上下文排除了整个 `config` 目录，真实密钥不会写入镜像。

### 阿里云多账号

`config/config.yaml` 当前包含 `aliyun-hycen` 和 `aliyun-yuanzhu` 两个阿里云账号配置位，不限定只能有两个账号。每个账号分别填写自己的 `access_key_id`、`access_key_secret` 和 `regions`，以当前 `enabled` 值决定是否参与调度。账号启用后，该账号下未写 `enabled` 的任务默认运行；不需要的任务请设置 `enabled: false`。

| 账号标识 | 任务 ID 前缀 | 余额规则 ID |
| --- | --- | --- |
| `aliyun-hycen` | `aliyun-`（原有任务，不含 `aliyun-secondary-`） | `aliyun-balance` |
| `aliyun-yuanzhu` | `aliyun-secondary-` | `aliyun-secondary-balance` |

实际使用哪份密钥由任务的 `account` 字段决定，必须与 `accounts` 中的 `id` 完全一致，不能仅看任务 ID 前缀。OSS、ALB/SLB 等监控对象按实际账号归属配置：每个 Bucket 填一条容量指标和一组请求量、出网流量指标；负载均衡必须使用对应产品的指标与维度。同一账号有多个资源时，可在相应任务的 `options.metrics` 下添加多条；不同地区可逐条填写 `region`，覆盖任务的 `options.region`。资源 ID、监控维度、额度及换算系数填好后，再启用这些任务。

新增第三个及更多账号时，可复制 `aliyun-yuanzhu` 账号配置、所需的 `aliyun-secondary-*` 任务和 `aliyun-secondary-balance` 规则。为新账号、任务、规则分别设置唯一 ID，并同步修改任务的 `account`、余额规则的 `match.account` 和余额任务的 `rules`。只新增密钥配置不会自动生成采集任务。

各账号共用独立通知服务中的短信、钉钉和邮件渠道；发送告警短信的凭据可属于专门的通知账号或第三方供应商。需要接入多个账号的阿里云业务短信回执时，在 `api.receipts.accounts` 下为实际接入的每个账号填写不同的、至少 24 字符的随机 token，并启用各自的 `sms-receipts` 任务。回调路径中的账号 ID 也需分别替换，例如 `/webhooks/aliyun/sms/aliyun-yuanzhu`；开启回执时，映射中不能保留未填写 token 的账号条目。

云监控免费 API 额度按阿里云主账号分别核算，同一主账号下的多个 RAM 用户共享额度；配置多个 ID 不会增加同一主账号的免费额度。当前频率及用量见 [当前配置调用量审计](docs/current-api-usage-audit.md)，新增资源或调整频率后可运行 `python scripts/estimate_api_usage.py config/config.yaml --days 31` 重新估算。

## Prometheus 与 Grafana

数据路径为：云 API / 四平台中转接口 / 爱签本地台账采集 → MySQL 明细与最新样本 → `GET /metrics` → 现有 Prometheus → 现有 Grafana。抓取只读取本项目的 MySQL，不额外调用云 API，不改变采集任务频率。原有告警引擎与独立通知网关继续处理通知。

- 同机 Prometheus 可抓取 `http://127.0.0.1:18080/metrics`；同一 Docker 网络使用 `cloud-monitor:8080`。跨服务器使用 [仅开放指标的 Nginx 示例](prometheus/nginx-metrics.conf)，按 Prometheus 来源 IP 放行。
- 将 [抓取配置](prometheus/scrape-config.yml) 中的任务合并到现有 `scrape_configs`。Prometheus 启动参数使用 `--storage.tsdb.retention.time=200h`；这是整个 Prometheus 实例的保留期，不是某个任务的 YAML 选项。
- Grafana 导入 [面板 JSON](grafana/cloud-resource-monitor.json)，选择已有 Prometheus 数据源和一个采集服务实例；可按云厂商、账号、资源筛选，展示告警、余额、到期、额度和数据库性能。
- 表格中的资源 ID、账号、资源包 ID 等标签按文本原样显示，单位只应用到数值列；到期表只有“剩余天数”带“天”。旧面板若出现资源 ID 带“天”，重新导入更新后的 JSON 并覆盖原仪表盘即可，无需重启采集服务。
- MySQL `samples` 保存按源时间去重的明细，`latest_samples` 保存每个序列的最新值。默认 30 天，每 24 小时清理一次，因此正常运行时最老记录可能接近 31 天。当前告警状态及待发、失败通知不随历史明细自动删除。

升级时自动创建最新样本表，下一次原有调度采集后填充；不会在启动时回扫历史表。Prometheus 从首次抓取开始积累历史，不自动回放 MySQL 旧数据。RDS 性能图表仅展示已启用且成功采集的实例；关闭的 PostgreSQL 模板不会生成数据。数据新鲜度、单位、认证和部署步骤见 [完整接入文档](docs/prometheus-grafana.md)。

## 告警行为

证书和资源包等任务支持**按资源单独开关告警**：在现有任务下添加 `alerting`，用 `default_enabled: true` 加排除清单，或用 `default_enabled: false` 加启用清单。证书可按域名或证书 ID 选择，资源包可按包 ID 整体选择到期与额度告警。未配置的任务沿用原有行为；采集、存储、Grafana 资源数据与 API 调用量不变，采集异常仍会告警。具体示例、匹配规则及生效步骤见 [按资源配置告警](docs/resource-alerts.md)。

2026-10-23 已按 `aliyun-hycen` 的证书 ID 白名单方式，将 `aliyun-yuanzhu-certificates` 设置为默认关闭、仅对当时尚未过期的 15 张证书开启告警，排除同次发现的 40 张过期证书。保留每天检查一次、30 天内 P1、7 天内 P0 的原有规则。该清单固定本次选中的证书 ID，续签或新增证书需要另行加入；证书清单见 [当前 yuanzhu 证书告警范围](docs/resource-alerts.md#当前-yuanzhu-证书告警范围)。

通知顶部图片的标题固定为“慧橙云能监控告警”，P0 严重为红色、P1 警告为黄色、P2 通知为蓝色、恢复为绿色。钉钉和邮件的标题、正文不再显示 `[P0]`、`[P1]`、`[P2]` 前缀，保留中文等级标签及“告警触发”“已恢复”状态；内部等级与通知路由保持不变。四张 PNG 已随项目保存，当前项目的钉钉 `banner_urls` 已填写四种状态的 HTTPS 图片直链；分享链接使用 `?raw=1` 返回图片。配置同步到服务器后，网关在下一次通知请求时读取新地址；某状态未填写地址时仍使用对应颜色的文字标记。邮件直接附带图片，不依赖公网图片地址；短信保持文字。图片、配置方法和离线预览见[通知展示格式](docs/notification-format.md)。

- 默认 P0 发短信，P1 发钉钉群，P2 发邮件，渠道映射在通知配置的 `routes` 中修改。同一规则同时满足多个等级时只发送最高等级。关闭渠道时仍保留告警，通知记为 `skipped`，不会补发或自动降级；当前短信关闭。
- 到期默认 30 天 P1、7 天 P0，已到期继续 P0。资源包使用率默认 80% P1、90% P0；短信余量 10% P1、耗尽 P0。
- 余额按 Excel 的初始值：2000 元 P1、1000 元 P0，可按账号修改。没有自动用不完整账单推断日均消费。
- 表中没有确定的 P2 业务阈值，因此没有虚构额外业务阈值。采集异常使用 P2 邮件通知，也可在规则中自行增加 P2。
- 首次越界、等级变化即时排队。同等级重复提醒间隔默认 P0 6 小时、P1/P2 24 小时，并且需要新的有效采集值。日采集任务最多随日采集提醒一次。
- 默认连续 2 个健康采样后发送恢复通知，恢复沿原告警级别渠道发送。每日任务的恢复确认可能需要次日采集，可以改 `recovery_samples: 1`。
- MySQL 保存去重、连续超阈值状态及发送队列。发送失败按 30 秒起步指数退避，最多 8 次，之后保留失败状态。新的有效提醒会替代旧失败消息。
- 云 API 失败、缺失关键字段、过期数据不会当作 0 或资源恢复。没有资源或没有足够短信回执时显示 `no_data`；短信日统计缺少日期或零基线无法计算增长率时显示 `insufficient_data`。历史资源告警不会仅因无数据或统计不足自动恢复。

失败队列可在修复通知配置后重试：

```sh
docker compose exec cloud-monitor python -m cloud_monitor --config /app/config/config.yaml retry-failed
```

由于外部通知接口不能与本地事务一起提交，若服务在供应商接受消息后、写入成功状态前中断，重试可能产生重复通知。事件 ID 保持一致，便于查重。短信模板字段名必须对应审核通过的模板；短信供应商接受请求不等于手机最终送达。

## 短信成功率

`QuerySendStatistics` 是按日统计，不能证明“持续 5 分钟失败”。实时成功率由独立的阿里云短信回执接入计算。以 `aliyun-hycen` 为例，填写 `api.receipts.accounts.aliyun-hycen.token` 为至少 24 字符随机值，启用 `api.receipts.enabled` 和 `aliyun-sms-receipts` 任务，将公网 HTTPS 回调配置为：

```text
https://your-monitor-domain/webhooks/aliyun/sms/aliyun-hycen?token=YOUR_RANDOM_TOKEN
```

使用 Nginx/Caddy 等反向代理转到 `127.0.0.1:18080`，访问日志不要记录查询串中的 token。也支持 `Authorization: Bearer TOKEN`。回调接收阿里云批量 JSON 回执，成功返回 `{"code":0,"msg":"success"}`。数据库只保存去重哈希、账号、报告时间和成功标记，不保存手机号或短信内容。

每分钟采集，计算最近 5 分钟**已收到的最终回执**的成功率；至少 20 条才计算。低于 80% 持续 5 分钟 P0，低于 95% 持续 10 分钟 P1。未到回执不算成功或失败，回执丢失/延迟会影响该口径。没有公网回执地址时保持任务关闭，不能宣称已实现实时送达率监控。当前业务回执解析仅支持阿里云；P0 告警短信由独立通知服务发送，可使用第三方 HTTP 接口，也保留阿里云、华为云适配器。

## 本地验证

```sh
python -m venv .venv
# Activate the virtual environment using your platform's command.
python -m pip install -e ".[test]"
python -m pytest -q
python -m cloud_monitor --config config/config.yaml check
python -m cloud_monitor --config config/config.yaml demo
```

`demo` 使用临时内存库模拟临期、升级、恢复，永不发送通知，不连接生产 MySQL。`once` 默认也不发送，但会调用真实只读云 API，并将告警写入 MySQL 队列，后续 `serve` 会发送仍有效的待发消息。仅在要即时发送时使用 `once --send`。运行中的同数据库服务会阻止第二个 `once` 进程。

排查采集失败时使用 `diagnose`，它仅调用只读云 API，不连接数据库、不创建告警、不发送通知，运行中的监控服务无需停止。`--job` 可重复，不指定时检查所有启用任务；需要数据库的短信回执任务会明确跳过。输出只包含状态、样本数量和受限的安全错误详情，详见 [采集报错排查](docs/troubleshooting-collection.md)。

短信统计不足时，诊断输出 `status: insufficient_data`、`samples: 0` 与具体安全原因，退出码为 0。只有成功完成分页并校验返回数据后，才会作此判断；权限、接口、分页或格式异常仍返回 `error` 和非零退出码。

```sh
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml diagnose --job huawei-balance --job huawei-elb-bandwidth
```

可直接打开 [通知样式预览](preview/index.html)；它仅使用虚构资源，不发送通知。修改通知排版后执行 `python scripts/preview_notifications.py --output preview` 可重新生成。可选的实际 MySQL 集成测试需通过 `MYSQL_TEST_DSN` 指向独立测试库服务器，测试账号需要创建、删除测试数据库的权限。

`/healthz` 检查调度心跳；`/status` 和 `/readyz` 需配置 `api.admin_token` 并使用 Bearer 鉴权，展示采集错误、数据过期和通知终止失败。`/readyz` 将 `insufficient_data` 与 `no_data` 一样视为查询可用，统计不足不会单独使服务未就绪，但仍可从状态和面板看到。项目包含离线 API 模拟、配置与数据库测试；真实采集验证结果见各项排查和 RDS 文档。新账号、新资源及实际通知送达仍需按其配置联调。

接口与最小权限说明：[阿里云](docs/aliyun-api.md)、[华为云](docs/huawei-api.md)、[通知渠道](docs/notifications.md)。
