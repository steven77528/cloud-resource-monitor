# 当前采集配置 API 用量审计

**2026-10-20 更新：** 下文是 2026-09-10 的历史配置快照。新增 [网络速率任务](network-traffic.md) 后，分钟任务每实例双向约增加 89,280 次/月数据查询，华为云 ECS 五分钟任务约 17,856 次/月。资源数由自动发现确定，因此旧 89,280/133,920 次不再是当前总量。请重新运行 `python scripts/estimate_api_usage.py config/config.yaml`；未知资源数时 CMS 池显示 `instance_count_required`，保留已知基数但不声称免费额度足够，盘点和元数据查询单独列出。

核验日期：2026-09-10。读取本地 `config/config.yaml` 的账号开关、任务开关、频率及指标条目，结合当前采集器的请求方式核算。新增监控任务和规则保存在 `config/rds-performance.yaml`，通过主配置的 `monitoring_files` 加载；估算器与服务复用同一个配置读取函数。下列“当前基线”指已有启用任务；新增 RDS 原生接口与 CMS 补充查询分别列项。本报告没有使用真实凭据查询已消费额度，也没有修改任何采集频率。

## 当前结论

按连续运行 **31 天**计算，当前启用的阿里云云监控查询任务预计每月 **89,280 次**，占该主账号每月 100 万次免费额度的 **8.928%**，余量 **910,720 次**。这是本服务的计划量；同主账号其他脚本、Grafana、人工诊断、额外分页和重启产生的请求需要另外相加。

按最新纯 API 方案，启用两台 MySQL 原生性能任务及一个批量慢查询补充任务后，CMS 合计为 **133,920 次/月，占 13.392%，余 866,080 次**。两台 RDS 原生性能 89,280 次、实例规格元数据 62 次、CMS 指标定义元数据 31 次另计，均不消耗四种 CMS 查询接口的百万次免费池。新增任务仍关闭；这组数字是启用后计划量，尚不是正在运行的用量。此方案不需要业务数据库账号。

目前只有 `aliyun-hycen` 一个阿里云逻辑账号启用，`aliyun-yuanzhu` 账号关闭。多个配置名称或 AccessKey 不代表多个免费额度池：如果以后启用多个逻辑账号，必须按所属阿里云主账号 UID 汇总。

| 逻辑账号 | 当前账号开关 | 本服务云监控查询/月 | 适用额度 |
| --- | --- | ---: | --- |
| `aliyun-hycen` | 开启 | 89,280 | 阿里云基础云监控 100 万次/月 |
| `aliyun-yuanzhu` | 关闭 | 0 | 关闭状态不发起请求；启用后按所属主账号汇总 |
| `huawei-hycen` | 开启 | 不适用 | 华为云使用自己的服务配额，不能套用阿里云 100 万次 |

旧文档 `aliyun-monthly-api-estimate.md` 中的 **361,677 次**是当时“3 个 OSS、4 组 SLB”的规划场景。本次按当前实际开关和指标条目计算，两个数字的资源范围不同。

## 阿里云任务明细

| 启用任务 | 当前频率 | 31 天调度轮数 | 接口类别及本次核算 |
| --- | --- | ---: | --- |
| `aliyun-slb-bandwidth` | 60 秒 | 44,640 | 两个 ALB 指标分别调用 `DescribeMetricList`，合计 **89,280 次 CMS 查询** |
| `aliyun-ecs-expiry` | 86,400 秒 | 31 | ECS `DescribeInstances`；至少 31 次，列表分页另加 |
| `aliyun-rds-expiry` | 86,400 秒 | 31 | RDS `DescribeDBInstances`；至少 31 次，列表分页另加 |
| `aliyun-domain-expiry` | 86,400 秒 | 31 | Domain `QueryDomainList`；至少 31 次，列表分页另加 |
| `aliyun-certificates` | 86,400 秒 | 31 | CAS 列表请求及每张选中证书的详情请求，见下方公式 |
| `aliyun-slb-expiry` | 86,400 秒 | 31 | BSS `QueryAvailableInstances`；至少 31 次，列表分页另加 |
| `aliyun-balance` | 3,600 秒 | 744 | BSS `QueryAccountBalance`；744 次 |
| `aliyun-oss-packages` | 43,200 秒 | 62 | BSS `QueryResourcePackageInstances`；至少 62 次，列表分页另加 |
| `aliyun-sms-statistics` | 3,600 秒 | 744 | SMS `QuerySendStatistics`；至少 744 次，结果分页另加 |

以上只有 ALB 指标查询属于 CMS 每月 100 万次免费池。ECS、RDS、域名、证书、余额、套餐包及短信统计均调用各自产品接口，不能把它们混加到 CMS 免费额度。

证书任务当前按默认两个状态范围查询列表，然后对去重、筛选后的每张证书查询详情。设每天实际列表总页数为 `P`，选中的证书数为 `C`，月请求量为 `31 × (P + C)`；每天最低两次列表请求，分页和详情需要额外相加。离线配置不能确定真实列表长度，因此不能把“31 轮”当作“31 次证书 API”。

当前关闭的阿里云任务包括 OSS 容量、OSS 请求量及出网流量、短信套餐、SLB 套餐、短信回执，及 `aliyun-yuanzhu` 账号下的所有任务。它们不计入当前基线；启用前可重新运行估算工具。

### ALB 请求为何是一轮两次

当前两个条目分别为 `acs_alb / LoadBalancerInBits` 和 `acs_alb / LoadBalancerOutBits`。采集器逐条调用 `DescribeMetricList`，不会因为它们在同一个任务中自动合并成一次请求。

两个条目均为 `period=60`、条目级 `max_age_seconds=300`。当前实现每次查询的时间范围为 `max_age_seconds + period = 360 秒`，约 6 个时间点，小于每页 `Length=1440`，按一页估算：

```text
31 × 24 × 3600 ÷ 60 × 2 = 89,280 次/月
```

云监控按 API 请求次数统计，返回 6 个数据点仍然是一次请求。需要 `NextToken` 继续取下一页时，才增加一次请求。任务级 `max_age_seconds` 用于本服务的样本有效性判断，实际查询窗口由指标条目级配置决定，两者不要混淆。

每请求一条精确时间序列的分页估算为：

```text
数据点数 = ceil((条目 max_age_seconds + period) / period)
每轮请求页数 = max(1, ceil(数据点数 / 1440))
月查询次数 = ceil(31 × 86400 / interval_seconds) × 所有条目的每轮请求页数之和
```

如果维度不完整导致返回多条时间序列、云端分页策略变化或发生重试，实际请求可能更多。当前代码要求每条指标指定精确维度，并会拒绝有歧义的数据。

## 华为云任务明细

| 启用任务 | 当前频率 | 31 天调度轮数 | 接口调用估算 |
| --- | --- | ---: | --- |
| `huawei-ecs-expiry` | 86,400 秒 | 31 | BSS 包周期资源列表；至少 31 次，分页另加 |
| `huawei-rds-expiry` | 86,400 秒 | 31 | 当前一个地域、MySQL 和 PostgreSQL 分别查 RDS 列表；至少 62 次，分页另加 |
| `huawei-slb-expiry` | 86,400 秒 | 31 | BSS 包周期资源列表；至少 31 次，分页另加 |
| `huawei-balance` | 3,600 秒 | 744 | BSS 余额查询；744 次 |
| `huawei-elb-bandwidth` | 60 秒 | 44,640 | `SYS.VPC` 上下行带宽各一次 CES `ShowMetricData`，合计 **89,280 次** |

华为云证书、OBS 套餐、OBS 容量、OBS 流量任务当前关闭。上述 CES 89,280 次不会消耗阿里云账号免费额度。

华为云官方 [ShowMetricData 接口说明](https://support.huaweicloud.com/api-ces/ces_03_0033.html)明确规定：“单个API的调用次数限制为500次/分钟”，返回 `ces.0429` 或 `Too Many Requests` 表示被限流。当前上下行两个条目每 60 秒各请求一次，即 **2 次/分钟，占 500 次/分钟的 0.4%**，本服务正常采集计划低于这个公开速率阈值，无需因此降低现有频率。同一配额范围内的其他程序、手工诊断和重试也要一起计算；月平均数不能代替峰值检查。

本次查阅的 `ShowMetricData` 接口说明和 [CES 约束与限制](https://support.huaweicloud.com/productdesc-ces/ces_07_0007.html)列出请求频率及告警、资源分组等数量限制，**未列出该查询接口类似阿里云“100 万次/月”的免费调用池**。因此本报告不把华为 CES 的 89,280 次与阿里云月额度比较，也不据此宣称华为所有 API 免费、无限次或没有其他计费与限制。未登录用户账号查询定制配额；BSS、RDS 等其他华为云产品接口需分别核对。

## 纯 API 数据库监控方案

CPU、内存、空间、连接及适用实例的复制延迟继续采用阿里云 RDS 原生 `DescribeDBInstancePerformance`，一个实例的一组性能指标合并为一次请求，每 60 秒采集一次。该原生接口不是 CMS `DescribeMetricList`，自身新增的 CMS 月调用量为 0。MySQL 慢查询使用新增 `rds_cloudmonitor` 任务调用 CMS `DescribeMetricList`，需要另外计入免费池。

同日新增的 MySQL 行锁等待速率通过同一次请求的 `MySQL_ROW_LOCK.innodb_row_lock_waits` 相邻采样差分获得。增加这个 Key 不增加每轮请求次数，不增加 CMS 免费池消耗；两台实例启用后仍为原生性能 89,280 次/月、规格元数据 62 次/月，含慢查询的 CMS 合计仍为 133,920 次/月。

每个实例的名称、规格等元数据使用 `DescribeDBInstanceAttribute`，默认缓存 86,400 秒。缓存必须跨采集轮次保持，不能每轮创建后立即丢弃。

设启用的 RDS 性能监控实例数为 `N`，31 天稳态估算为：

```text
RDS 性能接口：44,640 × N 次/月
RDS 元数据接口：31 × N 次/月（每天刷新一次）
CMS 查询接口：仍为现有 ALB 的 89,280 次/月
```

上述公式只计原生性能部分；加入下面的批量慢查询后，CMS 增加 44,640 次/月。

本次确认 `aliyun-hycen` 有 **2 台 MySQL 实例**。2026-09-10 首次检查新增 API 返回 `Forbidden`；用户补充只读权限后，同日复测两台实例的核心性能和规格接口均成功，当前权限已生效。新增性能任务仍保持 `enabled: false`，此次只读复测没有开启定时采集。两台实例的启用后场景如下；关闭期间不产生这些计划调度请求，人工复测请求另计：

| 接口 | 当前计划请求/月 | 两台 MySQL 启用后/月 | 是否消耗 CMS 100 万次 |
| --- | ---: | ---: | --- |
| RDS `DescribeDBInstancePerformance` | 0 | **89,280** | 否 |
| RDS `DescribeDBInstanceAttribute` | 0 | **62** | 否 |
| 已有 ALB `DescribeMetricList` | 89,280 | **89,280**，保持不变 | 是 |
| MySQL 慢查询，两个实例批量 `DescribeMetricList` | 0 | **44,640** | 是 |
| CMS 指标定义 `DescribeMetricMetaList` | 0 | **31**，同任务一个地域和指标，每天缓存 | 否 |

因此，两台 MySQL 纯 API 方案启用后的 CMS 合计为 `89,280 + 44,640 = 133,920` 次/月。若把两实例拆成两个单独慢查询请求，才会变成 178,560 次；当前设计采用同任务内批量，不采用这个较高数值。

现场确认每台实例一次批量性能请求包含 `cpuusage&memusage`（`%`）、`active_session&total_session`（`int`）、`ins_size&data_size&log_size&tmp_size&other_size`（`MB`）。每个 Key 返回 15 点、间隔 60 秒；仅核心 CPU、内存、空间、连接四项时，每台转换出 60 个样本，仍只计一次性能请求。两台实例均返回存储 100 GB、最大连接数 6000；“民用数据库”为 `HighAvailability`，`iot-kernel-mysql` 为 `Basic`。

最终配置仅在民用任务加入 `replication_role: primary`，基础版 iot 不配置复制监控。民用复制接口实际字段为 `replication-lag`（单位 `s`），本轮修复合法连字符字段兼容后复测成功：**民用 75 个样本（五项各 15 点），iot 60 个样本（四项各 15 点）**，最新采集时间均为 **2026-09-10 13:52:00（Asia/Shanghai）**，民用复制延迟最新为 **0 秒**。两项任务仍关闭。复制指标合并在民用同一次批量性能请求内，所以原生部分仍为两台合计 89,280 次性能查询和 62 次元数据查询/月，复制指标本身不增加 CMS 调用。纯 API 方案另有表中慢查询的 44,640 次 CMS 请求；返回样本或字段变多不会使原生请求次数按指标翻倍。

PostgreSQL 在另一个阿里云账号，保留关闭的原生性能及 CMS `PG_SharedBufferHitRate` 模板入口，尚未在实际账号验证。其账号、地域、实例及权限确认后，分别将原生性能计入 RDS API 计划量、CMS 补充计入所属主账号的 CMS 计划量。不能将其他账号的 PostgreSQL 与当前 MySQL 合并成一次请求。

实例数以 `rds_performance.options.instances` 最终配置为准。未确定实例数时，估算器保留上述公式，不将未知数量当成 0。实例数相同、每轮请求不拆分时，增加同一次 RDS 请求中的性能指标不会按指标数再次相乘。RDS 原生接口仍有自己的频率和返回量限制，不能理解为无限次、不受限制。

阿里云 [DescribeDBInstancePerformance 接口说明](https://help.aliyun.com/zh/rds/developer-reference/api-rds-2014-08-15-describedbinstanceperformance)列出单用户每秒 300 次的频率限制。两台实例每 60 秒共两次性能请求，平均约 **0.033 次/秒**；即使两次请求集中于同一秒，仅本服务的这两次请求也低于该公开阈值。仍需计入同配额范围的其他调用方，并在收到限流时处理，不能把平均速率低理解为任何场景都不会限流。

### CMS 批量和元数据口径

新任务 `aliyun-mysql-cloudmonitor` 在 `options.instances` 中列出两台 MySQL，同地域、同引擎的 `MySQL_SlowQueries` 每轮合并为一次请求。MySQL 首次 API 核查返回两实例合计 30 点，新采集器最终现场检查返回民用 14 点、iot 15 点，两次均无额外分页。后一次只调用 1 次元数据、1 次数据查询，未填补缺失点；新任务保持关闭，现场请求不代表已经启用调度。

新采集器按每个任务内的 `region + engine` 分组，当前每种引擎只对应一个补充指标：MySQL 为 `MySQL_SlowQueries`，PostgreSQL 为 `PG_SharedBufferHitRate`。每批最多 50 个实例，不跨任务、账号、地域或不同指标合并。`period` 固定 60 秒，`lookback_seconds` 默认 900 秒，`max_age_seconds` 默认 300 秒；样本新鲜度上限不是查询回看窗口。

查询时间边界向下对齐分钟。离线预算按边界点多预留一份，使用以下保守公式；它不把返回点数直接当作调用次数：

```text
每实例保守点数 = floor(lookback_seconds / 60) + 2
每批请求页数 = ceil(每实例保守点数 × 本批实例数 / 1440)
每轮 CMS 请求数 = 所有地域和引擎分组中、每个最多 50 实例批次的页数之和
每月 CMS 请求数 = ceil(31 × 86400 / interval_seconds) × 每轮 CMS 请求数
```

默认回看下每实例按 17 点预算，两实例按 34 点预算，仍只有一页。50 个实例按 850 点预算也为一页；第 51 个实例需另起一批。回看窗口增长造成超过 1440 点时，各批分页分别追加请求。

首次采集每个地域和指标时调用一次 `DescribeMetricMetaList` 验证单位，之后按 `metadata_cache_seconds` 缓存，默认 86,400 秒。稳态每月为 `31 × 该任务实际启用的地域和指标组合数`，不是每实例或每批各 31 次。缓存属于任务，多个独立任务或进程不共享缓存；重启或人工诊断还会增加首次查询。该接口查询的是指标定义，不属于 `List/Last/Data/Top` 四类查询免费池。

### 不接数据库的边界

最新配置已移除 `rds_sql` 任务，**不用填写业务数据库账号或密码**。MySQL 慢查询改由 CMS API 获取；PG 缓存候选使用 `PG_SharedBufferHitRate`，其字段单位和数据可用性仍待真实 PG 账号验证。它表示共享缓冲区命中率，不应未经核对便等同于先前直接查询 `pg_stat_database` 得到的加权增量口径。

MySQL 精确死锁小时计数仍未接入，原规则配置保留。新增的行锁等待速率独立使用 >=1 次/秒持续 5 分钟 P1，不把行锁等待当死锁。PG 已按用户确认改为原生 `mean_deadlocks_delta` >0 持续 3 分钟 P1，以及超过 5 秒的 SQL 数 >=1 持续 5 分钟 P1；两个 Key 合并原生请求，不增加每轮次数。PG 配置仍待回填实例并做现场验证，旧的精确死锁小时数及 30 秒 SQL 条件不再用于 PG 当前方案。

## 离线重复核算

在项目目录运行以下命令。仅需要 Python 和项目已有的 PyYAML；无需 MySQL、云凭据或网络连接。脚本也支持从其他工作目录使用脚本和主配置的绝对路径执行。`monitoring_files` 相对于主配置目录解析，并限制文件必须位于该目录内，避免漏算拆分文件中的任务。输出只包含允许展示的账号 ID、任务信息和计算结果，不输出密钥、数据库密码、通知配置或原始 YAML。

```shell
python scripts/estimate_api_usage.py config/config.yaml
```

按 30 天重算，并独立测算该账号 3 个 RDS 实例的方案：

```shell
python scripts/estimate_api_usage.py config/config.yaml --days 30 --rds-instances aliyun-hycen=3
```

该覆盖参数只添加 RDS 原生性能规划场景，不修改配置、不把它再次相加到已配置任务，也不替代 CMS 补充任务的配置。正式配置中的 `rds_performance` 按实际实例列表、频率及缓存时间输出；`rds_cloudmonitor` 按地域、引擎、50 实例分批及分页计入 CMS。关闭任务的实际计划调用为 0，其 `cloudmonitor_plan` 中的 `*_if_enabled` 字段单独展示启用后的预估量，不能与当前汇总重复相加。

如果确认两个逻辑账号属于同一主账号，可以明确传入相同 UID 后汇总。下面的 UID 为示例，需要替换成已确认的所属主账号 UID：

```shell
python scripts/estimate_api_usage.py config/config.yaml --owner aliyun-hycen=1234567890123456 --owner aliyun-yuanzhu=1234567890123456
```

不传 `--owner` 时会标记主账号归属未核实，并按逻辑账号展示；不能据此认为每个逻辑账号都各有 100 万次免费额度。

## 达到额度时的处理

当前基线无需为了免费额度降低 ALB 的 60 秒频率。未来同主账号增加大量 CMS 指标时，优先考虑适用产品的原生批量接口，或将同一 `MetricName` 的多个实例合并查询。当前通用 `metrics` 实现尚未自动进行跨条目批量合并。

如果仍需超出免费额度，应明确开通基础云监控按量计费，或评估按数据点计费的实时导出方案。免费额度耗尽且未开通按量计费时，查询会受影响；不能假设必定自动收费并继续工作。官方按量计费示例单价为 0.12 元/万次，实际以购买页面为准。

每月总量与每秒速率是不同限制。`DescribeMetricList` 单 API 默认主账号及其 RAM 用户共用 50 次/秒；当前两条指标每分钟的请求规模较小，但同账号其他程序或多任务并发仍应合并考虑。

本服务没有查询真实已用额度。可在云监控控制台“概览 → 资源消耗 → 基础云监控用量统计 → API 月调用次数”核对，结合本报告的剩余计划量判断当月是否足够。

## 官方依据

1. [DescribeMetricList：每月免费额度、50 次/秒、分页和最多 50 实例](https://help.aliyun.com/zh/cms/cloudmonitor-1-0/developer-reference/api-cms-2019-01-01-describemetriclist)
2. [API 调用次数口径：按请求统计、与数据条数无关、RAM 汇总到主账号](https://help.aliyun.com/zh/cms/cloudmonitor-1-0/support/query-the-number-of-api-calls)
3. [基础云监控免费额度](https://help.aliyun.com/zh/cms/cloudmonitor-1-0/product-overview/free-quota)
4. [基础云监控按量计费](https://help.aliyun.com/zh/cms/cloudmonitor-1-0/product-overview/pay-as-you-go-1)
5. [API 额度用尽处理](https://help.aliyun.com/zh/cms/cloudmonitor-1-0/support/fix-the-issue-that-the-api-quota-has-been-exhausted-or-is-about-to-be-exhausted)
6. [RDS 原生性能接口](https://help.aliyun.com/zh/rds/developer-reference/api-rds-2014-08-15-describedbinstanceperformance)
7. [华为云 ShowMetricData：单 API 每分钟 500 次](https://support.huaweicloud.com/api-ces/ces_03_0033.html)
8. [华为云 CES 约束与限制：资源及告警数量配额](https://support.huaweicloud.com/productdesc-ces/ces_07_0007.html)
9. [华为云 API 流控处理建议：减小频率、缓存、退避和申请扩容](https://support.huaweicloud.com/api-ces/ces_03_0162.html)
10. [DescribeMetricMetaList：指标定义、单位与统计周期](https://help.aliyun.com/zh/cms/cloudmonitor-1-0/developer-reference/api-cms-2019-01-01-describemetricmetalist)

以上每月 100 万次口径限定云监控 1.0 `Cms/2019-01-01` 的四种查询接口，不适用于云监控 2.0、Prometheus、RDS 原生接口或华为云。
