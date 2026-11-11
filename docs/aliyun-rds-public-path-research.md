# 阿里云 RDS 公网路径与独立流量来源补充研究

核查日期：2026-10-27。范围为标准 RDS for MySQL 的公网地址、数据库代理、用户 EIP 和 IPv4 网关能力。仅查阅官方公开文档，未 SSH、未调用业务数据库、未修改云资源。本笔记补充现有 `aliyun-rds-network-scope-audit.md`，不重复执行此前已经成功的云资源查询。

## 结论

当前没有从标准 RDS MySQL 公网地址本身获得独立收发流量的新接口证据。**数据库代理也不能直接解决本项目的内外网分项需求：MySQL 代理监控 API 当前仅提供 CPU。** 用户 EIP 确有双向吞吐监控，但此前 5 个 RDS 公网 IP 均没有匹配到当前账号、地域和授权范围内的 EIP，因此尚无可供本项目直接查询的独立公网序列。

这不等于“没有 EIP 就无法测流量”，也不等于“阿里云所有产品都没有分项”。如果将全部公网数据库连接改为经过可测量的自有入口，可以从入口观测公网流量，但这是网络或客户端连接改造，需要验证覆盖范围和统计口径，不能把新增入口的指标直接当作现有 RDS 公网地址的历史指标。

## 1. 标准 RDS MySQL 数据库代理：有内外网地址，没有对应的吞吐监控接口

官方[查看代理监控数据](https://help.aliyun.com/zh/rds/apsaradb-rds-for-mysql/view-the-proxy-monitoring-data-of-an-apsaradb-rds-for-mysql-instance)说明，控制台查看的是 CPU 利用率，并明确：

> DescribeDBProxyPerformance 查询数据库代理的性能数据，当前仅支持查看 RDS MySQL 代理实例的 CPU 利用率。

官方 [DescribeDBProxyPerformance](https://help.aliyun.com/zh/rds/api-query-performance-metrics-of-a-dedicated-proxy) 的 `MetricsName` 进一步区分引擎：

| 引擎 | 文档列出的指标 | 对当前 MySQL 需求的意义 |
| --- | --- | --- |
| RDS MySQL | 仅 `Maxscale_CpuUsage` | 不能通过该 API 获取代理收发流量 |
| RDS PostgreSQL | 包含 `Maxscale_DownFlows`、`Maxscale_UpFlows`，另有连接、QPS、内存、CPU | 不能将 PostgreSQL 指标套到 MySQL；文档也没有说这两个指标天然按内外网分类 |

该 API 支持 `Dimension=service`（代理连接地址）、`node`（代理节点）、`server`（数据库节点）等聚合维度，但 **维度存在不代表 MySQL 支持流量指标**。`rds:DescribeDBProxyPerformance` 是文档确认的只读动作，但目前没有理由为了本项目流量拆分再申请它。

官方[管理代理连接地址](https://help.aliyun.com/zh/rds/apsaradb-rds-for-mysql/manage-the-dedicated-proxy-endpoints-of-an-apsaradb-rds-for-mysql-instance)说明，单个实例支持 1~7 个代理连接地址，每个支持 1 个内网地址和 1 个外网地址。这说明连接地址可以分类，并不能补足吞吐指标。新开数据库代理本身不足以证明能得到内外网速率。

## 2. 用户 EIP：可以测，但需要存在实际承载目标流量的 EIP 资源

官方 [EIP 监控与报警](https://help.aliyun.com/zh/eip/monitoring-and-alerting)明确提供：

- 流出带宽：EIP 实例访问外部所消耗的带宽，单位 `bps`。
- 流入带宽：外部访问 EIP 实例所消耗的带宽，单位 `bps`。
- CloudMonitor API：`DescribeMetricList`。
- 自定义最小读取动作：`cms:QueryMetricList`；也可使用相关 CloudMonitor 只读系统策略。
- 另有基于日志服务的秒级监控，属于额外开通能力，涉及日志存储和可能的费用；其秒级带宽单位为 `Bps`，不能与基础监控的 `bps` 直接混用。

该文档要求 EIP 先绑定云资源才产生流量数据。EIP 监控统计的是该 EIP 实际承载的流量，并非一个能够输入任意公网 IP 查询流量的通用接口。

此前本项目精确查询没有匹配到 5 个 RDS 公网 IP 的用户可见 EIP。不能通过直接把公网 IP 当成 `AllocationId` 查询监控，也没有依据继续增加同类 EIP 读取权限就能找到服务托管的监控资源。

## 3. IPv4 网关与按 IP 绑定 EIP：通用网络能力，尚未确认适用标准 RDS 托管地址

官方 [AssociateEipAddress](https://help.aliyun.com/zh/vpc/developer-reference/api-vpc-2016-04-28-associateeipaddress)支持 `InstanceType=IpAddress`，此时需指定已启用 IPv4 网关且与 EIP 同地域的 `VpcId`。接口没有 `RdsInstance` 枚举；通用 IP 类型的存在本身不能证明支持将 EIP 绑定到标准托管 RDS 的内网地址。

官方 [IPv4 网关](https://help.aliyun.com/zh/vpc/user-guide/ipv4-gateway-overview)将它定义为公网流量控制组件，明确：

- IPv4 网关自身不提供公网访问能力，只控制公网流量；EIP 等公网资源提供公网访问能力。
- 激活会切换 VPC 的流量路径，可能发生短暂网络中断，需要先规划并配置路由。
- 文档示例和具体说明以 ECS、公网 NAT、CLB 等为主；此次查阅没有确认标准 RDS 公网连接地址的绑定或统计支持。

因此不能把“创建 IPv4 网关再按 IP 绑 EIP”列为已验证的 RDS 监控方案。其可用性应先向阿里云核实标准 RDS 的服务限制和受支持流量路径，且即便支持也属于网络改造，而非补一个读取权限。

## 4. 可控入口方案：工程上可测量，需要改造实际连接路径

工程上可以采用“公网客户端 → 自有 EIP / 自有 TCP 数据库代理 → RDS 内网地址”的架构，并在专用入口记录双向字节数。EIP 吞吐可作为网络侧参考，自有代理也可以按数据库实例和连接统计字节。此处是基于可控入口的工程方案，并非声称阿里云标准 RDS 提供原生公网指标。

成立条件：

1. 所有需要统计的公网连接都经过入口；仍直接使用 RDS 原公网域名的连接不会被入口统计覆盖。
2. 入口需按 RDS 实例隔离或能逐连接映射；共用 EIP 的总带宽会包含其他业务。
3. 代理入口的入/出方向与 RDS 的入/出方向一致，需按数据流校验；代理到 RDS 的后端流量本身走内网，业务口径应按客户端访问路径分类，不能简单用网卡名分类。
4. EIP 是网络侧字节统计，RDS 监控可能使用数据库或实例侧统计。TLS、TCP/IP 开销、重传、代理控制流量和统计边界均可能造成差异；若要“总量减公网”，必须先校准单位、时间窗、统计值和范围。
5. 新入口涉及连接地址、网络安全、可用性和成本。此项不是对当前部署可以仅通过读取 API 立即启用的修复。

现阶段应优先验证其他无需改变业务连接路径的观测能力，例如适用当前实例的 VPC 流日志或 DAS 指标；本笔记不将尚未核实的 NAT、Cloud Firewall 或 DDoS 指标包装为现有 RDS 的独立流量来源。

## 本次核查的边界

- 没有因为用户 EIP 查询为空而断言阿里云底层不存在公网资源。
- 没有将标准 RDS MySQL、PostgreSQL、数据库代理、ECS 自建数据库或其他产品的能力混为一谈。
- 尚未取得标准 RDS MySQL 按公网 / 内网分别统计的新增可用指标；不应据此改动现有采集或告警的流量口径。
