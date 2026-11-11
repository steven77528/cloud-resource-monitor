# 阿里云 RDS MySQL：DAS、审计与数据库代理的流量分项核查

核查日期：2026-10-27。范围为阿里云公开官方文档所定义的 RDS for MySQL 云 API；未连接业务数据库、开通付费服务、调整网络路径或调用写接口。本文件补充[已有 RDS 网络口径核查](aliyun-rds-network-scope-audit.md)。

## 结论

此次核查未在 DAS 当前会话、SQL 来源统计、SQL 日志、性能洞察或 RDS MySQL 数据库代理监控的公开接口中，找到可直接用于“总量减公网”的独立公网入站、出站字节数或速率序列。

- DAS 可以按客户端 IP 查看会话和 SQL 请求；公开字段主要是请求次数、耗时、扫描行数、返回行数等，不是网络字节数。
- 部分 SQL 日志还提供连接端点信息，但“有 IP/端点”与“有该 IP/端点的双向字节计数”是两项能力，不能互相替代。
- `DescribeDBProxyPerformance` 确有代理连接地址聚合维度，但是官方明确 RDS MySQL 当前只支持代理 CPU 使用率；文档列出的代理入流量、出流量适用于 RDS PostgreSQL，不能套用于现有 MySQL 实例。
- 这些结论限于下列已核查的公开接口。其他网络产品或用户控制的代理观测路径应另行验证，不能由本文件推定一概不可实现。

## 官方证据

| 路径 | API 与关键字段 | 能否提供本项目所需公网/内网收发字节 |
| --- | --- | --- |
| 当前会话 | [`GetMySQLAllSessionAsync`](https://help.aliyun.com/zh/das/developer-reference/api-das-2020-01-16-getmysqlallsessionasync)：`SessionList.Client`、`ClientStats.Key` 为客户端 IP；`TotalCount`、`ActiveCount` 为会话数；`Time` 为秒 | 公开响应未定义按客户端的入站/出站字节或速率。会话数量不能换算成网络吞吐 |
| 按访问来源统计全量 SQL | [`GetFullRequestOriginStatByInstanceId`](https://help.aliyun.com/zh/das/developer-reference/api-das-2020-01-16-getfullrequestoriginstatbyinstanceid)：`Key`、`OriginHost` 为客户端 IP；`Count` 为总执行次数，`CountRate` 为执行次数比例，另有 `AvgReturnedRows`、`ExaminedRows`、耗时等 | 公开响应未定义网络收发字节。执行次数比例或返回行数比例不能用来拆分总网络吞吐 |
| SQL 审计热数据 | [`GetDasSQLLogHotData`](https://help.aliyun.com/zh/das/developer-reference/api-das-2020-01-16-getdassqlloghotdata)：可按 `HostAddress` 过滤；响应有 `HostAddress`、`SQLText`、`ReturnRows`、`Latancy` 等 | 未公开定义 `BytesSent`/`BytesReceived` 等网络字节字段；SQL 文本大小、审计日志大小、返回行数均不是完整协议收发量 |
| 旧 SQL 日志接口 | [`DescribeSqlLogRecords`](https://help.aliyun.com/zh/das/developer-reference/api-das-2020-01-16-describesqllogrecords)：有 `HostAddress`、`ClientIp`、`Vip`（查询连接的 endpoint 解析地址）、`ReturnRows`、`Consume`；当前 API 目录将此接口列为废弃 | 连接端点信息可以帮助定位请求路径，但公开响应没有对应网络收发字节；不建议新增基于废弃接口的采集实现 |
| SQL 样本 | [`GetFullRequestSampleByInstanceId`](https://help.aliyun.com/zh/das/developer-reference/api-das-2020-01-16-getfullrequestsamplebyinstanceid)：`OriginHost`、`RowsReturned`、`Rt`、`Sql`；最多返回 20 条 SQL 样本 | 样本与行数不覆盖完整连接和全部网络字节 |
| 新版性能洞察 | [`GetPfsMetricTrends`](https://help.aliyun.com/zh/das/developer-reference/api-das-2020-01-16-getpfsmetrictrends)：公开 `Metric` 取值为 `count`、`avgRt`、`rtRate`、`rowsExamined`；[`GetPfsSqlSample`](https://help.aliyun.com/zh/das/developer-reference/api-das-2020-01-16-getpfssqlsample) 返回 `RowsSent` 等 | `RowsSent` 在此接口定义为返回行数，不是发送字节；趋势指标没有公开内/外网入站、出站选项 |
| 数据库代理监控 | [`DescribeDBProxyPerformance`](https://help.aliyun.com/zh/rds/developer-reference/api-rds-2014-08-15-describedbproxyperformance)：`Dimension=service` 按代理连接地址聚合，响应 `Service` 为代理连接地址终端 ID | RDS MySQL 仅支持 `Maxscale_CpuUsage`。`Maxscale_DownFlows`、`Maxscale_UpFlows` 被列在 RDS PostgreSQL 支持项中，不能作为现有 MySQL 的可用能力 |

### DAS“业务流量”不是按网络路径计量的承诺

官方 [DAS 审计日志说明](https://help.aliyun.com/zh/das/user-guide/overview-2)描述 SQL 洞察可“获取数据库 SQL 模板和流量”“按照分钟粒度进行预计算、分析、汇聚和存储”。结合上述 API 字段，这里的 SQL 请求分析不能直接解释成公网带宽计量接口。应要求同时存在明确的字节/速率字段、方向和网络路径维度，才能接入运行明细和告警。

目前产品文档将“DAS 企业版”更名为“DAS 审计日志”；部分 API 仍保留企业版名称。审计/SQL 洞察存在付费项目、日志存储计费与开通时间限制，不应为了本次流量拆分直接开通。`GetDasSQLLogHotData` 还要求已开启日志索引，只能查询开启后且最近 7 天内的数据，单次查询跨度最多一天、最多返回 1 万条；这些限制进一步说明它不是持续网络计数接口。

### 代理有连接地址维度，但 MySQL 没有公开吞吐项

官方[查看代理监控数据](https://help.aliyun.com/zh/rds/apsaradb-rds-for-mysql/view-the-proxy-monitoring-data-of-an-apsaradb-rds-for-mysql-instance)指南明确说明，控制台查看 `CPU 利用率（%）`，相关 `DescribeDBProxyPerformance` API“当前仅支持查看 RDS MySQL 代理实例的 CPU 利用率”。因此，不能因为 API 提供 `Dimension=service` 就假设它也有 MySQL 端点吞吐。

官方[数据库代理常见问题](https://help.aliyun.com/zh/rds/apsaradb-rds-for-mysql/faq-about-database-proxies)还说明：每个代理连接地址都可以申请内网连接地址和外网连接地址，原实例和只读实例地址不会被收回。因此，即使将来出现代理流量指标，还须核实同一 `Service` 是否混合内外网、是否覆盖原地址直连以及读写分离到其他节点的流量，不能直接减去后给剩余量贴“内网”标签。

## 权限与后续验证边界

官方授权表列出的只读动作包括：

- `hdm:GetMySQLAllSessionAsync`。
- `hdm:GetFullRequestOriginStatByInstanceId`。
- `hdm:GetDasSQLLogHotData`。
- `hdm:GetPfsMetricTrends`、`hdm:GetPfsSqlSample`。
- `rds:DescribeDBProxyPerformance`，支持 RDS 实例资源授权。

权限可让调用者读取该接口已经定义的数据，不能让接口生成不存在的网络分项。本次没有证据表明给采集用户添加上述 DAS 权限后，即可取得现有 RDS MySQL 公网/内网双向流量，因此不建议为此盲目扩权。

若后续由阿里云支持确认了未在上述公开资料定义的 RDS MySQL 分项指标，需要取得正式的命名空间、指标名、单位、内外网/端点维度、节点覆盖范围、采样周期和权限动作，再用同一实例、同一窗口只读取数核验。通过业务数据库 `performance_schema` 或其他 SQL 直查，不属于本项目“仅云 API、不新增业务数据库账号”的当前实施范围。
