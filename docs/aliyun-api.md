# 阿里云采集接口说明

阿里云采集器通过只读 API 获取数据，不会自动续费、购买资源包、修改云监控配置或下载证书私钥。采集频率和告警规则以项目配置文件为准；本文主要说明阿里云各类采集任务的 `options` 参数、接口字段及使用限制。

## 账号配置

具体授权动作、可导入的 JSON 策略和 RAM 控制台操作步骤见 [阿里云权限配置](aliyun-permissions.md)。

```yaml
id: aliyun-production
provider: aliyun
access_key_id: ${ALIYUN_ACCESS_KEY_ID}
access_key_secret: ${ALIYUN_ACCESS_KEY_SECRET}
regions: [cn-hangzhou, cn-shanghai]
timeout_seconds: 20
```

应用读取配置时，会把示例中的环境变量替换为实际凭据。采集器使用 `aliyun-python-sdk-core`，在首次请求时才创建客户端。所有 API 请求使用 HTTPS，并设置请求超时，避免无限等待。

费用中心使用 `business.aliyuncs.com`，云监控使用 `metrics.<region>.aliyuncs.com`。如果使用其他受支持的阿里云站点，可以通过 `account.endpoints` 按服务覆盖访问地址，例如 `bssopenapi`、`cas` 或 `cms`。需要指定地区的任务可配置 `options.region`；未指定时，ECS 和 RDS 会遍历 `account.regions` 中列出的地区。

建议为采集服务单独创建 RAM 身份，仅授予已启用服务所需的查询权限。本文末尾的官方接口文档列出了各操作需要的 RAM 权限。费用和账号查询可能还需要账号级费用权限；部分合作伙伴托管账号或国际站账号不一定支持相同接口。单个账号的访问密钥不会自动获得其他账号的资源访问权限。

## 采集类型与接口字段

| 采集类型 | 官方 API | 使用字段与处理方式 |
| --- | --- | --- |
| `ecs_expiry` | Ecs `2014-05-26` `DescribeInstances` | 读取 `Instances.Instance[].ExpiredTime`；跳过 `InstanceChargeType=PostPaid` 的按量付费实例；按地区分页查询 |
| `rds_expiry` | Rds `2014-08-15` `DescribeDBInstances` | 读取 `Items.DBInstance[].ExpireTime`；默认仅查询 `MySQL` 和 `PostgreSQL`；跳过 `PayType=Postpaid` 的按量付费实例 |
| `domains` | Domain `2018-01-29` `QueryDomainList` | 优先读取毫秒时间戳 `Data.Domain[].ExpirationDateLong`；缺少该字段时读取 `ExpirationDate`，按北京时间解释 |
| `certificates` | cas `2020-04-07` `ListUserCertificateOrder`, `GetUserCertificateDetail` | 查询 `OrderType=CERT`，分别获取有效证书与 `EXPIRED` 证书；读取准确到期时间 `NotAfter`；设置 `CertFilter=true`，不返回私钥 |
| `packages` | BssOpenApi `2017-12-14` `QueryResourcePackageInstances` | 读取 `Data.Instances.Instance[]` 中的 `ExpiryTime`、`TotalAmount`、`RemainingAmount`，以及总量和余量各自的单位字段 |
| `slb_expiry` | BssOpenApi `2017-12-14` `QueryAvailableInstances` | 使用 `ProductCode=slb`、`SubscriptionType=Subscription` 筛选；读取 `Data.InstanceList[].EndTime` 和 `InstanceID` |
| `balance` | BssOpenApi `2017-12-14` `QueryAccountBalance` | 默认读取 `Data.AvailableAmount` 和 `Currency`；也可配置为仅查询现金余额 `AvailableCashAmount` |
| `metrics` | Cms `2019-01-01` `DescribeMetricList` | 根据准确的命名空间、指标、维度、统计字段和周期查询；分页解析 JSON 格式的 `Datapoints`，保留最新数据点的实际 `timestamp` |
| `network_traffic` | ECS/RDS 实例列表 + CMS `DescribeMetricMetaList` / `DescribeMetricList` | 发现或显式选择实例，校验单位/周期/维度，输出 ECS 内网或 MySQL 总网络双向 `bit/s` |
| `sms_statistics` | Dysmsapi `2017-05-25` `QuerySendStatistics` | 读取 `Data.TargetList[].SendDate` 和 `TotalCount`；将昨日发送量与此前七个完整自然日的均值比较 |

2026-10-20 新增独立 `network_traffic` 任务，覆盖 ECS 内网速率和 RDS MySQL 总网络吞吐；范围、指标和三档初始告警线见 [网络速率监控](network-traffic.md)。RDS 原有效期任务不变，2026-09-10 新增 `rds_performance` 原生性能批量查询与 `rds_cloudmonitor` 云监控补充任务，只使用云 AccessKey，不需要业务数据库账号。MySQL 慢查询使用 `MySQL_SlowQueries`，PostgreSQL 缓存命中率使用 `PG_SharedBufferHitRate`；具体指标、未覆盖条件、权限和配置见 [RDS 性能监控](rds-performance.md)。SLB 的资源列表接口没有包年包月到期字段，因此改用费用中心的资源清单查询。按量付费负载均衡没有包年包月到期时间，不会人为生成到期日期。

当前带宽配置对应 CLB/SLB；ALB、NLB 需要使用各自的监控命名空间和维度。

## 资源筛选与额度处理

### 指定监控资源

`ecs_expiry`、`rds_expiry` 和 `slb_expiry` 均支持使用 `resource_ids` 列表限定资源。`rds_expiry` 还支持 `engines`，默认值为 `[MySQL, PostgreSQL]`。

`domains` 使用 `domain_names` 筛选域名；`certificates` 使用 `certificate_ids` 筛选证书。建议填写目前仍在使用的证书 ID，排除已经替换、但仍保留在云平台中的历史证书。

### 资源包参数与单位

`packages` 必须填写费用中心实际使用的 `product_code`，还支持以下参数：

- `package_ids`：仅采集指定资源包。
- `product`：供告警规则使用的统一产品分类。
- `expiry_only: true`：用于没有可衡量总额度的套餐，仅检查有效期。

OSS 资源包通常使用产品代码 `oss`。其他资源包的产品代码需要根据 `QueryProductList` 或费用中心资源清单核实，不能用商品代码、资源包类型或页面展示的产品名称替代。云平台保留旧资源包时，可以通过 `package_ids` 限定仍需要监控的资源包。

总量与余量的单位必须一致或能够换算。字节、KB、MB、GB、TB 按 1024 进制换算；其他单位仅在名称一致时进行比较。以下情况会报告采集错误，不会猜测额度：缺少额度字段、总量为零或负数、单位无法匹配、余量大于总量。

明确返回的余量为零属于有效数据，表示额度已耗尽，对应 `package_remaining_percent=0`、`package_used_percent=100`。

### 余额口径与币种

`balance` 默认使用 `balance_field: AvailableAmount`，表示可用余额；也可使用 `AvailableCashAmount`，只查询现金余额。`currency` 默认为 `CNY`。如果接口返回其他币种，会报告错误，避免把人民币阈值直接用于其他币种的金额。

## 云监控指标与单位换算

```yaml
options:
  region: cn-hangzhou
  product: oss
  metrics:
    - namespace: acs_oss_dashboard
      metric_name: MeteringStorageUtilization
      dimensions:
        BucketName: REPLACE_WITH_BUCKET_NAME
      resource_id: REPLACE_WITH_BUCKET_NAME
      resource_name: production-assets
      output_metric: storage_used_percent
      period: 3600
      aggregation: Average
      limit: 1099511627776
      multiplier: 1
      max_age_seconds: 10800
```

以上示例配置了 1 TiB 存储预算。阿里云官方控制台的公开代码将 `MeteringStorageUtilization` 标识为存储容量，单位为字节。示例中的统计周期和聚合字段仍需要根据实际指标元数据确认。

“指标元数据”就是云平台对该指标名称、维度、统计方式、上报周期和单位的定义。可以通过 `DescribeMetricMetaList` 或以下官方指标目录查询：

- [OSS 指标目录](https://cms.console.aliyun.com/metric-meta/acs_oss_dashboard/oss)
- [SLB 指标目录](https://cms.console.aliyun.com/metric-meta/acs_slb_dashboard/slb)
- [云产品监控指标索引](https://help.aliyun.com/document_detail/163515.html)

对应命名空间分别为 `acs_oss_dashboard` 和 `acs_slb_dashboard`。存储桶预算、QPS 基线和已购买带宽上限都需要根据实际业务填写。资源包的购买额度不等于存储桶的物理容量上限，采集器不会将两者混用。

[阿里云官方控制台公开代码](https://g.alicdn.com/cloudmonitor/cms-subapp-all/2.12.85/index.js)中还列出了 OSS 的 `TotalRequestCount`（总请求次数）、`InternetSend`（公网流出流量），以及 SLB 的 `TrafficRXNew` / `TrafficTXNew`（监听器入方向、出方向带宽）。

`TotalRequestCount` 不能直接当作 QPS。需要先核对周期和统计方式，再用 `multiplier` 把周期内请求总数除以周期秒数，换算成每秒请求数。`InternetSend` 的含义是流量，不能直接认定返回值已经是每秒比特数。不要使用尚未核实的 `TotalRequestQps` 指标名称。

### 必填参数与默认值

每个 `options.metrics` 项都需要填写 `namespace`、`metric_name`、非空且准确的 `dimensions`、`resource_id`、`output_metric`。归一化输出还需要大于零的 `limit`，新增 `network_in_bps` / `network_out_bps` 原始速率输出免填 `limit`，换算后的单位固定为 `bit/s`。每项对应一个资源的一条指标序列；网络任务优先使用自动核对源定义的 `network_traffic`。

`resource_name`、`region` 和 `product` 为可选参数。`period` 默认 60 秒，`aggregation` 默认 `Average`，`multiplier` 默认 1。`max_age_seconds` 是允许的数据最大陈旧时间，默认取 300 秒和三个上报周期中的较大值。

支持的统计字段名称为 `Average`、`Maximum`、`Minimum`、`Sum` 和 `Value`。所选字段必须实际存在于接口响应中，不能仅因为配置支持就认定该指标也支持。

### 计算公式

- `storage_used_percent = raw * multiplier / limit * 100`
- `bandwidth_used_percent = raw * multiplier / limit * 100`
- `qps_ratio = raw * multiplier / limit`

其中原始值先乘以换算系数，再除以你配置的预算或上限。容量和带宽输出百分比；QPS 输出相对基线的倍数。

如果原始数据为每秒字节数，而带宽上限使用每秒比特数，配置 `multiplier: 8`。如果原始数据是一个周期内累计的字节数，应按 `multiplier: 8 / period_seconds` 计算换算系数，再把计算结果作为 YAML 数值填写，不能直接填写算式。原始数据已经是每秒比特数时，使用 `multiplier: 1`。

SLB 入方向和出方向带宽应分别配置指标项，各自填写源指标名称和上限。QPS 比值为 2 表示达到基线的 200%，即两倍；“比基线增长 200%”则表示达到三倍，二者不能混淆。

数据过期、时间在未来、返回多条无法区分的序列、响应格式错误、缺少统计字段或没有监控数据时，采集器都会报告错误，不会用一个新的零值替代缺失数据。分页失败同样会报告错误，避免把不完整的资源清单当作完整结果。

## 短信日发送量

```yaml
options:
  is_globe: 1
  timezone_offset_hours: 8
  # sign_name: YOUR_SMS_SIGNATURE
  # template_type: 1
```

`is_globe` 为 1 时查询国内短信，为 2 时查询国际短信；`sign_name` 和 `template_type` 可以进一步缩小查询范围。统计时将最近一个完整自然日的发送量，与此前七个完整自然日的平均发送量比较：

`sms_growth_percent = (latest_day / prior_seven_day_average - 1) * 100`。

默认按 UTC+08:00 划分自然日。指标附带日期、发送量和基线信息，便于核对。由于云平台可能继续修正日报，指标的观测时间使用本次查询时间。

计算需要完整的八天数据，并先确认所有分页成功返回。接口成功返回完全空的统计列表时，任务显示 `no_data`；返回部分日期、但缺少窗口内其他日期时，显示 `insufficient_data`（统计不足），安全详情列出缺失日期。两种状态均不产生增长率指标，不会自行认定缺失日期没有发送。如果八天发送量都明确为零，增长率为零；如果此前七日均值为零、但最近一天有发送量，则显示 `insufficient_data`，原因是零基线无法计算百分比增长。新开通的短信业务可人工核对，或另设按绝对发送量判断的规则。

`insufficient_data` 表示接口查询成功而统计条件不足，不再触发“请检查权限和接口”的采集失败告警；已有采集失败告警可在连续两次正常查询结果后恢复（默认 `recovery_samples: 2`，统计不足也计入），不代表短信增长率已经恢复正常。历史短信业务告警保持，直到得到有效的业务指标再按规则判断。旧增长率样本会立即停止作为当前值导出，Grafana 任务状态显示橙色“统计不足”；`diagnose` 返回 `status: insufficient_data`、`samples: 0` 与安全原因，退出码为 0。分页未完成、接口失败、非法日期或数值等异常仍为 `error`，不会被归为统计不足。

短信日统计的当前增长率还要求最近持久化任务状态为 `ok` 且样本仍新鲜。`no_data`、`insufficient_data` 或真实 `error` 都隐藏旧增长率；统计不足之后发生接口错误，不会重新暴露此前的有效值。只有新的完整有效统计返回后，才重新导出当前增长率。其他任务的接口错误仍可保留尚未过期的旧样本。

该 API 返回的日回执总量，不能证明短信成功率是否连续 5 分钟或 10 分钟偏低。分钟级成功率由应用中独立的回执接收接口和时间窗口采集器处理，回执接口需要鉴权。本采集器仅输出日发送量增长率。

## 核验来源与限制

请求和响应字段已于 2026-09-08 对照阿里云发布在 PyPI 上的自动生成 SDK 模型核验，参考版本如下：

- `alibabacloud_ecs20140526==7.11.1`
- `alibabacloud_rds20140815==15.9.1`
- `alibabacloud_bssopenapi20171214==5.1.1`
- `alibabacloud_domain20180129==5.1.0`
- `alibabacloud_cas20200407==3.9.1`
- `alibabacloud_cms20190101==4.0.10`
- `alibabacloud_dysmsapi20170525==4.6.0`

这些 SDK 仅用于核对接口定义，不是项目运行时需要额外安装的依赖；运行时使用通用 RPC SDK。

官方接口文档：

- [查询 ECS 实例：DescribeInstances](https://api.aliyun.com/api/Ecs/2014-05-26/DescribeInstances)
- [查询 RDS 实例：DescribeDBInstances](https://api.aliyun.com/api/Rds/2014-08-15/DescribeDBInstances)
- [查询域名列表：QueryDomainList](https://api.aliyun.com/api/Domain/2018-01-29/QueryDomainList)
- [查询证书列表：ListUserCertificateOrder](https://api.aliyun.com/api/cas/2020-04-07/ListUserCertificateOrder)
- [查询证书详情：GetUserCertificateDetail](https://api.aliyun.com/api/cas/2020-04-07/GetUserCertificateDetail)
- [查询资源包实例：QueryResourcePackageInstances](https://help.aliyun.com/zh/user-center/developer-reference/api-bssopenapi-2017-12-14-queryresourcepackageinstances)
- [查询可用计费实例：QueryAvailableInstances](https://api.aliyun.com/api/BssOpenApi/2017-12-14/QueryAvailableInstances)
- [查询账号余额：QueryAccountBalance](https://api.aliyun.com/api/BssOpenApi/2017-12-14/QueryAccountBalance)
- [查询监控数据：DescribeMetricList](https://api.aliyun.com/api/Cms/2019-01-01/DescribeMetricList)
- [查询短信发送统计：QuerySendStatistics](https://api.aliyun.com/api/Dysmsapi/2017-05-25/QuerySendStatistics)

自动化测试使用模拟响应及实际通用 SDK 请求对象，覆盖分页、资源范围、到期时间精度、额度单位换算、零余量、余额币种、最新或过期监控数据、短信自然日边界、异常数据、HTTPS 地址和接口业务错误。

访问密钥留待回填，因此尚未执行真实云账号采集。上线时仍需核对实际账号权限、接口返回值及业务配置。
