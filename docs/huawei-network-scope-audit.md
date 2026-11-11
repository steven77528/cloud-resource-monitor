# 华为云 ECS / RDS 内外网流量口径核对

查阅日期：2026-10-27。范围为本项目使用的 ECS 基础监控、RDS for MySQL / PostgreSQL，以及用户提供的弹性公网 IP（EIP）监控说明。先核对公开文档，再经用户授权在服务器运行只读云 API 核验；现场结果见文末。核验本身未改采集配置或启动调度。随后按用户要求实现内网估算替换，并于当日 18:19:52 至 18:20 左右替换原服务器的采集服务与通知网关，实际部署和最终核验结果见 [部署记录](server-update.md#2026-10-27-华为内网估算直接替换)。

## 结论

**公网通道可以通过 EIP 单独监控；ECS / RDS 源指标仍是实例总量，项目现以同窗总量减全部已关联 EIP 同向公网速率，直接替换原华为四图和告警为“内网估算”。** 用户截图中的 `publicip_id` 是 EIP 资源 ID，不是 ECS / RDS 总吞吐指标的内外网筛选项。估算标签不等于云厂商原生、精确的纯内网定义。[1][2][3][4]

| 对象 | 官方源指标与维度 | 能确认的口径 | 能否直接区分内外网 |
| --- | --- | --- | --- |
| ECS 基础监控 | `SYS.ECS`，`network_vm_bandwidth_in` / `network_vm_bandwidth_out`，`instance_id` | 整机每秒接收 / 发送流量，官方明确为公网和内网总和；`Byte/s`，5 分钟 | 这两条指标不能直接拆分 [2] |
| ECS 操作系统监控（Agent） | `AGT.ECS`，网卡接收 / 发送计数 | Linux 来自 `/proc/net/dev`，Windows 来自 WMI 的 `MibIfRow` 对象；按网卡统计 | 安装 Agent 本身不等于获得内网 / 公网分类 [5] |
| RDS for MySQL | `SYS.RDS`，`rds004_bytes_in` / `rds005_bytes_out`；单机、主备用 `rds_cluster_id` | 所有网络适配器的输入 / 输出吞吐；文档单位 `KiB/s`，1 分钟 | 此次查阅的公开指标表未列出内外网分项 [3] |
| RDS for PostgreSQL | 同上指标，`postgresql_cluster_id` | 所有网络适配器的输入 / 输出吞吐；文档单位 `KiB/s`，1 分钟 | 此次查阅的公开指标表未列出内外网分项 [4] |
| EIP | `SYS.VPC`，`downstream_bandwidth` / `upstream_bandwidth`，`publicip_id` 或 `bandwidth_id` | 入云 / 出云的公网通道带宽；`bit/s`，1 分钟 | 可以独立观察对应 EIP 的公网入站 / 出站；不提供实例纯内网值 [1] |

MySQL 集群版的指标层级为 `rds_cluster_id,rds_instance_id`，其中后一项是节点 ID；它仍然是资源层级，不是内外网分类维度。[3]

## 用户截图对应的 EIP 指标

用户链接：[弹性公网 IP 支持的监控指标](https://support.huaweicloud.com/usermanual-eip/monitor_0002.html)。该页命名空间是 `SYS.VPC`，不是 `SYS.ECS` 或 `SYS.RDS`。[1]

| 需求 | 指标 ID | 官方说明 | 单位 |
| --- | --- | --- | --- |
| 公网入站速率 | `downstream_bandwidth` | 入网带宽，流入云平台的网络速度 | `bit/s` |
| 公网出站速率 | `upstream_bandwidth` | 出网带宽，流出云平台的网络速度 | `bit/s` |
| 入站数据量 | `down_stream` | 入网流量 | `Byte` |
| 出站数据量 | `up_stream` | 出网流量 | `Byte` |

以上均为 1 分钟原始监控周期。速率与数据量是不同指标，不能把 `Byte` 序列直接当作 `bit/s` 展示；文档对带宽采用 1000（SI）进制。[1]

- `publicip_id`：弹性公网 IP ID，通过 EIP 详情 API 获取。它不是公网 IP 地址字符串，也不是 ECS / RDS 实例 ID。[1][6]
- `bandwidth_id`：带宽 ID，通过带宽 API 获取。定位单个公网 IP 时，应使用支持该维度的 `publicip_id` 序列，不能直接把共享带宽整体值归到一台实例。[1][6]

## 如何关联 ECS / RDS

EIP 详情 API `GET /v1/{project_id}/publicips/{publicip_id}` 返回 `publicip.id`、`public_ip_address`、绑定时的 `port_id`、`private_ip_address`、`bandwidth_id` 等信息。需要以相同地域、项目中的真实绑定关系关联实例和 EIP，不能仅凭名称推断。[6]

RDS 官方还提供 `AttachEip` 接口，说明实例创建成功后可绑定 EIP，从公共网络访问数据库。接口路径为 `PUT /v3/{project_id}/instances/{instance_id}/public-ip`，绑定参数同时包含 `public_ip`（地址）和 `public_ip_id`（EIP 的 UUID）。这确认 RDS 实例与 EIP 的绑定关系是产品支持的能力；本次仅阅读该接口文档，未调用写接口。[7]

RDS `ListInstances` 的 `public_ips` 是“实例外网 IP 地址列表”，不是 EIP UUID；因此不能直接将该字段值放进 CES 的 `publicip_id`。需要再关联 EIP 资源信息。[8]

当前实现按同地域、同项目完整查询 ECS/RDS 与 EIP 列表，核对公网地址和绑定端口，拒绝重复候选、端口冲突、不完整公网地址覆盖和未绑定 EIP。采集所有已关联 EIP 的 `publicip_id` 双向速率，按实例聚合后相减。关联结果默认缓存 300 秒，即使任务使用显式实例清单，也需要这一查询。现场已验证的 36 台实例关系不代表未来所有实例天然具备有效 EIP 关联。

## 不能直接得到纯内网的原因

以下属于根据官方统计口径作出的工程判断，而不是官方承诺的计算公式：

- ECS 总网卡与 EIP 的测量位置不同，原始周期分别为 5 分钟和 1 分钟；直接相减最新点没有同一时间窗的保证。[1][2]
- 如果实例通过 NAT 网关、负载均衡或其他转发资源访问公网，某个 EIP 可能代表共享出口 / 入口，不能自动归属于单台 ECS / RDS。必须先核对实际网络拓扑和绑定关系；实例没有直接绑定 EIP，也不能据此推断没有公网流量。
- 共享带宽和共享公网出口是不同概念。共享带宽整体值不能直接代表单台实例；按 `publicip_id` 获取的单 EIP 值也只覆盖经过该 EIP 的流量。[1][6]
- RDS 指标统计的是所有网络适配器总吞吐，不能自动解释为业务 SQL 流量，更不能直接还原某台 ECS 到某台 RDS 的通信量。[3][4]

因此，不能把“实例总吞吐减 EIP 公网吞吐”直接标为精确内网指标。即使经过周期对齐和完整公网路径核验，也只能先作为估算进行验证；需要按通信对象或网络路径精确区分时，应另评估流日志或专门的网络观测方案。若选择 VPC 流日志，需先核对目标资源 / 网卡是否支持，再按源、目的 IP 等字段分析，不能假定托管 RDS 网卡可无条件开启。

还需避免一个术语误读：ECS 文档中的“带内”表示在云服务器内部统计，“带外”表示在虚拟化层统计，**不是“内网”和“外网”**。[2]

## 当前项目实现

`config/network-traffic.yaml` 的原 `huawei-ecs-network`、`huawei-rds-network` 直接使用 `scope: intranet_estimated`；原四条网络业务规则也改为匹配此范围，阈值数值与持续时间不变。原总量规则不并行保留，旧任务、规则 ID 延用，账号和通知开关不变。采集器 `cloud_monitor/providers/huawei_network_scope.py` 负责资源关联、单位校验和差分；输出仍是 `network_in_bps` / `network_out_bps`、单位 `bit/s`，标注 `source=total_minus_eip`。

所有源均使用 `period=300`、`filter=average`，排除当前未闭合窗口；总入站、总出站及每一个已关联 EIP 的两个方向必须具有严格相同的源时间戳，才成对输出一个实例的估算。RDS 原始分钟指标也以相同 5 分钟平均窗口查询，默认回看 1800 秒、源数据最大年龄 900 秒。缓存内观察到 EIP 绑定变化后，等待新绑定下完整闭合窗口，不用新映射回算旧点。

缺少或冲突的关联、清单不完整、无公共时间戳、未知单位、陈旧数据、负差等情况不输出该实例的任一方向；不补零、不将负差静默截断。2026-10-30 已部署版本仅将有限负差单列为 `degraded`（估算不可用），不再因此触发 `collection-error`；API、非法字段、非有限值、缺失/陈旧/不对齐数据和映射错误仍为 `error`。任务 `ok` 或 `degraded` 时可导出其他仍新鲜且可用的资源；真实错误或任务陈旧时隐藏整项估算任务当前值。NAT、负载均衡等未经过已关联 EIP 的公网路径无法由该计算排除，仍保留前述估算边界。

负差的无效源时间按任务、地域、资源持久化，只有同一资源取得严格更新的有效双向配对点才确认恢复，旧点或其他资源成功不能解除。不可用期间保留业务活动告警，并中断阈值候选和业务恢复连续性。Prometheus 用 `cloud_monitor_network_estimate_available` 标示已有有效配对点或负差记录资源的估算可用性，通用 Grafana 文件提供独立可用性表；`/readyz` 可接受 `degraded`，仍拒绝真实错误。当前保留既有数据源、300 秒平均、源时间和阈值，不将改变采样或均值当作已证明修复；实测原因与反例见 [负差专项核查](huawei-network-negative-difference-research.md)，完整处理见 [估算可用性](network-traffic.md#估算可用性)。采集质量处理已部署；线上现用看板尚未应用新增表，详见 [上线记录](server-update.md#2026-10-30-华为内网估算质量处理)。

服务启动应用新配置时清理旧 `scope=all` 业务活动状态、持续候选以及待发送/失败重试消息，包括待发送恢复消息；不生成迁移恢复通知。任务采集健康告警、其他任务、已发送消息和历史样本保留，新内网告警从新源样本累计。原华为四图在 **云业务与开发 · 运行明细**（`cloud-business-runtime-v1`）替换为内网估算，保留面板 ID、MB/s 单位及独立时间控件，详见 [网络分区维护](grafana-business-network.md)。

新口径已部署到原服务器，候选诊断及正式日志两轮均取得 ECS 66、RDS 6 个双向样本，容器健康。指标导出共 72 条估算序列；运行明细四图的实际 Prometheus 查询分别返回 33/33/3/3 条新鲜序列，监控库只读 SQL 确认旧总量告警/候选状态和待发送/失败重试消息均为 0。部署详情和回滚见 [部署记录](server-update.md#2026-10-27-华为内网估算直接替换)。阿里云 ECS 继续使用原生内网指标，阿里云 RDS 仍明确显示总量；独立核验及权限边界见 [阿里云 RDS 内外网核对](aliyun-rds-network-scope-audit.md)。

## 2026-10-27 服务器只读核查

经用户授权，通过密码弹框 SSH 登录 `192.168.120.28`，在
`/data/monitor/cloud-resource-monitor` 的现有 `cloud-monitor` 容器中读取配置并调用查询 API。
凭据仅在容器进程内使用，未输出或保存到报告；未重启容器、写监控数据库或发送通知。

| 核查项目 | 实测结果 |
| --- | --- |
| 启用的华为云账号 / 地域 | `huawei-hycen` / `cn-east-3` |
| ECS 详情查询 | 返回 33 台，总数 33；全部返回公网地址 |
| RDS 实例查询 | 返回 3 台，总数 3；全部返回 `public_ips` |
| CES EIP 指标发现 | `SYS.VPC/upstream_bandwidth` 返回 76 条，其中 38 条具有 `publicip_id` 维度，其余为带宽维度 |
| EIP 列表查询 | 首轮 `403 / VPC.2201`；用户补充权限后复查 `200`，返回 38 个 EIP，清单完整 |
| 实例与 EIP UUID 关联 | 36 个 EIP 按公网地址唯一匹配到全部 33 台 ECS、3 台 RDS；无重复候选，另 2 个 EIP 未匹配到这 36 台实例 |
| 同窗总量和公网双向数据 | 抽查 3 台 ECS 和全部 3 台 RDS，12 个方向序列均成功；同一源时间戳共 154 个匹配点 |
| API 查询结果 | 3 类资源清单和 24 次 CES 查询，共 27 个 GET，全部 `200` |

用户已给采集用户追加 [EIP 列表最小补充策略](policies/huawei-eip-discovery.json)，
`vpc:publicIps:list` 在 `cn-east-3` IAM 区域项目实测生效。
原 BSS、RDS、CES、ECS 授权及现有开关继续保留，本次核查未发现需追加其他查询权限。
EIP 列表可提供 UUID、公网地址和绑定端口，无需为常规关联额外授予 VPC 端口查询权限。
操作说明见 [EIP 补充查询权限](huawei-permissions.md#eip-关联的补充查询权限)。

复查窗口为 **2026-10-27 16:45–17:45（北京时间）**，所有序列统一使用
`period=300`、`filter=average`，按数据点实际单位换算为 `bit/s`，只比较完全相同的源时间戳。
每个方向取得 12 或 13 个匹配点，本窗口内 154 个差值均非负。最新示例：

| 实例、方向 | 源时间（北京时间） | 总速率 | EIP 公网速率 | 差值，仅为内网估算 |
| --- | --- | --- | --- | --- |
| `YJ-PMS` 入站 | 17:40 | 43,159,018.8 bit/s | 237.71 bit/s | 43,158,781.09 bit/s |
| `rds-PMS` 出站 | 17:45 | 354,009,759.52 bit/s | 0 bit/s | 354,009,759.52 bit/s |

差值非负不等于已经证明纯内网口径；测量位置与公网路径覆盖范围仍有前述边界。
本次关联结论依据同地域、同项目的公网地址唯一匹配，不宣称已经逐项审计所有端口与网络路径。
后续实现按用户要求直接替换华为原图和原告警，并以“内网估算”说明口径；该决定不改变本次现场证据的边界。缺失、不匹配、陈旧和负差不能伪造为零。

可复用脚本位于 [probe_huawei_network_scope.py](../scripts/probe_huawei_network_scope.py)。
它仅通过允许的 GET 路径查询启用华为账号及网络任务涉及的地域，不实例化业务服务或连接数据库。
本次通过 SSH 标准输入交给已有容器的 Python 临时执行，未向服务器安装新包或写入脚本。
脚本输出资源名称、公网地址、资源 UUID 和已脱敏错误标识，不包含密钥、配置原文或请求头。
本地后续复审还收紧了端口和地址相互冲突时的关联判断，并保留端口字段供复查；
上述现场统计来自收紧前的公网地址唯一匹配报告，不用该改动追溯宣称额外端口校验已通过。

差分公式必须按网络范围在同一方向内计算：
`内网入站估算 = 总入站 - EIP 公网入站`，
`内网出站估算 = 总出站 - EIP 公网出站`。
入站与出站是独立方向，不能互相相减得到另一方向；时间窗、单位和测量覆盖范围仍需校验。

## 官方来源

下列官方文档均于 2026-10-27 使用普通浏览器读取正文；命令行直接请求曾返回安全验证页，未将该拦截页作为正文证据。

1. [EIP：支持的监控指标](https://support.huaweicloud.com/usermanual-eip/monitor_0002.html)：`SYS.VPC`，公网带宽 / 流量指标，`publicip_id` / `bandwidth_id` 维度。
2. [ECS：支持的基础监控指标](https://support.huaweicloud.com/usermanual-ecs/ecs_03_1002.html)：整机指标明确包含公网与内网；带内、带外的统计位置定义。
3. [RDS for MySQL：支持的监控指标](https://support.huaweicloud.com/usermanual-rds-mysql/rds_06_0001.html)：所有网络适配器吞吐、原始周期和实例 / 节点维度。
4. [RDS for PostgreSQL：支持的监控指标](https://support.huaweicloud.com/usermanual-rds-pg/rds_pg_06_0001.html)：所有网络适配器吞吐与 `postgresql_cluster_id`。
5. [ECS：操作系统监控指标（安装 Agent）](https://support.huaweicloud.com/usermanual-ecs/ecs_03_1003.html)：`AGT.ECS` 网卡计数与操作系统采集方式。
6. [EIP：查询弹性公网 IP](https://support.huaweicloud.com/api-eip/eip_api_0002.html)：资源 UUID、地址、端口、私有地址、带宽与共享类型字段。
7. [RDS：绑定和解绑弹性公网 IP](https://support.huaweicloud.com/api-rds/rds_05_0009.html)：实例与 `public_ip_id` 的绑定能力。
8. [RDS：查询数据库实例列表](https://support.huaweicloud.com/api-rds/rds_01_0004.html)：`public_ips` 是外网 IP 地址列表。
