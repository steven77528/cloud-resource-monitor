# RDS MySQL / PostgreSQL 云 API 性能监控

只使用阿里云 AccessKey 采集，不需要业务数据库地址、用户名或密码。CPU、内存、空间、连接等通过 RDS 原生 API 批量查询；MySQL 慢查询及 PostgreSQL 缓存命中率通过云监控 API 补充。原有实例有效期任务、华为云任务及通知路由保持原配置。

## 指标和规则

| 指标 | 数据来源 | 采集周期 | 告警规则 |
| --- | --- | --- | --- |
| MySQL / PG CPU | `DescribeDBInstancePerformance` | 60 秒，同实例批量 | >=80% 持续 5 分钟，P1 |
| MySQL / PG 内存 | 同上 | 60 秒，同实例批量 | >=85% 持续 5 分钟，P1 |
| MySQL / PG 磁盘 | 同上，除以实例存储规格 | 60 秒，同实例批量 | >=80% 持续 10 分钟 P1；>=90% 持续 5 分钟 P0 |
| MySQL / PG 连接 | 同上，总连接数除以最大连接数 | 60 秒，同实例批量 | >=80% 持续 5 分钟，P1 |
| MySQL 复制延迟 | 主备 `MySQL_ReplicationDelay` / 只读 `slavestat` | 60 秒，同实例批量 | >3 秒，P0 |
| MySQL 行锁等待速率 | 原生 `MySQL_ROW_LOCK.innodb_row_lock_waits` 相邻采样差分 | 60 秒，合并原生批量请求 | >=1 次/秒持续 5 分钟，P1 |
| MySQL 每秒慢查询量 | CMS `MySQL_SlowQueries`，`count/s` | 60 秒，同指标同地域多实例批量 | >=10 次/秒，P1 |
| PG 缓存命中率 | CMS `PG_SharedBufferHitRate`，`%` | 60 秒，同指标同地域多实例批量 | <95% 持续 10 分钟，P1 |
| PG 死锁速率 | 原生 `PolarDBQPSTPS.mean_deadlocks_delta` | 60 秒，合并原生批量请求 | >0 次/秒持续 3 分钟，P1 |
| PG 当前超过 5 秒的 SQL 数 | 原生 `PolarDBLongSQL.mean_five_seconds_executing_sqls` | 60 秒，合并原生批量请求 | >=1 持续 5 分钟，P1 |

2026-09-10 用户已确认后两项新规则，替代 PG 原来的“每小时死锁 >3 次”和“当前 SQL 执行 >30 秒至少 1 条”。指标含义来自官方文档，告警阈值是本项目的运维设置，不宣称是阿里云官方通用推荐。

PG 持续 5 分钟有超 5 秒 SQL，表示每个有效采样点的该项数量均达到阈值，可能是不同 SQL 轮流执行；不代表同一条 SQL 连续运行 5 分钟。死锁速率使用原始精度判断，不乘 3600 冒充过去一小时次数。通知中大于 0、小于 1 的死锁速率显示为“大于 0 且小于 1 次/秒”，避免四舍五入后出现触发告警却显示 0 的情况。

云监控命名空间为 `acs_rds_dashboard`，数据查询使用 `DescribeMetricList`。`DescribeMetricMetaList` 只提供指标定义。新采集器首次及默认每 24 小时校验指标元数据中的单位、维度、60 秒周期及 `Average` 统计方式。

MySQL 慢查询使用 60 秒周期的 `Average`，对应该周期的每秒平均慢查询量，不保证捕捉一秒内的短峰值。“慢”的含义由数据库的慢查询设置决定。PG 缓存采用官方 SharedBuffer 命中率定义。

## MySQL 行锁等待

项目包含独立告警“近期行锁等待速率”，默认 >=1 次/秒持续 5 分钟触发 P1 钉钉通知。60 秒采样时，相邻区间新增至少 60 次等待才达到阈值。这是用于发现持续锁竞争的初始运维阈值，不是阿里云统一推荐，也不等于死锁次数或锁等待时长。

两台真实实例的 `MySQL_ROW_LOCK` 均返回 `Unit=int`、`ValueFormat=innodb_row_lock_waits&innodb_row_lock_time&innodb_row_lock_time_avg`。民用在最初 20 分钟检查中一直为 `455&111032&244`，iot 为全零。它们是累计次数、累计耗时和累计平均值；`111032 / 455` 约为 244 毫秒。不能把历史累计等待次数或累计平均耗时直接解释为当前一分钟异常。这里读取的是云端采样后的累计值，实际响应可能带小数，不能仅凭 `Unit=int` 强制要求每个值为整数。

```text
近期行锁等待速率 = (本次累计 waits - 上次累计 waits) / 两次云端采样的实际间隔秒数
```

采集器按字段名读取非负、有限的累计值，以真实时间戳差分。累计值保留十进制精度，先求差值再转换为输出速率，避免先转浮点导致大整数的小增量丢失；不将小数取整、不将非法值补为 0。整数累计值的 `source_counter` 仍保留整数形式。首次有效点只建立基线；累计 455 次一直不增长时，速率为 0。计数下降或采样间隔过大时丢弃跨越该区间的差分并重新建立基线，不输出伪造的 0。负数、非有限值、非法格式仍报告采集异常，没有有效差分或有效差分已过期时也报告异常。历史序列由每轮原生 API 返回，无需业务数据库连接或额外持久化计数。

### 2026-10-12 小数累计值兼容

本次民用数据库故障由用户提供的服务器日志与只读 API 数据确认。13:55 至 14:00 每轮为 `ok samples=89`，14:01:22 起连续报 `invalid or negative metric value; metric=db_row_lock_waits_per_second`。同一组云端采样的 waits 值如下，时间按 Asia/Shanghai 展示：

| 采样时间 | `innodb_row_lock_waits` | 旧版处理 |
| --- | --- | --- |
| 13:45 至 13:59 | `824.0` | 数值等于整数，接受 |
| 14:00 | `826.9` | 强制整数校验失败 |
| 14:01 至 14:04 | `827.0` | 单点符合整数校验，但整轮仍包含 14:00 的失败点 |

每轮请求回看最近 15 分钟，并校验整个返回序列。一次小数值会在后续轮次中反复出现，因此最新点恢复成整数后，旧版仍可能持续失败约 15 分钟，直至该历史点退出查询窗口；这并非持续新增的数据库错误。采集是整组校验，行锁解析失败也使同任务的 CPU、内存、磁盘、连接和复制延迟样本无法保存。

修复后，`824.0 -> 826.9 -> 827.0` 按原值和真实采样间隔计算速率，不舍弃 `0.9` 或将这段数据补为正常零值。有限小数的兼容不改变原有告警阈值、累计值下降处理、最大差分间隔或数据新鲜度要求；也不修改云账号、任务开关及通知渠道。详细服务器复查见 [采集报错排查](troubleshooting-collection.md#2026-10-12-rds-行锁采集失败)。

这项主要发现“等待发生得频繁”，不能单独发现“只有一个等待，但阻塞很久”。计数下降可识别常见重启、切换或计数器重置；若重置后已增长到旧值以上，仅靠这组计数无法识别，可能低估等待次数。当前接口没有同时提供可验证的启动标识，不能声称消除所有跨重启误差。

原 MySQL `db-deadlocks` 小时规则仍保留且没有绑定采集任务；已确认的 PG 死锁速率与 5 秒长 SQL 配置保持不变。

## 官方接口复核

| 关注点 | 官方定义和当前处理 |
| --- | --- |
| MySQL 死锁次数 | MySQL 完整性能参数表未列出死锁 Key。`MySQL_ROW_LOCK` 是最大/平均行锁等待时间及等待次数，不是死锁次数。暂不采集这项，也不生成正常值。 |
| PG 死锁 | `PolarDBQPSTPS` 的 `mean_deadlocks_delta` 原文为“每秒死锁数”。按 `ValueFormat` 定位该字段，采用用户确认的新速率规则。 |
| PG 长 SQL | `PolarDBLongSQL` 仅列 `mean_one_second_executing_sqls`、`mean_three_seconds_executing_sqls`、`mean_five_seconds_executing_sqls`，分别为超过 1/3/5 秒的 SQL 条数。本配置选择 5 秒桶。 |
| 原来的 30 秒条件 | `DescribeDBInstancePerformance` 没有 N / Threshold 参数；`DescribeMetricMetaList.Periods` 是统计周期，也不是 SQL 运行时间筛选。旧 30 秒规则已按用户选择替换，不是假装 API 支持任意 N。 |

`DescribeDBInstancePerformance` 的业务参数为 `DBInstanceId`、`Key`、`StartTime`、`EndTime`、`NodeId`。`Key` 最多 30 个是可查询的指标组数量，不是 SQL 超时 30 秒。

MySQL 的 DAS `GetDeadlockHistogram` 需要相应 DAS 运维服务和完整死锁日志设置，并按小时分析。错误日志 API 返回的记录条数也不直接等于死锁事件数，本次没有自动开通或接入这些功能。原 `db-deadlocks` 小时规则保留定义但无任务引用。

## 配置和开关

主配置 `config/config.yaml` 通过以下一行读取补充配置：

```yaml
monitoring_files: [rds-performance.yaml]
```

`config/rds-performance.yaml` 只包含任务、规则和实例信息，项目默认配置不使用需要业务数据库凭据的 `*-sql` 任务。代码中的 `rds_sql` 类型仅保留旧版兼容；从旧版迁移时，检查服务器配置中是否还保留这些任务。

| 任务 ID | 内容 | 项目默认开关 |
| --- | --- | --- |
| `aliyun-mysql-civil-performance` | hycen 民用数据库 CPU、内存、磁盘、连接、复制延迟、行锁等待速率 | 开启 |
| `aliyun-mysql-iot-performance` | hycen iot 数据库 CPU、内存、磁盘、连接、行锁等待速率 | 开启 |
| `aliyun-mysql-cloudmonitor` | hycen 上述两台 MySQL 的慢查询合并采集 | 开启 |
| `aliyun-yuanzhu-mysql-changsha-readonly-performance` | yuanzhu 长沙乐芒只读核心性能、行锁等待速率 | 开启 |
| `aliyun-yuanzhu-mysql-national-readonly-performance` | yuanzhu 乐芒全国只读核心性能、行锁等待速率 | 开启 |
| `aliyun-yuanzhu-mysql-modian-performance` | yuanzhu 魔电生产数据库核心性能、主备复制延迟、行锁等待速率 | 开启 |
| `aliyun-yuanzhu-mysql-wuhan-performance` | yuanzhu 武汉乐芒数据库核心性能、主备复制延迟、行锁等待速率 | 开启 |
| `aliyun-yuanzhu-mysql-changsha-performance` | yuanzhu 长沙乐芒数据库核心性能、主备复制延迟、行锁等待速率 | 开启 |
| `aliyun-yuanzhu-mysql-cloudmonitor` | yuanzhu 上述五台 MySQL 的慢查询合并采集 | 开启 |
| `aliyun-postgresql-performance` | PG 核心性能、死锁速率、超过 5 秒 SQL 数 | 关闭，待填实例 |
| `aliyun-postgresql-cloudmonitor` | 同一个 PG 的缓存命中率 | 关闭，待填实例 |

实际运行还要求主配置中的对应账号已启用，且容器能读取该账号凭据。仅配置 `access_key_id` 和 `access_key_secret` 不会自动生成原生性能任务；`rds_performance` 与 `rds_cloudmonitor` 必须明确填写 `options.instances`。新增实例时可按现有任务复制，给任务设置唯一 ID，核对 `account`、`resource_id`、`engine`、`region`，并在对应账号的慢查询任务中补充同一实例。每台实例一个原生任务可将性能接口或数据异常限制在该实例，不影响其他实例的原生采集。

2026-10-23 服务器只读发现 `aliyun-yuanzhu` 在 `cn-hangzhou` 有以下五台 MySQL 8.0。此前该账号只有两项关闭且未填 ID 的 PostgreSQL 模板，因此填写 AK 后仍没有这些 MySQL 的 CPU、内存、磁盘和连接数据。现已为这五台 MySQL 增加独立原生任务及一个批量慢查询任务。

| 实例名称 | 实例 ID |
| --- | --- |
| 长沙乐芒只读 | `rr-bp10kz0b0s7f86t3y` |
| 乐芒全国只读 | `rr-bp1396zk752h1z0t1` |
| 魔电生产数据库 | `rm-bp13k3392yhx84kg7` |
| 武汉乐芒数据库 | `rm-bp120320qjpva90ck` |
| 长沙乐芒数据库 | `rm-bp1b4y2vx0inl80i7` |

五台原生核心性能、行锁等待及 CMS 慢查询均已通过该账号的只读 API 诊断。实例元数据核实三台 `rm-` 为高可用主实例，两台 `rr-` 为高可用只读实例；三台主实例的 `MySQL_ReplicationDelay` 也有有效新鲜样本，已配置 `replication_role: primary`。两台只读实例的 `slavestat` 返回单位为空或不受支持，暂不配置只读复制延迟，不将缺失值补为 0；这不影响其 CPU、内存、磁盘、连接、行锁等待与慢查询。

RDS 网络速率与性能任务分别配置：`config/network-traffic.yaml` 中的 `aliyun-yuanzhu-rds-network` 按该账号地域自动发现 MySQL，不需要再复制上述五个性能任务的 ID 清单。服务器已确认该任务正常，导出中有五台实例各自的新鲜入站、出站样本。性能图无数据先检查实例清单；网络图无数据应检查最新样本、Prometheus 抓取和看板查询或筛选，不能靠重复添加网络任务解决。网络配置详见 [ECS 与 RDS 网络速率](network-traffic.md)。

PG 两项保持关闭模板。若后续确实接入 PostgreSQL，`account` 必须指向实际账号，同时填写相同的 `resource_id`、`resource_name`、`region`，再核实权限和原生返回；不能把当前五台 MySQL 填入 PG 模板。

当前七台 MySQL 原生任务的实例中包含 `row_lock_waits: true`，任务的 `rules` 引用 `db-row-lock-waits`。这个开关仅适用于 MySQL；设为 `false` 或不填写时，不请求行锁 Key。旧版配置若缺少此项，需补齐实例开关、任务规则引用及规则定义，并保留实际实例信息和原有任务开关。

PG 原生性能任务的实例选项已经包含：

```yaml
deadlock_rate: true
long_query_seconds: 5
```

它们只适用于 PostgreSQL。`long_query_seconds` 只接受 1、3、5，选择不同桶时需同步修改规则标题和阈值；指标身份区分桶，避免沿用旧持续状态。新配置中的 PG 开关仅代表启用任务后请求这些 Key，不代表已经在另一个账号现场验证。

新 `rds_cloudmonitor` 根据实例 `engine` 选择 MySQL 慢查询或 PG 缓存命中率，不接受任意 `metrics`、`long_query_seconds` 或 `node_id`。同账号、同地域、同指标每 50 台以内合并请求，超过 50 台拆批；不同账号不能共用请求。`resource_name` 用于告警展示，不填则回退实例 ID。

原生性能每天读取名称、存储规格和最大连接数并缓存；扩容、更名或修改连接上限后需要等缓存刷新或重启采集器。民用数据库已核实为高可用系列，使用 `replication_role: primary`；iot 为基础系列，不配置主备延迟。

## 时序和缺失数据

每轮读取最近 15 分钟的完整序列，按实际采样时间去重、排序、评估所有新增点，只通知最终状态，避免历史短暂异常造成过时通知。默认最大样本间隔 90 秒，漏点会中断持续时间证明，稀疏数据不会补为正常。

轮询间隔不等于云端上报频率。缺失、过期、未来、非法值、错误单位、实例身份不匹配、冲突数据及异常分页均按采集失败处理。批量请求中任一实例没有有效序列会使该任务报告错误，其他独立任务可继续。轮询增加 0～60 秒检测等待，另加云端延迟。

PG 两个新增原生 Key 的实际 Unit 拼写和复合字段仍需该账号现场验证。代码只接受对应速率/数量单位，未知或空单位报错，不能为通过检查任意换算。采样时间按 API 返回值保存，通知仍用 Asia/Shanghai；阈值判断保留原精度，仅展示取整。

## 阿里云权限

补充策略 `docs/policies/aliyun-rds-performance.json` 包含：

```text
rds:DescribeDBInstanceAttribute
rds:DescribeDBInstancePerformance
cms:QueryMetricList
```

这些都是只读动作。已有基础策略包含 `cms:QueryMetricList` 时不必重复授权，原有 `rds:DescribeDBInstances` 继续用于有效期。各实际账号分别核对。

`DescribeMetricMetaList` 官方授权信息为“当前 API 暂无授权信息透出”，公开元数据 `ramActions` 为空，不能据此编造 `cms:QueryMetricMeta` 动作。当前 MySQL 账号已成功读取指标元数据；其他账号以只读诊断结果为准。

## 部署

代码、配置结构、文档和 Docker Compose 在同一个 `cloud-resource-monitor` 项目内直接维护。部署时使用完整项目，按[项目部署说明](../README.md)配置服务器环境；更新已有部署时，保留服务器的账号凭据、通知配置、`.env`、资源信息和 MySQL 数据，按需合并配置结构变化。

Compose 的 `cloud-monitor.volumes` 必须包含：

```yaml
volumes:
  - ./config/config.yaml:/app/config/config.yaml:ro
  - ./config/rds-performance.yaml:/app/config/rds-performance.yaml:ro
```

确认主配置的 `monitoring_files`、上述挂载、实际账号及所需任务开关后，在服务器的完整项目根目录执行：

```bash
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml diagnose \
  --job aliyun-mysql-civil-performance \
  --job aliyun-mysql-iot-performance \
  --job aliyun-mysql-cloudmonitor
docker compose up -d --no-deps --force-recreate cloud-monitor notification-gateway
```

`diagnose` 只采集检查，不连接业务数据库、不写监控数据库、不发送通知，要求任务和账号已启用。重建镜像无需先删旧镜像，也无需删除 MySQL 数据卷。P0 短信开关、P1 钉钉和 P2 邮件路由沿用原配置。

补齐 yuanzhu 配置后，可在替换采集容器前单独只读检查这六项任务：

```bash
docker compose exec -T cloud-monitor python -m cloud_monitor --config /app/config/config.yaml diagnose \
  --job aliyun-yuanzhu-mysql-changsha-readonly-performance \
  --job aliyun-yuanzhu-mysql-national-readonly-performance \
  --job aliyun-yuanzhu-mysql-modian-performance \
  --job aliyun-yuanzhu-mysql-wuhan-performance \
  --job aliyun-yuanzhu-mysql-changsha-performance \
  --job aliyun-yuanzhu-mysql-cloudmonitor
```

挂载的 YAML 文件改动需要重启或重建 `cloud-monitor` 才会载入正在调度的服务；仅在诊断命令中读到新配置不表示运行中的调度器已更新。更新时保留服务器现有凭据、其他任务开关和通知配置，并备份原文件。只补配置无需重建通知网关或删除 MySQL 数据卷。

对于已部署的民用数据库任务，仅更新本次小数累计值修复时，在服务器原项目目录同步代码，保留服务器配置和全部开关后执行：

```bash
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml diagnose --job aliyun-mysql-civil-performance
docker compose up -d --no-deps --force-recreate cloud-monitor
docker compose logs --tail=100 cloud-monitor
```

先确认只读诊断输出 `status: ok`，再替换运行中的 `cloud-monitor` 容器。只构建镜像不会更新已有容器；本次修复无需重建通知网关、修改权限、关闭行锁规则或重建 MySQL 数据卷。诊断样本数随云端实际序列变化，不能把固定的 `89` 当作成功条件；成功依据是状态及后续正常采集日志。

## 用量和验证

以下 89,280 / 133,920 次月用量是 2026-09-10 两台 hycen MySQL 的历史场景，不含后续网络任务及 yuanzhu 任务，不能作为当前总量。该历史场景按连续 31 天估算，原启用配置 CMS 为 89,280 次/月；开启两台 MySQL 原生性能和批量慢查询后，CMS 为 **133,920 次/月，占 100 万次免费额度的 13.392%**；RDS 性能 89,280 次/月、实例规格 62 次/月及慢查询指标元数据约 31 次/月分别另计，均不占 CMS 四类数据查询的免费池。重启、人工诊断、重试、分页和同主账号其他系统额外计算。

yuanzhu 本次五台实例每 60 秒独立查询原生性能，31 天约增加 223,200 次 RDS 性能查询，默认每天读取规格约 155 次；同地域五台 MySQL 慢查询合并为每轮一次，约增加 44,640 次 CMS 数据查询，元数据约 31 次。各账号额度单独核对，RDS 网络自动发现和网络数据查询另计；按服务器最终配置运行下方估算脚本。

PG 的死锁及长 SQL 合并原生请求，不增加同实例每轮请求次数。另一账号每台 PG 原生性能为 44,640 次/月，另有 PG 缓存 CMS 查询，需按实际实例、地域、批次及账号核算。

MySQL 新行锁 Key 也合并同一次原生性能请求，因此两台原生性能仍为 89,280 次/月、规格 62 次/月，CMS 启用后仍为 133,920 次/月。无需增加 RAM 动作或云服务开通项。

```bash
python scripts/estimate_api_usage.py config/config.yaml --days 31
```

详见 [当前 API 用量审计](current-api-usage-audit.md)。2026-09-10 只读现场证据：两台 MySQL 原生性能返回每分钟数据，民用含复制延迟 75 个样本、iot 60 个样本；单次云监控慢查询请求同时返回两台各 15 个样本。该次采集器最终现场检查共调用 1 次指标元数据、1 次数据查询，取得民用 14 点、iot 15 点，最新慢查询均为 0 次/秒；最新采样分别为 2026-09-10 14:25:00、14:26:00（Asia/Shanghai），历史缺失点未补零。PG 尚无实际账号验证；当前任务开关以上方配置表和实际服务器 YAML 为准。

上一版纯 API 交付验证：628 项测试通过，8 项需要独立 MySQL 环境的集成测试跳过；Ruff 通过，Docker 镜像构建成功，断网容器内五项 RDS 配置检查及错误 SQL 桶映射拒绝检查通过。未连接业务数据库或发送通知。阿里云旧 SDK 的 TLS 回归测试仍有两条 `ssl.PROTOCOL_TLS` 弃用警告，不是此前的 SNI 缺失警告。

新增行锁后的最终现场复测：民用共 89 个样本，其中行锁速率 14 点；iot 共 74 个样本，其中行锁速率 14 点。最新采样均为 2026-09-10 14:44:00（Asia/Shanghai），两台速率均为 0 次/秒，原始累计次数分别为 455 和 0。每台只发起 1 次规格请求及 1 次性能请求；首个累计点仅作基线，所以 15 个累计点转换为 14 个速率点。

行锁更新验证：651 项本地测试通过，8 项需要独立 MySQL 环境的集成测试跳过；Ruff 和 Docker 构建通过，最终镜像在断网、只读容器中通过 82 项原生采集测试和配置检查。新样本转换覆盖精确计数、乱序、重置、漏点、过期及错误字段映射，告警测试覆盖持续时间和独立通知名称。仍未开启定时任务、连接业务数据库或发送通知。

## 官方依据

- [RDS 原生性能查询：请求参数、Key、ValueFormat、Date 和 Unit](https://help.aliyun.com/zh/rds/developer-reference/api-rds-2014-08-15-describedbinstanceperformance)
- [完整性能参数表：MySQL_ROW_LOCK、PolarDBQPSTPS、PolarDBLongSQL](https://help.aliyun.com/zh/rds/developer-reference/performance-parameters/)
- [DescribeMetricMetaList：指标定义，不是时间序列](https://help.aliyun.com/zh/cms/cloudmonitor-1-0/developer-reference/api-cms-2019-01-01-describemetricmetalist)
- [DescribeMetricList：多实例查询、分页、额度](https://help.aliyun.com/zh/cms/cloudmonitor-1-0/developer-reference/api-cms-2019-01-01-describemetriclist)
- [MySQL 云监控指标目录](https://cms.console.aliyun.com/metric-meta/acs_rds_dashboard/rds)
- [PostgreSQL 云监控指标目录](https://cms.console.aliyun.com/metric-meta/acs_rds_dashboard/postgresql)
- [PostgreSQL 增强监控指标](https://help.aliyun.com/zh/rds/apsaradb-rds-for-postgresql/view-the-enhanced-monitoring-metrics-of-an-apsaradb-rds-for-postgresql-instance)
- [DAS 全量死锁分析及前提](https://help.aliyun.com/zh/das/user-guide/deadlock-analysis-das)
- [MySQL 官方实现：累计行锁等待次数、时间和平均值](https://github.com/mysql/mysql-server/blob/8.0/storage/innobase/srv/srv0srv.cc)
