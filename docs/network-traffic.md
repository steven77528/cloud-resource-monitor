# ECS 与 RDS 网络速率监控

2026-10-20 新增 `kind: network_traffic`，复用云账号、MySQL 存储、Prometheus、Grafana、告警引擎和独立通知网关。只使用云 API，不登录 ECS，不连接业务数据库。独立配置位于 [network-traffic.yaml](../config/network-traffic.yaml)，通过主配置 `monitoring_files` 加载。

## 数据口径

| 云厂商与产品 | 命名空间与源指标 | 原始粒度、维度 | 展示口径 |
| --- | --- | --- | --- |
| 阿里云 ECS | `acs_ecs_dashboard`：`IntranetInRate` / `IntranetOutRate` | 配置 60 秒，元数据校验；`instanceId` | 内网入出速率，`scope=intranet` |
| 阿里云 RDS MySQL | `acs_rds_dashboard`：`MySQL_NetworkInNew` / `MySQL_NetworkOutNew` | 配置 60 秒，元数据校验；`instanceId` | 实例总网络吞吐，`scope=all` |
| 华为云 ECS | `SYS.ECS`：`network_vm_bandwidth_in` / `network_vm_bandwidth_out`，减对应 EIP 的 `SYS.VPC` 公网速率 | 统一为 300 秒平均值；`instance_id` 与 `publicip_id` | 内网估算，`scope=intranet_estimated` |
| 华为云 RDS MySQL | `SYS.RDS`：`rds004_bytes_in` / `rds005_bytes_out`，减对应 EIP 公网速率 | 原始 60 秒，统一查询 300 秒平均值；`rds_cluster_id` 与 `publicip_id` | 受支持实例的内网估算，`scope=intranet_estimated` |
| 华为云 RDS PostgreSQL | 同上指标与 EIP 公网速率 | 原始 60 秒，统一查询 300 秒平均值；`postgresql_cluster_id` 与 `publicip_id` | 内网估算，`scope=intranet_estimated` |

统一输出 `network_in_bps`、`network_out_bps`，单位为 `bit/s`。华为云按每个数据点的返回单位换算，`Byte/s`、`B/s`、`byte/s`、`Bytes/s`、`bytes/s` 均乘 8；RDS 的 `KiB/s` 及已有兼容别名 `KB/s`、`KByte/s` 均乘 8192。2026-10-20 生产诊断确认 `SYS.RDS` 的 `rds004_bytes_in` 可返回 `byte/s`，因此不能对 RDS 固定使用 8192。未知或缺失单位仍拒绝采集。阿里云首次及默认每 24 小时读取 `DescribeMetricMetaList`，核对单位、统计字段、维度和周期后换算；未知或有歧义的单位拒绝采集，不猜测 K 的进制。

2026-10-27 当前项目直接将原 `huawei-ecs-network`、`huawei-rds-network` 及其四条业务规则替换为内网估算；任务、规则 ID 和原数值阈值、持续窗口继续使用，未新增一套并行总量告警。两项华为任务均每 300 秒查询一次，`period=300`、`filter=average`，只采用已闭合窗口。计算分别为 `内网入站估算 = 总入站 - 全部关联 EIP 公网入站`、`内网出站估算 = 总出站 - 全部关联 EIP 公网出站`，不能在入站与出站之间相减。

ECS/RDS 和全部关联 EIP 必须属于同地域、同项目，公网地址与端口关系无冲突且映射完整；每个方向及两个方向间均须存在严格相同的源时间戳。实例总量按实际单位换算到 `bit/s` 后，减去该实例所有已关联 EIP 的同向速率。EIP 入站用 `downstream_bandwidth`，出站用 `upstream_bandwidth`，维度为 `publicip_id`，不把共享带宽总值分摊给实例。关联变更后等待新绑定下完整闭合的窗口，避免用新 EIP 减旧窗口。

缺关联、关联冲突、源数据缺失、单位非法、时间不齐、陈旧或负差时，该实例双向数据均不输出，不补零、不将负值截成零。2026-10-30 已部署版本将有限负差单列为 `degraded`（估算不可用），不因此触发采集失败；其他实际采集或校验失败仍为 `error`。任务为 `ok` 或 `degraded` 时，只导出源样本仍新鲜且未被标记不可用的资源；真实错误或任务陈旧时，隐藏该估算任务全部当前值。RDS 官方说明数据可能延迟 5-10 分钟，因此估算回看 1800 秒、允许源数据年龄 900 秒；持续告警按源时间计算，通知仍受云端延迟影响。

该差值保留“估算”标签：测量位置不同，NAT、负载均衡等未经过已关联 EIP 的公网路径仍可能留在差值里。关联与现场证据见 [华为云口径核对](huawei-network-scope-audit.md)。阿里云 RDS 当前指标及元数据未确认内外网分项，继续明确展示总量；`rds:DescribeDBInstanceNetInfo` 只能查询地址类型，进一步探测权限与限制见 [阿里云 RDS 核对](aliyun-rds-network-scope-audit.md)。

RDS 汇总数据可能包含复制等流量，不能还原某台 ECS 到某台 RDS 的流量。当前每次查询保留最新有效点，不提供精确日/月累计流量，不能直接将速率相加。逐通信关系分析仍需 VPC 流日志。

本轮未覆盖阿里云 PostgreSQL 网络速率，因为尚无已核准的源定义，不影响原有 PG 性能任务。华为云 MySQL 集群、分布式实例需要节点维度，本轮不支持。华为云 ECS 自动发现仅涵盖 CES 中具有相应基础指标的实例。

## 估算可用性

2026-10-30 的原始数据核查确认：相同的 `300/average` 聚合标签不保证 ECS 与 EIP 的实际统计窗口可比。`prometheus` 14:10 出站的 ECS 总量为 `60,701.44 bit/s`，EIP 公网为 `638,741.87 bit/s`，差为 `-578,040.43 bit/s`。这表示该窗口的差分估算不可用，不能解释为内网流量小于零。固定平移五分钟未改善负差；改用 Agent 原生分钟样本再计算五分钟均值也未消除负差，详见 [专项核查](huawei-network-negative-difference-research.md)。保留当前源、单位换算、300 秒平均窗口、源时间策略及阈值，不将调整采样或平均方式当作已验证修复。

| 情况 | 任务及采集健康 | 当前值与业务判断 |
| --- | --- | --- |
| 全部估算可用且查询校验成功 | `ok` | 按原规则导出新鲜估算并判断阈值 |
| 只有有限负差，且无真实错误 | `degraded`；不新增 `collection-error`，查询成功可参与既有采集健康告警恢复 | 不输出受影响资源的任一方向，其他可用资源继续导出和判断 |
| API、缺字段、非法单位、非有限值、缺失/陈旧/不对齐数据、映射等真实错误 | `error`；沿用采集健康告警 | 隐藏整项估算任务的当前值；成功样本仍可保存 |

按任务、地域和资源持久化每次负差的无效源时间，进程重启不会将旧值恢复为可用。必须取得**同一资源、严格晚于其无效源时间的有效入出站配对点**才能恢复该资源；重复旧点、仅另一个资源恢复或一轮查询成功都不能代替这一条件。一个方向为负即同时关闭该资源双向估算。不可用期间保留已经生效的业务告警，不发送虚假业务恢复；超限候选持续时间和业务恢复连续性被中断，恢复后从有效源点重新累计。

新的负差水位还会取消同一资源已经排队或等待失败重试的业务恢复通知，但只取消其恢复依据的源时间不晚于该负差水位的消息。已有业务活动告警继续保留，业务触发（`firing`）通知不因负差被取消，其他资源或更新源时间的恢复消息不在此取消范围内。

完整且成功的目标发现会记录当前采集资源范围。已移出该范围的资源不再因历史质量记录使任务为 `degraded`，也不导出该资源的可用性序列或旧估算值；其历史负差水位仍保留。资源重新被发现后，仍须取得严格晚于该水位的有效双向配对点才能恢复，不能靠移出后重新发现绕过恢复条件。发现失败或清单不完整不能当作一次成功的范围更新。

`/readyz` 接受 `degraded`，表示采集服务仍可查询，不保证每台资源都有可用估算；真实错误仍使就绪检查失败。Prometheus 额外导出 `cloud_monitor_network_estimate_available`，标签为 `provider`、`account`、`job_id`、`resource_id`、`region`、`reason`；`0` 表示该资源估算不可用，`1` 表示已取得有效配对点；曾负差的资源还须由严格更新的有效配对点确认恢复。有负差历史时保留 `reason=negative_difference`，从未记录负差的有效资源为 `reason=""`。该指标覆盖已取得有效双向配对点或已有负差记录的资源，并非目标发现清单的完整健康证明；没有该序列不能直接解读为已验证可用，值为 `1` 也不代替任务错误和样本新鲜度检查。

项目通用 Grafana 看板文件增加独立的“内网估算质量 / 内网估算可用性”表，并将 `degraded` 显示为橙色“估算不可用”。曲线缺值与质量表配合判断，不以零流量替代不可用。2026-10-30 已部署采集代码并核验可用性指标；线上现用看板尚未应用新增表。账号、任务、通知渠道和阈值继续使用原配置，核验与回滚见 [上线记录](server-update.md#2026-10-30-华为内网估算质量处理)。

## 资源发现

新配置为现有三个账号分别增加 ECS/RDS 任务，不修改旧账号开关。启用账号的新任务在部署启动后生效，关闭账号仍不运行。阿里云 RDS 默认只选 MySQL，华为云选择 MySQL、PostgreSQL。

2026-10-26 当前项目的 `huawei-ecs-network` 已移除 `options.instances: []`，恢复在账号/任务指定地域自动发现。两个阿里云 ECS 任务保留各自实例清单，本地未填写的空清单表示暂不采集；填写方法见下节。RDS 继续自动发现。既有部署未配置 `instances` 的任务仍按原方式自动发现，升级代码不会自动改写服务器配置。不要将本地阿里云空清单覆盖到已有生产清单；华为恢复步骤见 [服务器更新](server-update.md#2026-10-26-恢复华为-ecs-自动发现)。

未填写 `options.instances` 时，按任务 `region`、`regions` 或账号 `regions` 选定地域，清单缓存 300 秒。阿里云使用 ECS/RDS 实例列表，包括按量付费资源；华为云 ECS 使用 CES `ListMetrics`，RDS 使用 `ListInstances`。资源列表分页失败不视为完整清单，真实空清单显示 `no_data`，不伪造零流量。

华为云 ECS 在 CES 发现后，通过 ECS `ListServersDetails` 按相同地域、项目精确匹配 `id` 和 `name`。每批最多 100 个 `server_id`，覆盖按需和包年包月实例，不通过费用中心猜名称。名称随清单默认缓存 300 秒，改名在缓存更新后生效。图例已有 `resource_name`，无需修改看板；新采样携带名称，历史数据的 UUID 标签不会被回写。

自动发现时的名称查询失败、实例名称缺失时，对应名称可回退到资源 ID，并输出不含云端原始响应的日志提示；缺名本身不单独触发业务告警。内网估算还必须查询 ECS/RDS 详情和 EIP 完整列表以核对绑定关系，详情无权限或清单不完整时会停止对应估算，不适用仅靠 CES 总量继续展示的旧模式。显式清单可用 `resource_name` 覆盖显示名称，仍须做关联盘点。

### 只采集指定 ECS

编辑 `config/network-traffic.yaml`，找到实例所属账号的任务，在已有 `options` 下添加 `instances` 或将 `instances: []` 替换为实际清单。华为任务当前采用自动发现，只有需要限制实例范围时才添加清单：

| 任务 ID | 所属账号 | 数据口径 |
| --- | --- | --- |
| `aliyun-ecs-network` | `aliyun-hycen` | ECS 内网入站、出站 |
| `aliyun-yuanzhu-ecs-network` | `aliyun-yuanzhu` | ECS 内网入站、出站；仍受原账号开关控制 |
| `huawei-ecs-network` | `huawei-hycen` | ECS 内网入站、出站估算 |

以下是阿里云 ECS 任务的 `options` 示例，清单可写多台、多个地域。保留任务原有 `account`、`enabled`、`rules`、采样周期和阈值：

```yaml
options:
  product: ecs
  instances:
    - resource_id: i-your-app-instance
      resource_name: 应用服务器
      region: cn-hangzhou
    - resource_id: i-your-worker-instance
      resource_name: 任务服务器
      region: cn-shanghai
  period: 60
  inventory_cache_seconds: 300
  max_age_seconds: 300
```

`resource_id` 和 `region` 必填且精确匹配，不能用名称、通配符或 IP 代替实例 ID；`resource_name` 可选，填写后用作图例名称。同一任务不能重复填写相同地域和 ID。华为云改填控制台的 ECS UUID，保留 `scope: intranet_estimated`、`period: 300`、任务间隔 300 秒和 `max_age_seconds: 900`，选定地域仍须在主配置中有 `account.project_ids`。

清单同时控制入站、出站两条速率及其网络业务告警；不在清单中的 ECS 不查询其网络指标。显式清单跳过目标发现；华为内网估算仍按所选实例涉及的地域查询完整 ECS/EIP 清单，以校验绑定关系。清单不影响 ECS 到期任务或其他产品任务，三个 ECS 清单彼此独立，账号关闭时清单不会启用账号。

- `instances: []`：明确选择零台 ECS，零网络 API 调用，任务显示 `no_data`，不视为采集故障。
- 填写非空列表：只采集所列 ECS 的入站、出站速率。
- 删除整个 `instances` 字段：恢复账号/任务指定地域的自动发现。不要把它写成空值 `instances:` 或 `null`，这会校验失败。

配置随采集服务启动读取，修改后需重新创建采集容器，首次使用此版本还需同步源码并重建镜像，见 [服务器应用步骤](server-update.md#ecs-网络速率按实例清单采集)。本地可先离线检查，网络任务清单即使账号关闭也会校验，不要求填写云凭据和数据库连接，不访问云 API、不连接 MySQL、不发送通知：

```powershell
.\.venv\Scripts\python.exe -m cloud_monitor --config config/config.yaml check
```

移出清单的 ECS 在新配置生效后停止导出当前网络样本；原有网络告警、持续超限候选状态和待发送/失败重试通知会在服务初始化时清理，不发送恢复通知。保留清单内实例的状态及任务采集健康告警。MySQL 历史采样、Prometheus 已存历史曲线和已发送消息保留，因此 Grafana 选择包含变更前的时间范围时仍可能看到旧实例曲线；新采样仅来自清单内实例，无需导入新看板。

RDS 也可使用显式清单，但必须为非空列表；每个实例还需 `engine: MySQL` 或 `engine: PostgreSQL`，阿里云本轮只接受 MySQL。新账号需要建立对应任务，不会由密钥自动生成任务。

单个实例无数据、陈旧或拓扑不支持时，仍保存其他正常实例的双向数据，但整项任务标记为 `error`，保留成功样本数和失败实例数，并走原有采集异常告警。全部目标发生真实失败或资源盘点失败时也按采集失败处理。只有有限负差按上节单列为 `degraded`；同时存在负差和真实错误时仍以 `error` 为准。`diagnose` 对真实错误显示部分失败，对仅有负差显示 `degraded` 与不可用资源数，不输出原始响应或资源明细。CES 删除资源后会保留约 3 小时指标缓存，自动发现可能暂时包括已删除对象；可用显式清单排除。

## 三档告警

以下为缺少实例规格与历史基线时的**绝对速率初始运维线**，不代表云厂商限速、带宽配额或业务容量。入、出方向各自判断，不相加。1 Mbps = 1,000,000 bit/s。

| 资源 | P2 邮件 | P1 钉钉 | P0 短信 |
| --- | --- | --- | --- |
| 阿里云 ECS 内网 | >=100 Mbps，持续 15 分钟 | >=300 Mbps，持续 10 分钟 | >=800 Mbps，持续 5 分钟 |
| 华为云 ECS 内网估算 | >=100 Mbps，持续 30 分钟 | >=300 Mbps，持续 15 分钟 | >=800 Mbps，持续 10 分钟 |
| 阿里云 RDS MySQL 总网络 | >=50 Mbps，持续 15 分钟 | >=100 Mbps，持续 10 分钟 | >=200 Mbps，持续 5 分钟 |
| 华为云 RDS 内网估算 | >=50 Mbps，持续 15 分钟 | >=100 Mbps，持续 10 分钟 | >=200 Mbps，持续 5 分钟 |

ECS 使用较高绝对速率线，RDS 用较低吞吐线以提前观察数据库负载；华为云 ECS 因五分钟采样使用更长窗口。相同 bit/s 跨云单位一致，不因厂商不同而假设不同实例容量。上线后应结合正常峰值、批量任务与实例规格调整；低带宽实例可能更早达到容量上限，高带宽实例的正常业务也可能超过初始线。

规则按厂商、产品、范围和方向隔离。同一源时间重复返回不增加持续时间；源时间间隔超过 1.5 个采样周期会中断连续性。多等级同时满足时由原引擎选最高等级，默认连续两个正常源样本后恢复。重复提醒与渠道是否启用继续使用既有配置。通知以 Kbit/s、Mbit/s、Gbit/s 展示，阈值判断保留原始精度。

业务阈值在独立配置的 `rules[].levels` 中修改；单个实例可新增带 `match.resource_id` 的规则并调整任务绑定。只采集不做业务告警时，将任务 `rules` 设为 `[]`，采集异常告警仍保留。

内网替换生效时，服务初始化会清理这两项任务旧 `scope=all` 的活动告警和持续候选，并取消待发送、失败重试及待发送恢复消息，不发送人为恢复通知。任务健康告警、其他任务和账号保留；已发送消息、MySQL 历史样本及 Prometheus 已有历史不会删除。新内网告警从新的有效估算源样本重新累计，旧总量不再参与当前导出或业务判断。

## 权限与调用量

阿里云复用 `ecs:DescribeInstances`、`rds:DescribeDBInstances`、`cms:QueryMetricList`。指标元数据 API 未公布对应 RAM 动作，不编造 `cms:QueryMetricMeta`，详见 [阿里云权限](aliyun-permissions.md)。

华为云复用 `rds:instance:list`、`ces:metricData:list`、`ecs:cloudServers:list`；ECS 自动发现另需旧 1.1 自定义策略授权项 **`ces:metrics:list`**。内网估算还需 **`vpc:publicIps:list`** 查询 EIP 绑定；用户已在现有 `cn-east-3` 区域项目补充并验证，最小补充文件为 [EIP 列表策略](policies/huawei-eip-discovery.json)。不要用仅一项的补充文件覆盖原权限。显式 ECS 清单不调用 CES `ListMetrics`，但估算仍需要 ECS 详情和 EIP 列表。见 [华为云权限](huawei-permissions.md)。

按 31 天估算，阿里云分钟任务每实例双向约 89,280 次数据查询；华为内网估算每轮是 `2N + 2E` 次 CES 查询，`N` 为选中实例数、`E` 为其关联 EIP 总数。每实例恰好一个 EIP 时，五分钟双向总量加双向公网共约 35,712 次/月，多个 EIP 继续增加。目标发现、名称批次、每个有选中实例的地域的 ECS/RDS 关联清单和 EIP 列表分页另计，关联缓存默认 300 秒；显式清单也有该盘点成本，ECS/RDS 任务之间不共享此缓存。阿里云元数据默认每天每地域每方向查询一次，不占四种监控数据 API 的百万次免费池。Prometheus/Grafana 抓取不额外调用云 API。

自动发现时离线不能确定实例数及实际有目标的地域；显式实例数也不能确定 EIP 数和完整清单页数。估算器保留最低调用量和公式，自动发现显示 `instance_count_required`，非空显式华为估算显示 `eip_count_required`、精确监控调用量为未知；未验证数目时不声称得到完整总量。阿里云池未知实例数时也不声称额度足够。旧 89,280 或 133,920 次不能再作为新增网络任务后的总量。运行：

```bash
python scripts/estimate_api_usage.py config/config.yaml
```

华为云 ECS 自动补齐实例名称还需 **`ecs:cloudServers:list`**（1.1 自定义策略），对应 `GET /v1/{project_id}/cloudservers/detail`，与已废弃的 OpenStack `ecs:servers:list` 不同。名称请求按清单刷新频率每地域每 100 台一次；按 31 天、五分钟调度估算，每个非空地域每 100 台约 8,928 次名称查询，另计 CES 发现分页及流量查询。估算器对自动发现保留实际实例数未知状态并显示该公式，不把它算进阿里云 CMS 免费池。

## 服务器应用

若网络监控已接入，仅华为云 RDS 报 `CES network rate has an unexpected unit` 且诊断确认单位为 `byte/s`，按 [单位兼容修复](server-update.md#华为云-rds-单位兼容修复) 更新 `cloud_monitor/providers/huawei.py`、`cloud_monitor/providers/network_traffic.py`，先只读诊断再替换采集服务。这是返回单位兼容问题，不需要为此修改 IAM、监控配置、阈值、数据库或 Grafana；若网络新版通知网关尚未部署，仍需按以下首发步骤更新网关。

网络监控首次接入的完整上传清单、备份、逐条检查、上线和回滚命令见 [最新代码与服务器更新](server-update.md#1-确认原部署并备份)。首发同时更新采集与通知展示：

1. 同步源码、面板及独立的 `config/network-traffic.yaml` 到服务器原项目目录，保留生产 `.env`、主配置、通知配置、旧开关和原 Compose 项目名。
2. 在生产主配置的原 `monitoring_files` 中**追加** `network-traffic.yaml`，在生产 Compose 的 `cloud-monitor.volumes` 中追加 `./config/network-traffic.yaml:/app/config/network-traffic.yaml:ro`。核对新任务账号 ID、地域和华为云 `ces:metrics:list` 权限。镜像不包含配置文件，缺少挂载会导致启动失败。
3. `docker compose build cloud-monitor` 构建两个服务共用的新镜像；分别运行采集配置 `check --live`、网关配置 `check` 和按启用任务选择的 `diagnose`。前两项不调用云 API，诊断只读查询云 API，不写监控数据库、不发通知。
4. 检查通过后先替换 `notification-gateway` 并确认健康，再替换 `cloud-monitor`，均使用 `--no-deps`。网关包含新增的中文指标名与速率格式，需要使用新代码；不修改渠道配置、不重启 MySQL。启动采集服务后立即运行已启用任务，并可能通过现有渠道发送真实告警。
5. 本次图表目标是现有 **云业务与开发 · 运行明细**（UID `cloud-business-runtime-v1`），直接替换华为 ECS/RDS 入站、出站四图，保留面板 ID、独立时间控件、MB/s 单位和参考线。使用 `scripts/update_huawei_runtime_intranet.py` 更新同一看板的最新导出；对应 JSON 为 [cloud-business-runtime-v1.json](../grafana/cloud-business-runtime-v1.json)，维护方法见 [现有看板网络分区](grafana-business-network.md)。原问题看板的历史追加脚本不用于此次运行明细替换。图表查询 `scope=intranet_estimated`，采集器完成差分后输出速率 Gauge，Grafana 不重复相减或调用 `rate()`。

旧镜像不支持新估算范围，回滚时必须同步恢复配置与对应源码/镜像，避免仅把标签改回总量。此次已于 2026-10-27 18:19:52 至 18:20 左右部署到原服务器，采集与通知网关健康，正式日志两轮成功采集 ECS 66、RDS 6 个双向样本。运行明细四图的实际 Prometheus 查询分别返回 33/33/3/3 条新鲜序列；监控库只读 SQL 确认旧总量活动/候选状态及待发/失败消息均为 0。实际备份及回滚命令见 [部署记录](server-update.md#2026-10-27-华为内网估算直接替换)。

## 官方来源

- [阿里云官方控制台指标配置](https://g.alicdn.com/cloudmonitor/cms-subapp-all/2.12.85/index.js)
- [阿里云指标元数据 API](https://help.aliyun.com/zh/cms/developer-reference/api-cms-2019-01-01-describemetricmetalist)
- [华为云 ECS 基础指标](https://support.huaweicloud.com/usermanual-ecs/ecs_03_1002.html)
- [华为云 RDS MySQL 指标](https://support.huaweicloud.com/usermanual-rds-mysql/rds_06_0001.html)
- [华为云 RDS PostgreSQL 指标](https://support.huaweicloud.com/usermanual-rds-pg/rds_pg_06_0001.html)
- [华为云 RDS 监控延迟](https://support.huaweicloud.com/usermanual-rds-mysql/rds_06_0003.html)
- [华为云 CES 发现权限](https://support.huaweicloud.com/api-ces/ces_03_0048.html)
- [华为云 CES ListMetrics 与缓存](https://support.huaweicloud.com/api-ces/ces_03_0023.html)
- [华为云 ECS 详情列表与名称字段](https://support.huaweicloud.com/api-ecs/zh-cn_topic_0094148850.html)
- [华为云 ECS 旧版策略授权表](https://support.huaweicloud.com/api-ecs/ecs_06_0002.html)
