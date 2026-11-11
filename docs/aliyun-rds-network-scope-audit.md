# 阿里云 RDS 内外网流量口径核对

核查日期：2026-10-27。范围为本项目使用的阿里云 RDS for MySQL 监控，以及当前启用账号在 `cn-hangzhou` 的云监控指标元数据。公开文档用于确认接口语义，现有采集账号的只读 API 核查用于确认当前可用指标。本次未修改云资源或调用业务数据库。

## 结论

**阿里云 RDS 可以区分内网、外网连接地址；当前项目使用的网络吞吐指标没有内外网分项，不能像阿里云 ECS 一样直接获取原生内网入站、出站速率。**

2026-10-27 现场只读核验已完成：两个启用账号共 7 台 RDS 的连接地址查询全部成功；其中 5 个公网地址的 EIP 精确查询也全部成功，但均未匹配当前账号、地域及授权可见范围内的 EIP。当前采集链路没有可用于“总量减公网”的独立公网吞吐来源，阿里云 RDS 继续展示总量，保持 `scope=all`。这不是在断言阿里云产品底层没有使用 EIP 或服务托管的公网资源。

| 对象 | 当前数据来源 | 已确认口径 | 运行明细的展示口径 |
| --- | --- | --- | --- |
| 阿里云 ECS | `IntranetInRate` / `IntranetOutRate` | 原生内网入站 / 出站速率 | 内网 |
| 阿里云 RDS for MySQL | `acs_rds_dashboard` 下 `MySQL_NetworkInNew` / `MySQL_NetworkOutNew`，`instanceId` | 实例网络入站 / 出站；此次官方定义和当前账号元数据均未提供内外网分类 | 总量；保持 `scope=all` |
| RDS 连接地址 | `DescribeDBInstanceNetInfo` 的 `IPType` | `Inner` / `Public`，或 VPC 下 `Private` / `Public` | 仅表示地址类型，不是流量测量结果 |

“未提供内外网分项”限于此次核查的官方接口和当前账号指标元数据，不代表阿里云所有产品、版本及额外网络观测服务都不存在其他方案。

## 补充研究：还有哪些获取办法

2026-10-27 继续核查了 RDS/DAS、数据库代理、VPC 流日志、NIS、流量镜像、云防火墙、EIP 和 IPv4 网关。**存在网络层间接统计办法；对当前 7 台标准 RDS MySQL，尚未确认可直接读取现有原生公网端点双向吞吐、或原生内网双向吞吐的新增云 API。** 本轮仅研究公开资料，未开通付费服务、改变连接地址或路由，也未修改现有采集和告警。

| 方法 | 能取得什么 | 对当前项目的适用条件 |
| --- | --- | --- |
| RDS 原生性能 / 当前 CMS 指标 | 实例总入站、总出站 | 已在采集；没有发现内外网分项或按公网 IP 筛选吞吐的参数 |
| DAS 会话、SQL 来源 / 审计、性能洞察 | 客户端 IP、端点、请求次数、行数、耗时等 | 已核查的公开响应没有所需双向网络字节字段，不能用请求或行数比例拆分流量 |
| RDS MySQL 数据库代理 | 当前公开监控为代理 CPU | `DescribeDBProxyPerformance` 的 `Maxscale_UpFlows` / `Maxscale_DownFlows` 属于 PostgreSQL 支持项；不能用于现有 MySQL，也不能因有连接地址维度就认为可拆分 |
| 客户端 ECS ENI 的 VPC 流日志 | 按 RDS 私网 IP / 实际端口汇总的双向网络字节 | 可测被覆盖客户端的私网访问；需开通流日志和 SLS，或复用已有日志。全部私网访问路径覆盖前，只能称为被覆盖客户端流量 |
| RDS 所在 VPC / 交换机的流日志 | 受支持关联 ENI 的流量 | 尚未找到普通 RDS 托管侧接口受支持的明确说明，不能承诺直接采集整台 RDS 的全部流量 |
| NIS / 云防火墙 / 流量镜像 | 已采到的流日志分析，或受保护 / 镜像路径的流量 | NIS 不扩大采集范围；云防火墙未确认直接支持原生 RDS 公网端点；镜像不支持托管 ENI 作为源 / 目的 |
| 自有 EIP + 专用 ECS TCP 代理 / 转发入口 | 全部经过该入口的公网访问双向流量 | 工程上可测，但需要公网客户端切换入口、按实例隔离统计，并核验覆盖和字节口径；属于网络改造 |

### 优先验证客户端侧内网流量

若业务通过 ECS 私网访问 RDS，可在客户端 ENI 侧采集 [VPC 流日志](https://help.aliyun.com/zh/vpc/vpc-flow-logs)，使用源 / 目的 IP、端口、方向和 `bytes` 直接统计。这个方案不依赖查询到 RDS 自身的 ENI。

| RDS 视角 | 客户端侧日志条件 | 窗口平均速率 |
| --- | --- | --- |
| 内网入站 | 客户端 ENI `out`，目的为 RDS 私网 IP 和实际数据库端口 | 对完整窗口求 `sum(bytes) * 8 / 窗口秒数`，单位 bit/s |
| 内网出站 | 客户端 ENI `in`，源为 RDS 私网 IP 和实际数据库端口 | 同上 |

需要识别全部访问方，包括跨账号 / VPC、专线 / VPN、只读或代理端点；跟踪 IP / 端口变更，在固定的客户端侧计数并消除重复记录。1 / 5 / 10 分钟是聚合窗口，默认 10 分钟。需检查 `ACCEPT` 和 `log-status`，`SKIPDATA` 表示存在跳过记录，缺失窗口不能补成零。只有覆盖完整且日志质量核验通过后，才能把该业务内网量用于整台实例的展示和告警。

这取得的是客户端到 RDS 的网络层流量。现有 RDS 总量与网络侧 `bytes` 是否同层、是否包含相同协议开销和节点范围，仍需验证；不能直接用两者相减承诺精确公网流量。完整内网量若能直接测得，也不必先计算公网残差。流日志只保留开启后的数据，不能仅靠添加权限还原此前未记录的历史流量。

流日志不是单纯增加一个 RDS 只读权限：如未启用，需要开通流日志、日志服务和投递角色，且存在流日志生成费、SLS 写入 / 存储等费用；每主账号每地域每月的 5 GB 免费额度仅覆盖流日志生成费。采集用户运行时只需读取已选资源和目标 Logstore，不需要业务数据库账号；最小策略应在确定现有 Project / Logstore 和采集范围后按实际资源生成。当前没有理由为此盲目增加 DAS 或数据库代理权限。

### 公网统计与“总量减其中一项”的边界

用户提出的同方向相减在统计范围和口径一致时成立。公网 IP 可以作为流日志 / 代理统计的筛选标识，但它本身不是字节计数器；用户 EIP 只是独立计量点的一种，并非测量公网流量的必要条件。

如未来选用“公网客户端 → 自有 EIP / 专用 ECS TCP 代理 → RDS 内网”，必须让全部目标公网连接实际经过入口，按实例归属流量，并校准时间窗、单位、统计值和计量层次。入口到 RDS 的后端连接走私网，仍需按原始客户端路径区分业务内外网，避免把公网访问的代理后端量误认成内部业务访问。该方案只计量新入口实际经过的流量，不会补出原生公网地址的历史吞吐。

如希望完全保留当前访问架构，下一项需要阿里云明确确认的是：具体 RDS 实例的托管接口是否支持 VPC 流日志；或是否有公开文档之外、可授权读取的公网端点吞吐指标。需要得到命名空间、指标名、单位、方向、端点 / 网络类型维度、实例及节点覆盖、周期和权限动作后，才能实施取数。不能把通用 `IpAddress` EIP 绑定能力直接视为标准 RDS 支持。

详细官方证据与已排除路径：

- [DAS、审计与 MySQL 数据库代理](aliyun-rds-das-traffic-research.md)。
- [VPC 流日志、NIS、镜像与云防火墙](aliyun-rds-flowlogs-research.md)。
- [原生公网端点、EIP、IPv4 网关及可控入口](aliyun-rds-public-path-research.md)。

## 2026-10-27 只读元数据核查

对两个启用的阿里云采集账号，在 `cn-hangzhou` 调用 `DescribeMetricMetaList`，命名空间为 `acs_rds_dashboard`，遍历全部分页后按 `net` / `traffic` 过滤。两个账号均仅发现下列网络相关指标族，未发现 `Intranet`、`Internet` 或内外网分项：

- `MySQL_NetworkInNew` / `MySQL_NetworkOutNew`。
- `Cluster_NetworkInNew` / `Cluster_NetworkOutNew`，包含 `nodeId` 维度。
- `PG_NetworkRx` / `PG_NetworkTx`。
- `SQLServer_NetworkInNew` / `SQLServer_NetworkOutNew`，以及 `NetworkRead` / `NetworkWrite` 的集群从节点变体。

MySQL 两条指标返回单位 `bit/s`，维度为 `userId,instanceId`，周期为 `60,300` 秒，支持统计值 `Average,Min,Max`。元数据未提供按公网 / 内网选择的维度。该结果是当前账号实际可用指标的证据；本次未将凭据、签名或临时令牌写入文档。

## 2026-10-27 现场 RDS 与 EIP 交叉核查

使用现有采集 IAM 用户，在 `cn-hangzhou` 对启用的 RDS 网络作业执行只读探测：先读取全部连接地址，再将 `IPType=Public` 的公网 IP 作为 `DescribeEipAddresses` 的 `EipAddress` 筛选条件，检查匹配资源。

| 账号 | RDS 实例数 | 连接地址查询成功 | 公网 IP 数 | EIP 精确查询成功 | 匹配 EIP 数 | 未返回公网端点的实例数 |
| --- | --- | --- | --- | --- | --- | --- |
| `aliyun-hycen` | 2 | 2 / 2 | 2 | 2 / 2 | 0 | 0 |
| `aliyun-yuanzhu` | 5 | 5 / 5 | 3 | 3 / 3 | 0 | 2 |
| 合计 | 7 | 7 / 7 | 5 | 5 / 5 | 0 | 2 |

最终探测结果为 `status=complete_in_current_scope`，`required_actions=[]`。慧橙账号所需只读权限补齐后，两个账号的目标查询均已成功，当前核验不需要继续新增权限。

5 个公网地址的结果均为 `no_match_in_visible_scope`，表示当前账号、地域和授权可见范围中没有匹配的 EIP 资源，不能解释为平台底层没有公网地址、没有 EIP 或未使用服务托管资源。元竹另外 2 台实例的结果为 `no_public_endpoint_returned`，仅说明此次地址接口未返回公网端点，也不能把所有潜在公网路径的流量自动视为零。

由于未取得可与这 5 个公网地址关联的用户可见 EIP 资源和独立公网流量序列，此次无法执行“RDS 总量减 EIP 公网”推算内网流量；结合原生内网指标未发现的结果，当前阿里云 RDS 保持总量口径。成功空结果与权限拒绝已明确区分，不能通过继续盲目增加同类只读权限来保证得到流量分项。

## 官方接口与文档证据

### 1. RDS 性能 API 提供实例收发流量

[`DescribeDBInstancePerformance`](https://help.aliyun.com/zh/rds/developer-reference/api-rds-2014-08-15-describedbinstanceperformance) 的请求参数包括 `DBInstanceId`、`Key`、`StartTime`、`EndTime`，以及可选的 `NodeId`。接口没有内网 / 公网筛选参数。

官方[性能参数表](https://help.aliyun.com/zh/rds/developer-reference/performance-parameters/)将 `MySQL_NetworkTraffic` 定义为：

> 实例平均每秒钟的输入流量，实例平均每秒钟的输出流量。单位：KB/秒。

API 示例使用 `Key=MySQL_NetworkTraffic`，返回值格式 `recv_k&sent_k`。这能确认输入 / 输出两个方向，不能据此区分内网 / 外网。该接口与本项目的 CMS 指标是不同接口，单位也不同，不能把 `KB/秒` 直接当成 CMS 的 `bit/s`。

官方[查看监控指标](https://help.aliyun.com/zh/rds/apsaradb-rds-for-mysql/view-the-metrics-of-an-apsaradb-rds-for-mysql-instance)指南将网络流量列为实例基础监控项，未在本次查阅的定义中提供内外网吞吐分项。

### 2. 地址 API 可以区分连接地址类型

[`DescribeDBInstanceNetInfo`](https://help.aliyun.com/zh/rds/developer-reference/api-rds-2014-08-15-describedbinstancenetinfo) 返回 `ConnectionString`、`IPAddress`、`IPType`、`Port`、`VPCId`、`VSwitchId` 等字段。官方说明中，经典网络地址类型为 `Inner` / `Public`，VPC 地址类型为 `Private` / `Public`。

这回答的是“该地址通过哪种网络访问实例”，没有返回该地址的收发字节数或速率。此次查阅的返回结构也未给出可直接用来查询用户 EIP 监控的 `AllocationId`；因此不能直接照搬华为云 RDS 到 EIP 的关联方式。

官方[查看和修改内外网地址与端口](https://help.aliyun.com/zh/rds/apsaradb-rds-for-mysql/view-and-change-the-internal-and-public-endpoints-and-port-numbers-of-an-apsaradb-rds-for-mysql-instance)文档也将内网地址、外网地址定义为两种访问路径，没有将其描述为两组监控序列。

官方[申请或释放外网地址](https://help.aliyun.com/zh/rds/apsaradb-rds-for-mysql/apply-for-or-release-a-public-endpoint-for-an-apsaradb-rds-for-mysql-instance)文档说明，申请外网地址以及通过该地址连接产生的公网出、入流量均不收费。该计费说明确认公网通信的存在，并不提供公网流量指标或可减去的公网数值。

## 对本项目的影响

1. 阿里云 ECS 保持使用已有原生内网指标。
2. 阿里云 RDS 继续保留 `network_in_bps` / `network_out_bps` 的 `scope=all`。运行明细若仅展示内网，不应将这两条总量序列改名后混入；应明确其总量口径或显示内网暂不可用。
3. 仅补充 `rds:DescribeDBInstanceNetInfo` 权限，最多能获得内外网地址信息，不能产生独立流量序列；此次未发现可以通过增加一项 RDS 只读权限解锁的原生内网速率指标。
4. 如后续找到可独立测量公网吞吐的已验证来源，可在方向、实例范围、单位、采样周期、统计方法和时间戳一致后，评估“总量减公网”的内网估算。应保留估算标签；缺失值不能补零，负差不能静默归零。仅凭公网地址存在、地址类型或免费计费规则，不能执行该推导。

## 补充核查：RDS 公网地址是否就是用户 EIP

### 官方可确认的资源关系

**RDS 的公网连接地址不能直接等同于当前账号可管理、可查询监控的 EIP。** 本次已通过两项只读接口，按公网 IP 完成当前两个账号的交叉查询；5 个公网 IP 均未匹配用户当前可见的 EIP，详见上面的现场结果。

官方 [`AllocateInstancePublicConnection`](https://help.aliyun.com/zh/rds/apsaradb-rds-for-mysql/api-rds-2014-08-15-allocateinstancepublicconnection-mysql) 用于申请 RDS 外网连接地址。主要请求参数为 `DBInstanceId`、`ConnectionStringPrefix`、`Port`，另有 PostgreSQL 及专属集群相关可选参数。文档没有 `AllocationId`、选择已有 EIP 或绑定用户 EIP 的参数；响应返回连接域名和实例 ID。该接口为写操作，本次仅阅读定义，不应为了监控而调用它。

上述[公网地址指南](https://help.aliyun.com/zh/rds/apsaradb-rds-for-mysql/apply-for-or-release-a-public-endpoint-for-an-apsaradb-rds-for-mysql-instance)进一步明确：

> 连接地址可以设置为固定 IP 地址吗？不可以设置为固定 IP。实例主备切换或配置变更可能导致 IP 变化，建议使用连接地址以减少对业务的影响。

> 外网连接地址由系统分配后保持稳定，仅在您主动释放或重新申请外网地址时才会变更。

这里“连接地址”是域名；不能把域名稳定解读为底层 IP 或 EIP 资源 ID 固定。

官方 [`AssociateEipAddress`](https://help.aliyun.com/zh/vpc/developer-reference/api-vpc-2016-04-28-associateeipaddress) 的 `InstanceType` 枚举为 `EcsInstance`、`SlbInstance`、`Nat`、`HaVip`、`NetworkInterface`、`IpAddress`，没有 `RdsInstance`。因此此次查阅没有确认到“将用户现有 EIP 直接绑定标准 RDS MySQL 实例”的公开支持路径。

当前文档另有 `IpAddress` 绑定类型，要求指定启用了 IPv4 网关的 `VpcId`。这一通用 IP 绑定能力不能仅凭类型名称就认定支持托管 RDS 的地址、适合现有 RDS 网络，或等同于 RDS 原生公网端点；需要独立核实具体产品限制和实际路由。为取得监控而创建网关、变更路由或新增 EIP，不属于本次只读核验。

### MySQL 高可用版本的证据边界

本次读取的 RDS MySQL 公网申请指南和申请 API 未给出按 MySQL 高可用系列版本开放“选择用户 EIP”的差异，也没有发现某一常见引擎版本可通过上述 RDS API 传入 EIP ID 的例外。不能据此编造一个按 MySQL 5.6、5.7、8.0 等版本划分的 EIP 支持矩阵，也不能将其他产品（例如 ECS 自建 MySQL、RDS Custom、数据库代理或独立 NAT 路径）的能力推广到标准 RDS 高可用实例。

后续对其他实例复核时，可使用已有实例清单或 `DescribeDBInstanceAttribute` 信息记录引擎版本、实例系列和网络类型，再执行下面的只读探测。此次账号实测只适用于所核查的实例及当前地域；产品底层是否使用服务托管的公网资源，不能由用户清单中无匹配记录来否定。

## 最小只读探测建议

### 权限与接口

| 操作 | API 版本 | RAM 动作 | 用途与必要返回字段 |
| --- | --- | --- | --- |
| `DescribeDBInstanceNetInfo` | RDS `2014-08-15` | `rds:DescribeDBInstanceNetInfo` | 按实例查询连接地址；仅保留 `ConnectionStringType`、`IPType`、`IPAddress`、`VPCId`、`VSwitchId`、`Port` |
| `DescribeEipAddresses` | VPC `2016-04-28` | `vpc:DescribeEipAddresses` | 用 `EipAddress` 精确筛选；读取 `AllocationId`、`IpAddress`、`InstanceId`、`InstanceType`、`InstanceRegionId`、`ServiceManaged`、`Status` |

这两个动作是此轮关联探测所需的最小动作集合，不需要新增 RDS 业务数据库账号、申请公网连接地址或 EIP 写权限。若现有策略已包含其中一项，仅补缺失项；保留现有权限和启用状态。

可复用策略为 [`policies/aliyun-rds-eip-discovery.json`](policies/aliyun-rds-eip-discovery.json)，仅列出这两个动作，使用 `Resource: "*"` 便于发现当前账号中的资源。当前两个账号的现场查询已经成功，无需再次添加。其他账号如需收紧资源范围，可参考下面的 ARN；收紧后，成功空结果仍只覆盖授权可见范围。

官方授权表分别支持 RDS 实例资源和 EIP 资源级授权，可按已知账号、地域与实例范围约束：

- RDS：`acs:rds:<regionId>:<accountId>:dbinstance/<dbinstanceId>`，核查多实例时可使用对应地域的 `dbinstance/*`。
- EIP：`acs:vpc:<regionId>:<accountId>:eip/*`。

[`DescribeEipAddresses`](https://help.aliyun.com/zh/vpc/developer-reference/api-vpc-2016-04-28-describeeipaddresses) 官方定义确认：`RegionId` 必填；`EipAddress` 为可选公网 IP 筛选条件，一次最多 50 个 IP，以半角逗号分隔；`PageNumber` 默认 1，`PageSize` 最大 100。不要把 RDS 实例 ID 直接填入 `AllocationId`，也不要传入不存在于枚举中的 `AssociatedInstanceType=RdsInstance`。

### 探测顺序及判定

1. 对当前账号、地域中的 RDS 实例调用 `DescribeDBInstanceNetInfo`。筛选 `IPType=Public` 的记录，记录其普通连接或读写分离类型；一个实例可能存在多种连接地址，不能只取第一条。为减少输出，避免记录该接口附带的 `SecurityIPGroups` 白名单内容。
2. 提取公开连接记录的 `IPAddress`。如果缺失或不是可用 IP，可做域名解析用于补充诊断，但需注明取值来自 DNS，不能把 DNS 结果当成已验证的 EIP 绑定关系。没有公网记录表示此次接口未返回该实例的公网端点，不能自动断言所有公网路径流量恒为零。
3. 在相同账号和地域调用 `DescribeEipAddresses(RegionId=..., EipAddress=...)`，不额外指定绑定实例类型或标签。按顺序处理返回分页，比较公网 IP 是否完全相同，保留 EIP 资源 ID 和绑定信息。
4. 若请求因 `403` 等权限错误失败，判定为“未能核查”，不可判定为“没有 EIP”。若请求成功且无匹配记录，仅能确认该账号、地域、当前授权范围下的 EIP 清单没有匹配项；检查资源范围或标签限制后，才能排除权限可见性造成的缺失。
5. 若找到 EIP，仍需核实其绑定资源、是否服务托管、是否共享其他业务路径及该记录与目标 RDS 的实际对应关系。仅公网 IP 相同不能证明 EIP 的全部吞吐都属于该 RDS。
6. 只有资源关系与流量归属确认后，再用已有云监控元数据查询确认该 EIP 可用的网络指标、维度、单位和方向，并取与 RDS 总量相同窗口的双向数据。确认这些条件之前，不执行“总量减公网”的计算。

探测报告建议只持久化计数、脱敏资源标识、匹配状态和指标定义，不保存响应中的凭据、签名或完整访问白名单。本补充内容给出的是可执行的只读验证路径，不代表当前账号已验证存在可直接复用的 RDS EIP 监控。

### 可复用探测命令与生产挂载权限

脚本为 [`scripts/probe_aliyun_rds_eip.py`](../scripts/probe_aliyun_rds_eip.py)。它使用正常配置加载器，仅选择启用的阿里云 RDS `network_traffic` 作业；输出计数、哈希资源标识和状态，不输出 IP、完整连接地址、访问白名单、签名或密钥。

在项目根目录且 Python 环境已安装项目依赖时运行：

```sh
python -m scripts.probe_aliyun_rds_eip --config config/config.yaml
```

当前镜像默认以 `monitor` 用户运行，配置由宿主机只读挂载。生产文件的所有者和权限可能仅允许容器内 root 读取；脚本本身也未包含在现有 Docker 镜像中。此时在生产项目根目录使用一次性容器，以 root 读取现有挂载，并额外只读挂载脚本目录：

```sh
docker compose run --rm --no-deps --user 0 --interactive=false \
  --entrypoint python --volume "$PWD/scripts:/probe:ro" \
  cloud-monitor /probe/probe_aliyun_rds_eip.py --config /app/config/config.yaml
```

这条命令覆盖默认入口执行探测，不启动生产调度器或通知发送，也不需要启动依赖数据库。`--user 0` 仅用于本次诊断容器读取生产挂载文件，不修改现有服务用户、配置权限或云 IAM 权限。

若现有容器已经将该脚本目录只读挂载到 `/probe`，也可执行：

```sh
docker compose exec --user 0 -T cloud-monitor \
  python /probe/probe_aliyun_rds_eip.py --config /app/config/config.yaml
```

现有 Compose 默认没有挂载 `/probe`，因此优先使用前面的一次性容器命令。退出码 `1` 表示配置或查询不完整；退出码 `0` 只表示本轮目标处理完成，需同时检查 JSON 中的 `complete_in_current_scope`、`no_match_in_visible_scope` 等状态，不能将空匹配视为已获得公网零流量。

