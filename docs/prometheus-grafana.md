# Prometheus 指标与 Grafana 自定义面板

本项目继续负责云 API 采集、MySQL 存储和告警通知，新增标准 `GET /metrics` 接口供你已有的 Prometheus 抓取。Grafana 面板使用 Prometheus 数据源，无需给 Grafana 配置本项目 MySQL 账号，也无需新增业务数据库账号。

```text
阿里云 / 华为云只读 API
         |
         | 现有任务的 interval_seconds
         v
cloud-monitor -----> MySQL samples 明细（默认 30 天）
         |           MySQL latest_samples 最新样本
         |                        |
         |                        v
         |                   GET /metrics
         |                        |
         |                        | 继承现有 Prometheus 抓取频率
         |                        v
         |                 已有 Prometheus（200h）
         |                        |
         |                        v
         |                 已有 Grafana 自定义面板
         v
现有告警引擎 -> 独立通知网关 -> 已启用的通知渠道
```

## 数据保留

| 存储 | 默认保留 | 用途与清理方式 |
| --- | --- | --- |
| MySQL `samples` | 30 天 | 保存每条采集结果的值、单位、源观测时间、维度及采集器保留的明细；按任务、指标身份、源观测时间去重 |
| MySQL `latest_samples` | 每个任务、指标身份一条最新样本 | `/metrics` 读取此表，避免每次抓取扫描历史；旧源时间不会覆盖较新样本，超出明细保留期也会清理 |
| MySQL 回执和已结束通知 | 30 天 | 清理旧短信回执与 `sent`、`cancelled`、`skipped` 通知 |
| MySQL 当前状态、待发与失败通知 | 不按历史明细保留期删除 | 保留告警连续性、去重状态与失败排查依据；`pending`、`failed` 不因超期丢弃 |
| Prometheus 时序数据 | 200 小时，即 8 天 8 小时 | 由现有 Prometheus 的 TSDB 保留配置管理 |

MySQL 采用现有的 `service.retention_days: 30`。保留 30 天便于核查月内的告警和采集明细；Grafana 近期趋势使用 200 小时的 Prometheus 数据。清理在监控服务启动时运行，此后每 24 小时执行，因此正常持续运行时数据最老可能接近 31 天；服务停机期间不会清理。更改保留天数会在下一次清理时生效，缩短保留期会删除超期明细。

粗略容量估算为：`序列数 × 86400 ÷ 源采样间隔秒数 × 保留天数`。例如 100 条序列每分钟新增一个源样本，30 天约 432 万条明细。相同源样本被多轮 API 查询返回只保存一次；API 一次返回多个新时间点时按各时间点保存。实际磁盘量还包含 JSON 明细、索引和 MySQL 开销，按运行后的表大小估算，不能只按数值本身的 8 字节计算。

Prometheus 通过启动参数配置：

```text
--storage.tsdb.retention.time=200h
```

该参数作用于整个 Prometheus 实例。已有 `200h` 则保留；否则在现有 systemd、Docker 或 Kubernetes 启动参数中添加或替换这一个参数，保留其他参数和原有抓取频率。不要写进 `scrape_configs`，也不要为此覆盖现有完整启动命令。若同时设置了 `--storage.tsdb.retention.size`，达到容量上限可能更早删除数据；保留 200 小时需要足够存储空间。

## 指标入口

接口复用已有采集服务 HTTP 端口，默认启用：

| Prometheus 的位置 | 抓取地址 |
| --- | --- |
| Prometheus 进程运行在监控服务宿主机 | `http://127.0.0.1:18080/metrics` |
| Prometheus 容器与监控服务处于同一 Docker 网络 | `http://cloud-monitor:8080/metrics` |
| 另一台服务器 | `http://监控服务器内网IP:18082/metrics`，使用下文受限反向代理 |

Compose 保持 `127.0.0.1:18080:8080`。Prometheus 容器里的 `127.0.0.1` 指向它自身，不能直接用来访问宿主机的监控服务；同机独立容器需要加入同一个 Docker 网络，或使用可达的受限代理地址。

每次抓取只读取本项目 MySQL 最新样本及状态，不调用云 API，不增加云端 API 费用，不触发告警发送。接口只输出有限的资源身份标签，不包含 AK/SK、通知凭据、手机号、完整 API 原始响应或任意明细字段。资源名称、账号标识及余额仍属于内部运维数据，按内网接口部署。

可在主配置已有 `api` 节点内合并以下可选设置：

```yaml
api:
  metrics:
    enabled: true
    token: ${METRICS_TOKEN}
```

`token` 不填写时不认证；填写时必须是至少 24 字符的随机字符串，接口只接受 `Authorization: Bearer TOKEN`，不接受查询串 token。使用环境变量时，在现有 Compose 的 `cloud-monitor.environment` 中增加 `METRICS_TOKEN: ${METRICS_TOKEN}`，并在服务器环境或 `.env` 中设置同名值；变量为空会在配置检查时失败。将同一个 token 放入 Prometheus 可读的独立凭据文件，在下文抓取配置中启用 `authorization.credentials_file`。这不是管理接口或通知网关 token，不要复用这些凭据。

改为 `enabled: false` 会使 `/metrics` 返回 404。读取数据库失败返回 503，Prometheus 的 `up` 为 0；采集任务失败时 HTTP 抓取仍可成功，任务错误通过指标独立呈现。

### 跨服务器代理

使用 [prometheus/nginx-metrics.conf](../prometheus/nginx-metrics.conf)，将示例 IP `192.0.2.10` 改为监控服务器内网地址，将 `192.0.2.20` 改为代理实际看到的 Prometheus 来源 IP。把 `server` 块放进 Nginx 的 `http` 配置上下文，执行 `nginx -t` 通过后按你的部署方式重新加载 Nginx；主机防火墙也仅允许 Prometheus 来源访问 18082。

该示例只代理精确路径 `/metrics`，其他路径返回 404，不能通过它访问 `/status`、短信测试或回执接口。示例适用于可信内网；跨非可信网络时在代理上配置 TLS，抓取任务改为 `scheme: https` 并信任对应证书。若 Nginx 自己在容器内，`proxy_pass` 应使用同一 Docker 网络内的 `http://cloud-monitor:8080/metrics`，不能使用代理容器自身的 `127.0.0.1`。

## 接入现有 Prometheus

将 [prometheus/scrape-config.yml](../prometheus/scrape-config.yml) 中的任务条目合并进现有 `scrape_configs` 列表，并按上一节选择正确地址。同名任务只保留一个，避免重复抓取。文件特意不写 `scrape_interval` 和 `scrape_timeout`，继承你当前的全局配置：

```yaml
scrape_configs:
  - job_name: cloud-resource-monitor
    metrics_path: /metrics
    scheme: http
    static_configs:
      - targets: ['127.0.0.1:18080']
    # 已配置 api.metrics.token 时启用：
    # authorization:
    #   type: Bearer
    #   credentials_file: /etc/prometheus/secrets/cloud-monitor-token
```

凭据文件中只放 token 本身，不加 `Bearer ` 前缀；容器部署时挂载到 Prometheus 容器内，并限制文件读取权限。按既有流程先检查完整配置 `promtool check config /实际路径/prometheus.yml`，再重新加载 Prometheus。改动 TSDB 启动参数需要重启 Prometheus，仅重新加载 YAML 不会改变保留期。

在 Prometheus Targets 中确认 `cloud-resource-monitor` 为 UP，再查询：

```promql
up{job="cloud-resource-monitor"}
cloud_monitor_scheduler_alive{job="cloud-resource-monitor"}
cloud_monitor_resource_value{job="cloud-resource-monitor"}
```

`up=1` 表示抓取成功，`cloud_monitor_scheduler_alive=1` 表示采集调度心跳正常。任务是否成功、样本是否新鲜需要看下面的独立指标。

## 指标口径

2026-10-20 新增 [ECS/RDS 网络速率](network-traffic.md)，使用 `metric="network_in_bps"` / `network_out_bps`、`unit="bit/s"`，直接画 Gauge，不用 `rate()`。新分区按 `product` 和 `scope` 分开显示 ECS 内网、ECS 总网卡、RDS 总吞吐，旧面板 ID 保留。阿里云 ECS 的 `scope=intranet`，华为云 ECS 和两云 RDS 为 `scope=all`；图中三档速率线与独立配置对应，告警持续时间按各云来源区分。

2026-09-11 新增 [七牛、汇付、爱签和微信小程序](external-platforms.md) 采集入口，仍使用下述 `cloud_monitor_resource_value{metric="..."}` 统一格式；新增来源、币种、账期、SPU、包 ID 标签均来自有限白名单，原始响应/签名/token 不导出。Grafana JSON 已增加五组相关分区，需要重新导入原 UID 面板。

平台查询额外暴露 `api_query_success`、`api_query_duration_seconds` 与 `api_last_success_timestamp_seconds`。最后成功时间标记即使超过普通业务新鲜度也保留导出，失败不会刷新它；`resource_fresh` 仍显示其实际年龄，其余过期业务值继续隐藏。这样可以区分查询正常但业务异常、查询失败及源数据过旧。

所有 `cloud_monitor_*` 指标当前都是 Gauge。Prometheus 的抓取时间是时序采样时间，云 API 返回的源观测时间单独暴露为 `cloud_monitor_resource_observed_timestamp_seconds`。没有向 Prometheus 样本写入旧显式时间戳，也不会将 MySQL 历史自动回放到 Prometheus。

| 指标 | 含义 |
| --- | --- |
| `cloud_monitor_resource_value` | 最新且有效的业务采集值；通过 `metric` 和 `unit` 区分余额、天数、百分比、速率等 |
| `cloud_monitor_resource_observed_timestamp_seconds` | 最新持久化样本的源观测时间，Unix 秒 |
| `cloud_monitor_resource_fresh` | 样本仍在有效期内且最近任务状态不是 `no_data` 或 `insufficient_data` 时为 1，否则为 0；短信日统计还要求最近持久化任务状态为 `ok` |
| `cloud_monitor_job_status` | 每个已启用任务的状态；`ok`、`no_data`、`insufficient_data`、`error`、`stale`、`waiting` 中当前状态为 1，其余为 0 |
| `cloud_monitor_job_last_run_timestamp_seconds` | 最近一次采集尝试时间，Unix 秒 |
| `cloud_monitor_job_interval_seconds` | 该任务配置的采集间隔 |
| `cloud_monitor_job_samples` | 最近一次采集返回的样本数，不是累计计数 |
| `cloud_monitor_scheduler_alive` | 调度服务已启动且心跳在 120 秒内为 1，否则为 0 |
| `cloud_monitor_active_alert` | 现有告警引擎尚未恢复的告警，每条为 1；包含规则、资源、等级标签 |
| `cloud_monitor_alerts` | 已启用任务按云厂商、账号、P0/P1/P2 汇总的未恢复告警数量 |
| `cloud_monitor_outbox_messages` | 当前服务数据库按状态统计的通知队列条数，覆盖全部账号 |

资源标签包含 `provider`、`account`、`job_id`、`resource_id`、`resource_name`、`metric`、`unit`，以及允许的地区、产品、引擎、命名空间与节点身份。`metric_key` 区分同一资源不同维度的序列。Prometheus 会再添加抓取任务 `job` 与服务地址 `instance`；这两个标签不等于云采集任务 `job_id` 或云资源 ID。

百分比使用源值 **0 到 100**；`qps_ratio` 使用 **0 到 1**，例如 0.8 表示预算的 80%，超过预算可大于 1。余额按 `unit` 的币种分别展示，不合计不同币种。每秒慢查询、行锁等待和 PG 死锁已经是速率，直接画值，不再使用 `rate()` 或 `irate()`。原 MySQL `db-deadlocks` 小时规则继续单独保留，行锁等待不能当作死锁。

例如查看源样本年龄：

```promql
time() - cloud_monitor_resource_observed_timestamp_seconds{job="cloud-resource-monitor"}
```

数据超出任务的 `max_age_seconds` 后不再暴露 `resource_value`，仍保留源时间与 `resource_fresh=0` 用于排查，不输出伪造的零值。未显式设置时，RDS 原生性能和 RDS 云监控为 300 秒，其他任务为 `max(900, 2 × interval_seconds)`。源时间超过当前时间 60 秒也视为不新鲜。

最近采集状态为 `no_data` 或 `insufficient_data` 时立即隐藏业务值，并将旧样本标为 `resource_fresh=0`。阿里云短信日统计分页完整且接口成功，但缺少计算所需日期，或前七日为零而昨日有发送量时，为 `insufficient_data`；它不是 API 采集失败，也不表示历史短信业务告警已经恢复。

短信日统计采用更严格的当前值策略：只有最近持久化任务状态为 `ok`，且样本仍在有效期内，才导出增长率。`no_data`、`insufficient_data` 和真实采集 `error` 都会隐藏旧增长率并令其 `resource_fresh=0`；即使先统计不足、下一轮接口失败，也不会重新显示旧值，直到新的完整有效统计产生指标。其他任务遇到 API 失败时立即为 `error`，此前尚在有效期内的样本仍可继续显示，达到有效期后消失；因此其他业务曲线仍有值不代表最近一次采集成功。任务超过 `2.5 × interval_seconds + 60` 秒没有运行记录时变为 `stale`。Grafana 图表保持缺口，不把无数据补零或跨缺口连线。

项目面板把 `insufficient_data` 映射为橙色“统计不足”，并计入“异常 / 待采集任务”。`/readyz` 将它与 `no_data` 一样视为查询可用，不因统计不足单独返回未就绪；就绪检查反映采集服务健康，不保证业务统计完整。统计不足详情通过受鉴权保护的 `/status`、任务日志或只读 `diagnose` 查看，不作为高基数 Prometheus 标签导出。

## 导入和调整 Grafana 面板

面板文件：[grafana/cloud-resource-monitor.json](../grafana/cloud-resource-monitor.json)。布局预览：[preview/grafana.html](../preview/grafana.html)，使用虚构数据，仅用于查看面板布局。

1. 在现有 Grafana 进入 Dashboards → New → Import，上传面板 JSON 并确认导入。
2. 打开面板，在顶部 `Prometheus` 下拉框（变量 `DS_PROMETHEUS`）选择现有 Prometheus 数据源。它是面板变量，无需依赖导入时的数据源映射提示。
3. 顶部 `采集服务` 下拉框选择一个 `instance`，再按云厂商、账号和资源筛选。`instance` 为单选，避免把多个采集服务的队列和健康状态混合。
4. 选择时间范围查看趋势。面板默认展示近期数据，可调整到最近 200 小时；超出 Prometheus 保留期的区间没有历史数据。
5. 后续可直接在 Grafana 编辑 PromQL、布局、单位、阈值和变量并保存。需要维护项目交付文件时，同步修改 `scripts/build_grafana_dashboard.py` 并执行 `python scripts/build_grafana_dashboard.py`，使生成器和 JSON 保持一致；仅覆盖 JSON 会使一致性检查失败。

表格标签（资源 ID、账号、资源包 ID、账期等）统一使用 `String` 单位，保留纯数字文本、前导零和长 ID；仅给重命名后的 `Value` 数值列设置测量单位。到期表的“资源 ID”是云资源标识，阿里云 SSL 对应云 API 返回的证书 `CertificateId`；“剩余天数”来自独立的到期指标，只有该列带“天”，并保留升序排列与到期阈值颜色。余额表仅“可用余额”使用人民币单位。

如果旧仪表盘显示 `12154010 天` 这样的资源 ID，原因是整张到期表继承了“天”单位。重新导入本项目更新后的 JSON，覆盖同一 UID `cloud-resource-monitor` 的仪表盘即可生效，无需重新采集或重启服务。也可在 Grafana 编辑该表，将默认 Unit 设为 `String`，再为“剩余天数”添加字段覆盖（Unit 为自定义 `suffix:天`），保留既有排序与颜色覆盖。

面板将重要数据组织为采集与告警总览、余额与到期、额度与容量、RDS 核心性能、MySQL 锁等待与慢查询、PG 死锁与长 SQL、任务与数据新鲜度。通知队列属于所选采集服务的全局状态，不随云厂商、账号或资源筛选变化；任务状态按账号筛选，无法按单个资源拆分。余额专用面板明确筛选 `unit="CNY"`，其他币种在全部指标明细中保留，需要时可复制余额面板并替换币种与单位。告警数量来自原有告警状态，图表色阶只是辅助展示，不会替换原有规则、持续时间判断或通知路由。

当前 `config/rds-performance.yaml` 的五个性能任务仍为关闭状态。相关图表在你明确启用任务并成功采集前显示无数据；尤其 PG 还需要实际实例信息与账号权限。面板不会为演示自动开启任务，不会生成真实环境的示例值。原有有效期任务按现有开关运行。

首次升级会自动建 `latest_samples` 表，已有历史明细保留。该表在后续原有采集调度中填充，不会启动时扫描历史表回填；重启后已有最新样本会保留并继续按新鲜度判断。Prometheus 历史从首次抓取开始，因此上线前的 200 小时不会立即出现在 Grafana。

## 部署与离线验证

所有源文件、示例与面板都在本项目内。更新服务器时保留现有 `.env`、主配置、账号和通知凭据，在服务器当前项目目录执行：

```sh
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check
docker compose up -d --no-deps cloud-monitor
```

上述最后一步是实际部署操作，会启动或重建现有采集调度和通知分发，安排在你的部署窗口执行。本次开发验证不执行它，也不发送真实通知。云采集和通知供应商的配置、账号和任务开关不需要因指标接入而改变。

本地离线检查：

```sh
python -m pytest -q
python -m cloud_monitor --config config/config.yaml check
python scripts/build_grafana_dashboard.py --check
python scripts/check_observability.py
```

`check_observability.py` 需要可用的 Docker 和本地已有的 `prom/prometheus:v3.13.0` 镜像，也可通过 `--image` 指定本地镜像。它以 `--pull=never --network none` 运行 `promtool`，验证抓取配置、面板查询和虚构样本的指标格式，不启动 Prometheus 服务、不连接生产数据库、不实际抓取。也可使用本机 `promtool check config prometheus/scrape-config.yml` 单独检查抓取配置。

本次已通过 Prometheus 3.13.0 的抓取配置检查、面板及变量查询的语法检查和指标格式校验。MySQL 集成测试只能使用独立临时测试服务器，不能配置成已有业务数据库。部署后从 Prometheus Targets 与 Grafana 检查实际接入结果。
