# 华为云 API 对照与配置说明

本项目使用华为云官方 Python SDK，版本为 3.1.213，仅调用只读 API。
接口名称、响应字段、身份认证范围和分页方式已于 2026-09-08
对照公开的 SDK 源码核验。由于没有可用的真实云账号，尚未进行端到端验收测试。

| 采集器类型 | 官方 SDK 接口 | 使用的字段 | 默认采集频率 |
| --- | --- | --- | --- |
| `ecs_expiry` | 费用中心 BSS `ListPayPerUseCustomerResources` | `data[].resource_id`、`expire_time` | 每 24 小时 |
| `rds_expiry` | 云数据库 RDS `ListInstances` | `instances[].datastore.type`、`charge_info.charge_mode`、`expiration_time` | 每 24 小时 |
| `slb_expiry` | 费用中心 BSS `ListPayPerUseCustomerResources` | 同上方 ECS 到期采集字段，服务类型为 ELB | 每 24 小时 |
| `certificates` | 云证书管理 SCM `ListCertificates` | `certificates[].id`、`status`、`expire_time` | 每 24 小时 |
| `balance` | 费用中心 BSS `ShowCustomerAccountBalances` | `account_balances[].account_type`、`amount`、`currency` | 每 1 小时 |
| `packages` | 费用中心 BSS `ListFreeResourceInfos` | `expire_time`、`free_resources[].amount`、`original_amount` | 每 12 小时 |
| `metrics` | 云监控 CES `ShowMetricData` | `datapoints[].timestamp`，以及所选的 `max`/`average`/... 统计值 | ELB 每 1 分钟；OBS 流量每 1 小时；OBS 容量每 24 小时 |

默认调度沿用 Excel 的要求：容量和有效期每天检查一次，资源包每天检查两次。
如果存储容量增长较快，可以把容量检查提高到每小时一次。
ELB 流量每分钟采集一次，便于通过连续观测判断是否持续 5 分钟满足告警条件。
仅凭某个 5 分钟窗口内的最大值，无法证明整个窗口始终超过阈值。

## 密钥与权限

具体授权动作、全局与项目授权范围、可导入的 JSON 策略和 IAM 控制台操作步骤见 [华为云权限配置](huawei-permissions.md)。

- 安装 `huaweicloudsdkbss==3.1.213`、`huaweicloudsdkscm==3.1.213`、
  `huaweicloudsdkces==3.1.213`、`huaweicloudsdkrds==3.1.213`、`huaweicloudsdkecs==3.1.213`。
  配套的核心 SDK 会作为依赖自动安装。
- 账号配置项包括：`id`、`access_key_id`、`access_key_secret`、`regions`，以及可选的
  `domain_id`、`bss_region`（默认 `cn-north-1`）、`scm_region`（默认
  `cn-north-4`）、`timeout_seconds`（默认 20 秒）。
- BSS 和 SCM 使用全局凭据 `GlobalCredentials`；这里的区域用于选择服务接口地址，
  并不表示返回的所有资源都位于该区域。填写 `domain_id` 可以省去一次额外的 IAM 账号查询。
  本版本的 BSS 使用中国站接口地址 `https://bss.myhuaweicloud.com`。
  如需查询国际站账单，必须先单独核验其接口地址和 API。
- SDK 的“全局凭据”不等于 IAM 的“全局服务”授权范围。BSS 按官方要求需覆盖所有区域；
  SCM 授予全局服务。BSS 与 RDS/CES 可使用合并策略并授权到所有区域；如需把 RDS/CES
  限定到实际使用的区域项目，应分开授权。SCM 保持独立，具体文件和步骤见上面的权限配置文档。
- CES、RDS 和 ECS 名称查询使用项目级凭据 `BasicCredentials`。请在 `project_ids` 中，
  为每个需要查询的区域填写对应的 IAM 项目 ID，形成“区域 → 项目 ID”的映射。
  如果使用子项目 ID，必须与资源所属子项目一致。
- 请为采集账号授予以下只读权限：BSS 包年包月订阅、资源包和账户余额查询，
  RDS 实例列表查询，SCM 证书列表查询，以及 CES 监控数据查询。
  账单类 API 除了区域资源权限，还可能要求账号级权限。
  不要为采集账号授予购买、续费、删除或通知管理权限。
- 连接超时为 5 秒，读取超时可以配置，官方 SDK 会对连接失败重试两次。
  HTTP 或 API 调用失败时，会产生采集错误，包含接口名称和云厂商错误码，
  不包含原始请求数据。

ECS 网络监控使用 CES 发现实例后，通过 ECS `ListServersDetails`（`GET /v1/{project_id}/cloudservers/detail`）读取 `servers[].id/name`，每批按 `server_id` 精确查询最多 100 台，名称与清单一起缓存。该只读调用需要旧版 1.1 策略的 `ecs:cloudServers:list`；名称不可用时保留 ID 和流量采集并记录日志，见 [网络监控](network-traffic.md#资源发现)。显式实例清单直接使用配置的 `resource_name`，不调用此接口。

## 计费与到期时间口径

`ListPayPerUseCustomerResources` 的英文名称容易造成误解。
官方 SDK 将其描述为“查询包年/包月资源”，使用的请求路径为
`POST /v2/orders/suscriptions/resources/query`。
因此，本项目使用它查询预付费 ECS/ELB 订阅的到期时间。
运行中的按需计费 ECS/RDS/ELB 实例没有预付费订阅到期时间，系统不会为其虚构到期时间。

每个 ECS/ELB 到期采集任务只查询处于生效、冻结或过期状态的主资源。
默认的服务类型筛选值分别为 `hws.service.type.ec2` 和
`hws.service.type.elb`。如需使用其他已核验的计费目录代码，
可以覆盖 `options.service_type_code`。
使用特殊产品版本或站点时，请通过 BSS `ListServiceTypes` 确认当前账号适用的代码。

`options.resource_ids` 可以将每个任务的采集范围限制为明确列出的资源 ID，
每个任务最多指定 50 个 ID；数量更多时，请拆分为多个任务。
未设置该选项时，系统会自动遍历 API 的全部分页结果。
`options.regions` 用于限定返回的计费资源所在区域，未配置时使用账号的 `regions`。
如果明确指定的资源没有出现在响应中，任务会报告采集失败；
资源改为按需计费后导致查不到的情况，也会按此处理。

RDS 到期采集直接使用对应区域的 RDS API。
系统通过 `ListInstances.datastore_type` 分别查询 MySQL 和 PostgreSQL，
检查返回的 `datastore.type`，并且仅在 `charge_info.charge_mode` 为
`prePaid` 时使用 `expiration_time`。
明确标记为 `postPaid` 的实例属于按需计费，没有预付费到期时间，会跳过。
如果预付费实例缺少到期时间，或计费模式无法识别，任务会报告采集失败。
其他数据库引擎不在采集范围内。
`options.engines` 可选择 `[MySQL]`、`[PostgreSQL]`，或同时选择两者。
`options.regions` 会覆盖账号配置的区域；`options.enterprise_project_id`
用于限定企业项目范围；可选的 `options.resource_ids` 用于指定部分资源，
不受 BSS 的 50 个 ID 限制。实例列表自动分页，每次请求 100 条记录。
2026-10-20 新增 `network_traffic`，通过 CES 采集 ECS 总网卡和 RDS 总网络双向速率。ECS 为 5 分钟粒度的公网与内网合计，RDS 为分钟指标且可能延迟 5-10 分钟；本轮不包含华为云 CPU/内存等其他性能指标。自动发现、支持拓扑、单位换算和 P0/P1/P2 初始阈值见 [网络速率监控](network-traffic.md)。

余额默认使用 `options.account_type: 1`，即现金余额。
信用账户额度、奖励金和保证金不会计入现金余额，以免高估可用现金。
`amount` 按接口返回的货币单位直接使用（`measure_id: 1`）。
仅凭现金余额无法预测欠费停服时间：授信条件、欠款和专用资金等，
都可能影响账号实际的付款状态。

## 资源包

`options.service_type_codes` 默认值为 `[hws.service.type.obs]`。
可选的 `region`、`product_id` 和 `resource_ids` 分别用于筛选区域、
产品目录 ID，以及已购买的资源包实例 ID（`order_instance_id`）。
已购买实例的 ID 与可重复购买的目录产品 ID 不是同一种标识。

API 会分别返回每个资源包中的各项额度。`amount` 表示剩余额度，
`original_amount` 表示原始总额度，两者使用相同的 `measure_id` 计量单位。
采集器按每个 `free_resource_id` 分别报告剩余百分比和已用百分比，
不会将不同计量单位或不同用途的额度相加。
对于 `quota_reuse_mode: 1`，这些数值表示当前重置周期内的额度，
而不是资源包自购买以来的累计消耗。到期时间关联到已购买的资源包实例 ID。
采集范围包含生效、已用尽和已过期的资源包，忽略尚未生效和已退订的资源包。
历史已过期资源包仍会继续出现在结果中，直到缩小配置的采集范围，
或云厂商不再返回这些资源包。

如果缺少额度、总额度小于等于零，或缺少到期时间，任务会报告采集失败。
如果接口明确返回剩余额度为零，则属于有效的“额度已用尽”状态。

## 证书与域名

SCM 采集范围包括已签发、已上传和已过期的证书。
待处理订单不属于有效期已知的证书。
`options.resource_ids` 用于选择证书 ID；`options.region` 用于覆盖 SCM
服务接口所在区域；`enterprise_project_id` 可用于限定企业项目查询范围。
证书列表自动分页，每页使用接口支持的 50 条记录。

华为云 DNS 托管区域不等同于域名注册记录。
本版本不会将 DNS 托管区域的元数据视为域名注册到期信息。
目前尚未核验到可用的华为云域名注册到期 API，因此明确不支持 `domains`。
请使用域名注册商已核验的 API；如果域名注册在阿里云，
可以使用本项目已经实现的阿里云域名注册信息采集器。
SCM 有效期表示其管理的证书的有效期，不能证明公网网站正在使用同一张证书。

## CES 监控指标配置

`options.metrics` 中的每一项都必须填写目标资源在 CES 监控页面或 API
中对应的真实指标标识，包括按 API 顺序填写的全部维度。
“维度”用于确定指标对应的具体资源，例如负载均衡实例、监听器或存储桶。
具体取值会随 ELB 产品版本、资源范围和 OBS 指标类型而变化；
采集器不会自行假定某个账号级或存储桶级指标存在。

```yaml
options:
  metrics:
    - region: cn-north-4
      namespace: SYS.ELB
      metric_name: m7_in_Bps
      dimensions:
        lbaas_instance_id: CHANGE_ME_LOAD_BALANCER_ID
      resource_id: CHANGE_ME_LOAD_BALANCER_ID
      resource_name: production-load-balancer
      output_metric: bandwidth_used_percent
      period: 60
      filter: average
      multiplier: 8
      limit: 100000000
      lookback_seconds: 300
      max_age_seconds: 300
```

官方独享型 ELB 指标表将 `SYS.ELB/m7_in_Bps` 定义为入方向字节数/秒，
`m8_out_Bps` 定义为出方向字节数/秒，`mb_l7_qps` 定义为七层每秒查询次数。
在负载均衡实例级别，这些指标都只使用 `lbaas_instance_id` 一个维度。
监听器级别的指标还需要将 `lbaas_listener_id` 作为第二个维度；
可用区级别的指标可能使用 `available_zone`。
维度是指标身份的一部分，因此两个监听器的告警状态不会互相覆盖。
这些 ELB 指标每分钟发布一次。
采集任务应使用 `interval_seconds: 60`，需要持续超限的告警等级应使用
`for_seconds: 300`。重复观测同一个数据点不会增加持续时间；
观测间隔超过任务允许的间隔时，连续计时窗口会重置。

上述示例先将字节数/秒转换为比特数/秒，再与单独确认的已购买带宽上限
100 Mbit/s 比较。启用配置项前，请确认指标名称、单位和维度适用于你的 ELB 版本。
某个时间窗口内的字节总数不等于字节数/秒，必须使用适当的统计周期，
或通过 `multiplier` 进行单位换算。
对于本身已经是百分比的指标，请使用 `limit: 100` 和 `multiplier: 1`。

对于 `*_percent`，输出公式为 `value * multiplier / limit * 100`；
对于 `*_ratio`，输出公式为 `value * multiplier / limit`。
必须填写大于零的 `limit`。
将 OBS 已用存储量除以明确配置的存储预算，可映射为 `storage_used_percent`。
官方 OBS 容量指标为 `SYS.OBS/capacity_total`，单位是字节：
存储桶级别使用 `bucket_name` 这一个维度，账号级别使用 `tenant_id` 这一个维度。
这两种维度范围需要二选一，不是需要同时填写的两级维度。
云厂商每 30 分钟发布一次容量数据。
默认容量任务按照 Excel 的要求每 24 小时运行一次；同一配置项也支持按需改为每小时运行：

```yaml
options:
  metrics:
    - region: cn-north-4
      namespace: SYS.OBS
      metric_name: capacity_total
      dimensions:
        bucket_name: CHANGE_ME_BUCKET_NAME
      resource_id: CHANGE_ME_BUCKET_NAME
      output_metric: storage_used_percent
      period: 3600
      filter: max
      multiplier: 1
      limit: 1099511627776
      lookback_seconds: 10800
      max_age_seconds: 10800
```

这里的 `limit` 是人为设定的 1 TiB 存储预算，并非系统自动发现的容量硬上限。
OBS 对象存储在预付费资源包用尽后通常会继续按需计费，
因此资源包使用情况与存储桶容量预算是两类独立的告警。
ELB 的每秒查询次数（QPS）除以已确认的基准值或配额，可映射为 `qps_ratio`；
采集器不会自动学习基准值。

对于 OBS 流量，当前官方指标表提供以下每分钟更新的指标：

| `SYS.OBS` 下的指标名称 | 单位 | 存储桶级维度 | 含义 |
| --- | --- | --- | --- |
| `request_count_per_second` | 次/秒 | `bucket_name` | 总请求速率 |
| `download_bytes_extranet` | 字节/秒 | `bucket_name` | 公网下载带宽 |
| `download_bytes` | 字节/秒 | `bucket_name` | 总下载带宽 |
| `download_traffic_extranet` | 字节 | `bucket_name` | 下载字节总数，不是带宽速率 |

使用 `request_count_per_second` 时，配置 `output_metric: qps_ratio`、
`multiplier: 1`，并将已确认的 QPS 基准值填入 `limit`。
使用 `download_bytes_extranet` 时，配置 `output_metric: bandwidth_used_percent`、
`multiplier: 8`，并将已确认的带宽预算以比特数/秒为单位填入 `limit`。
默认 OBS 流量任务沿用 Excel 的每小时采集频率，不要求持续一定时长才告警。
每小时采集一次可能漏掉短时间的流量尖峰。
如果需要提高 OBS 采集频率，可以将任务设为每 60 秒运行一次，
并配置 `period: 60`、`filter: average`、`lookback_seconds: 300`
和 `max_age_seconds: 300`；需要判断持续 5 分钟的告警规则时，必须采用这种更高的采集频率。
这些监控值不表示预付费资源包的剩余额度，剩余额度应使用资源包采集器查询。

`period` 可选值为 1、60、300、1200、3600、14400、86400 秒。
`filter` 默认值为 `max`（最大值），也支持 `average`（平均值）、
`min`（最小值）、`sum`（总和）、`variance`（方差）。
`lookback_seconds` 表示向前查询数据的时间范围，默认至少覆盖三个统计周期，
且不小于 900 秒；`max_age_seconds` 表示允许的数据最大时间差，默认等于回溯范围。
系统使用返回结果中最新的数据点，并保留它的实际时间戳。
OBS 数据的过旧判定限制应考虑云厂商发布数据的延迟。
如果数据点缺失、过旧、不是有限数值，或时间戳位于未来，系统会报告采集错误，
不会因此清除已有告警。

## 核验资料

- 官方 Python SDK 代码仓库：https://github.com/huaweicloud/huaweicloud-sdk-python-v3
- BSS 客户端及请求、响应模型：
  https://github.com/huaweicloud/huaweicloud-sdk-python-v3/tree/master/huaweicloud-sdk-bss/huaweicloudsdkbss/v2
- SCM 客户端及证书模型：
  https://github.com/huaweicloud/huaweicloud-sdk-python-v3/tree/master/huaweicloud-sdk-scm/huaweicloudsdkscm/v3
- RDS `ListInstances` 请求和 `InstanceResponse` 响应模型：
  https://github.com/huaweicloud/huaweicloud-sdk-python-v3/tree/master/huaweicloud-sdk-rds/huaweicloudsdkrds/v3/model
- CES 支持的服务及指标维度：
  https://support.huaweicloud.com/api-ces/ces_03_0059.html
- OBS 指标表，已对照当前发布的 HTML 页面核验：
  https://support.huaweicloud.com/usermanual-obs/obs_03_0010.html
- 独享型 ELB 指标表，已对照当前发布的 HTML 页面核验：
  https://support.huaweicloud.com/usermanual-elb/elb_ug_jk_0001.html
- API 在线调试平台：https://apiexplorer.developer.huaweicloud.com/

测试使用官方 SDK 请求对象、模拟的只读接口响应，以及离线构建的客户端。
覆盖范围包括分页、API 查询范围、单位、额度用尽、证书状态、空资源列表、
格式异常的响应、过旧的监控数据，以及经过脱敏处理的 API 调用失败信息。
真实权限、已开通的产品版本、API 调用配额、实际数据延迟和通知是否真正送达，
仍需在部署时使用运维人员回填的凭据和通知目标进行验收。
