# 阿里云自定义权限配置

核验日期：2026-09-09。以下权限对应本项目当前 `cloud_monitor/providers/aliyun.py` 和 `cloud_monitor/notifiers.py` 实际调用的接口，已逐项对照阿里云官方 API 文档的“授权信息”表。

## 先明确账号和权限范围

每个阿里云主账号分别创建一个专用于本服务的 RAM 用户，再把该用户的 AccessKey ID / AccessKey Secret 填入 `config/config.yaml` 对应的 `accounts` 配置项。`aliyun-main`、`aliyun-secondary` 是程序内的账号标识，不是云平台自动创建的账号或跨账号授权关系。每个主账号都需要独立完成授权。[12][13]

已提供以下可直接用于控制台“脚本编辑”的 JSON 文件：

| 文件 | 用途 | 建议绑定对象 |
| --- | --- | --- |
| [aliyun-collector.json](policies/aliyun-collector.json) | 原有基础采集任务的查询权限，共 9 个授权动作 | 各主账号内的采集 RAM 用户，例如 `cloud-monitor-collector` |
| [aliyun-rds-performance.json](policies/aliyun-rds-performance.json) | 2026-09-10 新增 RDS 原生性能、规格及云监控数据查询，只读 | 启用数据库性能监控的各账号采集 RAM 用户 |
| [aliyun-sms-sender.json](policies/aliyun-sms-sender.json) | P0 告警短信发送权限 | 实际发送短信的账号内，另建 `cloud-monitor-sms` RAM 用户 |

采集模板覆盖全部可选任务，请根据每个账号实际启用的任务删除不用的动作。该模板按动作限制权限，`Resource: "*"` 表示允许查询该账号内对应服务的全部资源；它不表示拥有所有管理操作，也不表示可以跨账号访问。此模板包含证书详情查询权限，必须阅读下面的“证书详情权限边界”。

本程序查询 ECS / RDS 的到期信息时读取的是云资源控制面信息，不需要操作系统登录权限，也不需要 MySQL / PostgreSQL 数据库用户名、密码或 SQL 数据访问权限。OSS 数据来自云监控和费用中心，SLB 数据来自云监控和费用中心，当前采集方式不需要 `oss:GetObject` 等文件读取权限，也不需要负载均衡修改权限。

## 采集任务与准确的权限名称

RAM 授权动作不一定与 API 名称相同。下面的 `Action` 应按原样使用，不能自行替换为 API 的名字。

| 配置中的采集类型 | 实际调用的 API | RAM 策略中的 `Action` | 来源 |
| --- | --- | --- | --- |
| `ecs_expiry` | `DescribeInstances` | `ecs:DescribeInstances` | [1] |
| `rds_expiry`，含 MySQL / PostgreSQL | `DescribeDBInstances` | `rds:DescribeDBInstances` | [2] |
| `rds_performance` | `DescribeDBInstancePerformance`、`DescribeDBInstanceAttribute` | `rds:DescribeDBInstancePerformance`、`rds:DescribeDBInstanceAttribute` | [RDS 性能监控](rds-performance.md) |
| `rds_cloudmonitor` | `DescribeMetricList` | `cms:QueryMetricList` | [9] |
| `domains` | `QueryDomainList` | `domain:QueryCommonInfo` | [3] |
| `certificates` | `ListUserCertificateOrder` | `yundun-cert:ListUserCertificateOrder` | [4] |
| `certificates` | `GetUserCertificateDetail` | `yundun-cert:GetUserCertificateDetail` | [5] |
| `packages`，含 OSS / 短信 / SLB 资源包 | `QueryResourcePackageInstances` | `bss:DescribeInstances` | [6] |
| `slb_expiry` | `QueryAvailableInstances` | `bss:DescribeInstances` | [7] |
| `balance` | `QueryAccountBalance` | `bss:DescribeAcccount` | [8] |
| `metrics`，含 OSS 容量、请求量、流量及 SLB 带宽 | `DescribeMetricList` | `cms:QueryMetricList` | [9] |
| `sms_statistics` | `QuerySendStatistics` | `dysms:QuerySendStatistics` | [10] |

几个容易填错的官方原文：

- 域名 `QueryDomainList` 的授权信息是 `domain:QueryCommonInfo`。[3]
- 云监控 `DescribeMetricList` 的授权信息是 `cms:QueryMetricList`。[9]
- 查询账号余额的授权信息确实是 `bss:DescribeAcccount`，`Acccount` 中有三个连续的字母 `c`，按此拼写保留。[8]
- 费用中心两种实例查询均使用 `bss:DescribeInstances`；不要改成 `bssapi:QueryResourcePackageInstances` 或 `bssapi:QueryAvailableInstances`。[6][7]
- 证书 API 的服务名虽然是 `cas`，授权动作前缀是 `yundun-cert`。[4][5]

官方授权表中，域名、证书、上述费用中心操作、云监控查询和短信操作均使用全部资源 `*`，没有对应的产品条件关键字或关联操作。ECS / RDS 查询支持文档所列的资源 ARN，但本模板采用账号内全量查询权限以适配当前分页枚举方式。[1]-[10]

配置中的 `resource_ids`、`domain_names`、`certificate_ids`、`package_ids` 是程序在取得列表之后做的筛选，不是 RAM 权限边界。只在配置里填写 3 个 OSS Bucket 或 4 个 SLB 对象，不会把 AK 的查询权限自动限制到这几个资源。若要将 ECS / RDS 权限进一步限制到实例或地域，应拆分对应 Statement，按官方 ARN 设置范围，并验证当前枚举请求在该范围下能否正常返回。[1][2]

## 证书详情权限边界

当前程序调用证书详情时固定传 `CertFilter=true`。官方文档说明，值为 `true` 时不返回 `Cert`、`Key`、`EncryptCert`、`EncryptPrivateKey`、`SignCert`、`SignPrivateKey`；`false` 时会返回，默认值为 `false`。[5]

这只是程序传入的请求参数，**不是 RAM 强制的字段级限制**。官方对 `yundun-cert:GetUserCertificateDetail` 列出的授权条件关键字为“无”。拥有该动作的凭据可以在其他调用中使用 `CertFilter=false`，取得接口可返回的证书及私钥信息。因此不能把这条权限描述为“密钥本身无权读取证书私钥”。[5]

如果采集身份必须在云端权限上完全无权取得证书私钥，当前证书查询实现无法用这份策略达成该要求。可先关闭对应账号的 `certificates` 任务，并从该账号策略中删除两个 `yundun-cert` 动作；其他采集类型可继续使用。保留证书采集时，应将此访问能力纳入凭据管理范围。

## 在阿里云控制台创建和授权

以下操作由该主账号的管理员执行；采集 RAM 用户本身不需要管理 RAM 用户或创建策略的权限。官方路径可能随控制台版本显示为“新增授权”或“添加权限”。[12]-[15]

1. 登录 [RAM 控制台](https://ram.console.aliyun.com/)，确认当前是准备监控的那个阿里云主账号。
2. 打开“身份管理 > 用户 > 创建用户”，登录名称可填写 `cloud-monitor-collector`。给程序使用时选择“使用永久 AccessKey 访问”，按页面要求确认并创建；当前程序使用你回填的 AK/SK。无需为采集用户启用控制台登录。[12]
3. 创建时保存 AccessKey ID / AccessKey Secret；Secret 仅展示一次。若创建用户时没有生成 AK，进入该用户“凭证管理”页，在 AccessKey 区域创建。[13]
4. 打开“权限管理 > 权限策略 > 创建权限策略 > 脚本编辑”，填写 [aliyun-collector.json](policies/aliyun-collector.json) 的内容。根据这个账号实际启用的任务删减不用的动作，检查 JSON，策略名称可填写 `CloudMonitorCollectorRead`。[14]
5. 回到“身份管理 > 用户”，在采集用户的操作列选择“新增授权”。资源范围选“账号级别”，有些页面称为“整个云账号”；权限策略选“自定义策略”，勾选刚创建的 `CloudMonitorCollectorRead`，确认授权。这一步将策略绑定到用户，单独创建策略不会让用户获得权限。[15]
6. 将这对 AK/SK 填入配置中对应 `accounts` 项，并填写正确地域、资源信息。每个阿里云主账号重复上述步骤；可使用相同的 RAM 用户名和策略名，但 AK/SK 应分别属于各自账号。
7. 对照下表删减可选权限，同时确保相应采集任务已关闭，避免持续产生权限不足的采集错误。

| 该账号不需要的功能 | 可以删除的动作 |
| --- | --- |
| ECS 有效期 | `ecs:DescribeInstances` |
| RDS 有效期 | `rds:DescribeDBInstances` |
| 域名有效期 | `domain:QueryCommonInfo` |
| 证书有效期 | 两个 `yundun-cert` 动作 |
| 所有资源包查询和 SLB 有效期均不需要 | `bss:DescribeInstances`；任一功能保留时，该动作也要保留 |
| 账号余额 | `bss:DescribeAcccount` |
| 所有 OSS / SLB / RDS 云监控指标均不需要 | `cms:QueryMetricList` |
| 短信日发送量统计 | `dysms:QuerySendStatistics` |

`sms_receipts` 任务读取本服务数据库中的短信回执，不主动调用阿里云查询 API，因此不因启用此任务额外添加 RAM 查询动作。短信平台回执推送的地址和鉴权配置见 [短信成功率配置](../README.md#短信成功率)。人工查找产品代码的 `QueryProductList` 不在定时采集流程内。

2026-09-10 新增 `rds_cloudmonitor` 会首次及默认每天调用 `DescribeMetricMetaList` 校验单位、周期和维度。该接口官方授权表写明“当前 API 暂无授权信息透出”，公开接口元数据未列 RAM 动作；本策略不猜测添加 `cms:QueryMetricMeta`。当前 MySQL 账号已成功调用，其他账号以只读诊断结果为准。PG 的死锁速率和超过 5 秒 SQL 使用同一 `rds:DescribeDBInstancePerformance`，不需要新增数据库账号或 DAS 权限。

## P0 短信、P1 钉钉、P2 邮件

使用阿里云发送 P0 短信时，在实际发送短信的主账号里创建独立的 `cloud-monitor-sms` RAM 用户，按上述方式建立并绑定 [aliyun-sms-sender.json](policies/aliyun-sms-sender.json)。准确授权动作是 `dysms:SendSms`，填入 `notifications.sms.access_key_id` 和 `notifications.sms.access_key_secret` 的应是这个发送用户的密钥。[11]

这份发送策略只有发送短信的动作，不包含创建或审核短信签名、模板的权限。管理员先在短信服务中准备已审核可用的签名和模板，再在配置中填写 `sign_name`、`template_code`、收件手机号及模板参数。发送短信仍受短信服务开通状态、签名和模板审核、余额及发送频控约束；RAM 授权不代替这些业务条件。[11]

多云、多采集账号可以共用配置中的一组短信发送凭据。发送策略只需授予真正发送通知的 RAM 用户，不必授予每个采集用户。如果实际选择华为云短信，此阿里云短信发送策略无需配置。

P1 钉钉机器人使用机器人 Webhook 和签名密钥，P2 邮件使用 SMTP 配置和邮箱凭据；按本项目实现，它们不依赖阿里云 RAM 权限。短信发送和采集分开配置，能够分别控制查询权限与短信消费能力。

## 校验和已知限制

本次完成的是官方授权映射核验和策略 JSON 结构检查，未使用真实云账号验证授权，也未发出真实通知。项目的普通 `check` 只验证配置结构；`check --live` 也只检查启用配置和必填项，不证明云平台已授权成功。

上线后若出现权限不足，先核对失败任务绑定的账号、实际使用的 AK 所属用户、该用户已绑定的自定义策略，以及个人权限和继承用户组权限。管理员可在“RAM > 权限管理 > 权限诊断”使用接口错误里的 Request ID 或诊断信息定位拒绝原因；不要把 API 名称直接当成授权动作补入策略。[15]

本文针对已列出的阿里云中国站接口。费用中心在不同账号类型、站点、托管或结算关系下可能有额外业务限制；存在组织级显式拒绝时，也可能覆盖这里的 Allow。需以实际账号接口返回和权限诊断结果为准。模板没有授予自动购买、续费、修改资源配置或管理 IAM 的能力；证书详情的敏感读取能力已单独说明。

## 官方来源

以下接口页面均直接核查了“授权信息”表；证书详情页面同时核查了 `CertFilter` 参数说明。

1. [ECS DescribeInstances](https://help.aliyun.com/zh/ecs/developer-reference/api-ecs-2014-05-26-describeinstances)
2. [RDS DescribeDBInstances](https://help.aliyun.com/zh/rds/developer-reference/api-rds-2014-08-15-describedbinstances)
3. [域名 QueryDomainList](https://help.aliyun.com/zh/dws/developer-reference/api-domain-2018-01-29-querydomainlist)
4. [证书 ListUserCertificateOrder](https://help.aliyun.com/zh/ssl-certificate/developer-reference/api-cas-2020-04-07-listusercertificateorder)
5. [证书 GetUserCertificateDetail](https://help.aliyun.com/zh/ssl-certificate/developer-reference/api-cas-2020-04-07-getusercertificatedetail)
6. [费用中心 QueryResourcePackageInstances](https://help.aliyun.com/zh/user-center/developer-reference/api-bssopenapi-2017-12-14-queryresourcepackageinstances)
7. [费用中心 QueryAvailableInstances](https://help.aliyun.com/zh/user-center/developer-reference/api-bssopenapi-2017-12-14-queryavailableinstances)
8. [费用中心 QueryAccountBalance](https://help.aliyun.com/zh/user-center/developer-reference/api-bssopenapi-2017-12-14-queryaccountbalance)
9. [云监控 DescribeMetricList](https://help.aliyun.com/zh/cms/developer-reference/api-cms-2019-01-01-describemetriclist)
10. [短信 QuerySendStatistics](https://help.aliyun.com/zh/sms/developer-reference/api-dysmsapi-2017-05-25-querysendstatistics)
11. [短信 SendSms](https://help.aliyun.com/zh/sms/developer-reference/api-dysmsapi-2017-05-25-sendsms)
12. [创建 RAM 用户](https://help.aliyun.com/zh/ram/user-guide/create-a-ram-user)
13. [创建 AccessKey](https://help.aliyun.com/zh/ram/user-guide/create-an-accesskey-pair)
14. [创建自定义权限策略](https://help.aliyun.com/zh/ram/user-guide/create-a-custom-policy)
15. [管理 RAM 用户的权限](https://help.aliyun.com/zh/ram/user-guide/grant-permissions-to-the-ram-user)
