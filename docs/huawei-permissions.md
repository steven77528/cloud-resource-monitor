# 华为云采集账号权限配置

本文对应当前项目华为云采集器与内网估算关联查询的实际调用，原权限核验日期为
2026-09-09，2026-10-27 补充 EIP 关联与只读实测。适用于华为云中国站，使用 IAM 用户的 AK/SK。
每个被监控的华为云主账号都应在自己的 IAM 中创建采集用户、用户组和策略。

当前实现只调用查询接口。以下策略按华为云现有授权项收紧权限；云厂商的一个授权项
可能覆盖多个接口，不能将它理解为只允许读取某几个字段。
原策略编写时未使用真实凭据；2026-10-27 用户补充 EIP 列表权限后，已在原采集容器中只读验证生效，详见下文。程序不会自动创建云端策略。

## 先选对授权方式

本目录提供的 JSON 使用 **角色与策略授权中的自定义策略**，版本固定为 `1.1`。
在 IAM 控制台选择“权限管理 > 权限 > 创建自定义策略 > JSON 视图”。
不要将这些文件粘贴到新版“自定义身份策略”编辑器。
华为云官方明确说明：两种授权模型的策略和授权项不互通。[1][2]

例如，同一个接口在两种模型下可能使用不同的名字：

| 本项目提供的 1.1 自定义策略 | 新版身份策略中的名称，仅供辨认 |
| --- | --- |
| `bss:balance:view` | `billing:balance:view` |
| `bss:renewal:view` | `billing:subscription:view` |
| `bss:bill:view` | `billing:resourcePackages:view` |
| `rds:instance:list` | `rds:instance:listAll` |
| `ces:metricData:list` | `ces:metricData:get` |

此表不是可混用的授权清单。下面的操作步骤及 JSON 文件统一使用左列的模型。[3][4][5][6][7][19]

## 需要的权限

下表的授权范围按分开授权时可收紧的范围列出；使用合并策略时，BSS、RDS 和 CES
统一覆盖所有区域项目，具体操作见“使用合并策略”。

| 监控内容 | 程序实际调用 | 1.1 自定义策略授权项 | 授权范围 |
| --- | --- | --- | --- |
| 华为云账户余额 | `ShowCustomerAccountBalances`，`GET /v2/accounts/customer-accounts/balances` | `bss:balance:view` | 覆盖所有区域项目 |
| ECS、ELB 包年包月有效期 | `ListPayPerUseCustomerResources`，`POST /v2/orders/suscriptions/resources/query` | `bss:renewal:view` | 同上 |
| OBS 等资源包的额度和有效期 | `ListFreeResourceInfos`，`POST /v3/payments/free-resources/query` | `bss:bill:view` | 同上 |
| MySQL、PostgreSQL 有效期 | `ListInstances`，`GET /v3/{project_id}/instances` | `rds:instance:list` | 实际资源所属的区域项目 |
| SSL 证书有效期 | `ListCertificates`，`GET /v3/scm/certificates` | `scm:cert:list` | 全局服务资源 |
| OBS 容量和流量、ELB 带宽等监控数据 | `ShowMetricData`，`GET /V1.0/{project_id}/metric-data` | `ces:metricData:list` | 实际查询的区域项目 |
| ECS 网络监控自动发现 | `ListMetrics`，`GET /V1.0/{project_id}/metrics` | `ces:metrics:list` | 实际查询的 IAM 区域项目，不支持企业项目授权 |
| ECS 网络实例名称补齐、内网估算资源关联 | `ListServersDetails`，`GET /v1/{project_id}/cloudservers/detail` | `ecs:cloudServers:list` | 实例所属区域项目；查询沿用 CES 的 `project_ids` |
| RDS 内网估算资源关联 | `ListInstances`，`GET /v3/{project_id}/instances` | `rds:instance:list` | 实例所属区域项目；显式实例清单也需要 |
| EIP 绑定关联 | `ListPublicips`，`GET /v1/{project_id}/publicips` | `vpc:publicIps:list` | 实例/EIP 所属 IAM 区域项目；现有账号已验证 `cn-east-3` |

BSS 的接口与授权项来自官方“策略授权参考”；RDS、SCM、CES 来自各服务的
权限对照表及当前接口页。[3][8][9][10]

**BSS 是容易配错的地方：** SDK 中使用 `GlobalCredentials`，表示认证使用账号 ID，
不代表 IAM 自定义策略应当仅授予“全局服务资源”。官方系统权限表将 BSS 列为项目级服务，
并注明“授权时，除了全局服务外，需要授予其他所有区域的权限”。
因此 BSS 所在策略需按“所有资源”授权，保证覆盖全部区域项目；
不要只授权 `bss_region: cn-north-1` 对应项目。[11][12]

SCM 是全局级服务。CES 是项目级服务；RDS 列表接口也按 IAM 项目授权。
费用中心 BSS 与 RDS/CES 都属于项目级服务，可以合并到一份策略中；
合并后按 BSS 要求授权到所有区域，RDS/CES 权限也会覆盖所有区域项目。
如需把 RDS/CES 限定到实际使用的项目，可采用后文的分开授权方式。
SCM 仍需使用单独的全局策略，不能混入项目级策略。[1][8][11][13][14]

### 授权项的范围边界

- `bss:renewal:view` 是查询包年包月资源的当前授权项。
  官方表中的 `bss:order:view` 是该接口待下线的旧授权项，不应作为替代项新配。[3]
- `ListFreeResourceInfos` 对应的是 `bss:bill:view`，不是 `bss:order:view`。
  前者还允许查看账单、用量和部分费用信息，华为云 1.1 授权项无法只开放资源包剩余百分比。[3]
- `rds:instance:list` 还覆盖若干其他 RDS 接口。官方表甚至将历史会话分析上传和
  PostgreSQL 大版本升级检查映射到这个授权项，所以不能承诺这把密钥在云端严格只能查询。
  本项目自身只调用只读列表及监控查询，用于 MySQL/PostgreSQL 有效期、网络资源发现和内网估算关联。[8]
- SCM 的 1.1 权限表仍以旧版 `GET /v2/{project_id}/scm/certlist` 展示
  `scm:cert:list`；当前 v3 `ListCertificates` 页面同样列出 `scm:cert:list`，
  并引导角色与策略用户查阅该表。本项目使用 v3 接口，未调用下载证书或私钥接口。[9][15]
- 当前程序通过 CES 获取 OBS/ELB 监控数据，通过 BSS 获取 ECS/ELB 到期信息，
  这些原有任务不要求 OBS 对象下载、ECS 管理或 ELB 管理权限。
  ECS 网络自动发现新增的名称补齐单独调用 ECS 只读详情列表，需 `ecs:cloudServers:list`，
  不需要创建、修改、删除或登录 ECS 的权限。[20]
- `options.resource_ids`、`regions`、企业项目筛选和监控维度属于程序查询范围，
  不能代替 IAM 授权边界。需要更细的隔离时，应结合云厂商支持的项目授权范围另行收紧。

## EIP 关联的补充查询权限

2026-10-27 已在 `192.168.120.28` 的现有采集容器执行只读核查：
`huawei-hycen` 的 `cn-east-3` 项目可查询 33 台 ECS、3 台 RDS，均返回公网地址；
CES 可发现 38 条 `publicip_id` 公网出站指标序列。
首轮 `GET /v1/{project_id}/publicips` 返回 `403 / VPC.2201`。
用户补充下述权限后，该接口实测 `200`，38 个 EIP 中 36 个按公网地址唯一关联到全部 ECS/RDS；
抽查 6 台实例的双向 CES 公网数据也全部成功，本次未发现需要追加其他查询权限。

最小补充文件为 [huawei-eip-discovery.json](policies/huawei-eip-discovery.json)，
只包含 **`vpc:publicIps:list`**。沿用“角色与策略”中的 **Version `1.1` 自定义策略**，
可单独创建补充策略并给现有采集用户所在用户组授权，范围仅选择资源所属的
**`cn-east-3` IAM 区域项目**。也可将该动作追加到原策略，保留原有所有授权项；
不要用这个仅一项的补充文件覆盖原合并策略。上传文件到服务器不会更改云端 IAM。

官方 [旧版 EIP 接口与授权项表](https://support.huaweicloud.com/api-eip/eip_apipermission_0002.html)
将 EIP 列表映射到 `vpc:publicIps:list`。
[ListPublicips](https://support.huaweicloud.com/api-eip/eip_api_0003.html)
返回 `id`、`public_ip_address`、`port_id`、`private_ip_address` 等关联字段。
常规自动关联不需要逐项详情的 `vpc:publicIps:get`，也不需要绑定、解绑或修改资源权限。
新版身份策略页面的主动作名称为 `eip:publicIps:list`，本项目继续使用上述 1.1 授权模型。

ECS 复用已有 `ecs:cloudServers:list` 返回的端口和公网地址，RDS 复用
`rds:instance:list` 的 `public_ips`；CES 指标发现和数据查询复用已有
`ces:metrics:list`、`ces:metricData:list`。完整统计口径和现场核查状态见
[华为云内外网口径核对](huawei-network-scope-audit.md)。

当前项目原华为 ECS/RDS 网络任务已改为 `scope: intranet_estimated`，每 300 秒用闭合平均窗口计算同方向总量减全部关联 EIP 公网量。该模式即使设置显式 `instances`，也需 `ecs:cloudServers:list`（ECS）或 `rds:instance:list`（RDS），以及 `vpc:publicIps:list` 和 `ces:metricData:list`；显式 ECS 仅省略 CES 指标发现。名称可以回退为 ID，但关联清单无权限、部分分页或关系不明确时不能继续输出内网估算。上述权限用于读取已有绑定，不需要创建、绑定、解绑 EIP 或访问业务数据库。

## 在控制台创建并授权

### 使用合并策略

已将原 `huawei-collector-billing.json` 和 `huawei-collector-project.json` 的五个授权项
合并到 [huawei-collector-combined.json](policies/huawei-collector-combined.json)。
使用这份合并文件时，无需再创建或重复绑定原来的两份策略。
证书权限仍使用独立的 `huawei-collector-global.json`。

| 建议的自定义策略名称 | 直接使用的文件 | 给用户组选择的授权范围 |
| --- | --- | --- |
| `CloudMonitorCombinedRead` | [huawei-collector-combined.json](policies/huawei-collector-combined.json) | “所有资源”，覆盖 BSS、RDS、CES 的所有区域项目 |
| `CloudMonitorCertificateList` | [huawei-collector-global.json](policies/huawei-collector-global.json) | “全局服务资源”；监控证书时需要 |

1. 在 IAM 创建专用用户组，例如 `cloud-resource-monitor`。
2. 在“权限管理 > 权限 > 创建自定义策略 > JSON 视图”中，使用合并 JSON 创建
   `CloudMonitorCombinedRead`，给专用用户组授权时选择“所有资源”。使用的是
   “角色与策略”中的 1.1 自定义策略，不是新版身份策略。[1][12]
3. 需要监控证书时，再创建并单独授权上表的证书策略，范围选择“全局服务资源”。
4. 创建编程访问的 IAM 用户，加入该用户组，将其 AK/SK、账号 ID 和项目 ID 回填到配置。

2026-10-20 为 [网络速率监控](network-traffic.md) 新增 `ces:metrics:list`，合并文件现在包含原有五项与该查询项。已有云端自定义策略需要同步新增项；使用显式 ECS 实例清单时不会调用指标发现。授权依据为官方 [CES 指标管理权限表](https://support.huaweicloud.com/api-ces/ces_03_0048.html)。绑定到所有区域后，RDS/CES 的云端可访问范围也会扩大。
程序仍按配置中的区域、项目和任务进行采集，不会因为合并权限而自动遍历所有区域。
若原来已经绑定分开的策略，可在合并策略绑定完成并验证采集成功后，解除该用户组上的
两份旧策略绑定；云端已有策略是否被其他用户组使用，需要单独核对，不必直接删除策略。

2026-10-20 为解决 ECS 网络图例仅显示 UUID，合并文件另增 `ecs:cloudServers:list`。这是旧版 1.1 自定义策略的 ECS 详情列表查询项，无依赖授权项；不要换成废弃的 OpenStack `ecs:servers:list` 或新版身份策略名称。已有云端策略需同步该项，上传 JSON 到服务器不会自动授权。旧总量模式仅缺名称时可回退到 ID；2026-10-27 内网估算模式还依赖详情确认 EIP 关系，显式 ECS 清单也需要此权限。[20]

### 按区域收紧时分开授权

原来的三份独立文件继续保留，用于将 RDS/CES 权限限制在实际使用的区域项目。
该方式与上面的合并方式二选一；若已经绑定了覆盖所有区域的合并策略，额外绑定下面的
区域策略不会收窄已有权限。

1. 使用有权管理 IAM 的管理员登录对应华为云主账号，进入“统一身份认证服务 IAM”。
   创建一个专用用户组，例如 `cloud-resource-monitor`。[12]
2. 在“权限管理 > 权限 > 创建自定义策略”中选择“JSON 视图”，分别建立下表中的三个策略，
   粘贴对应 JSON 文件的完整内容。文件中没有账号密钥，不需要把 AK/SK 写进权限策略。[1]
3. 对专用用户组分别执行三次授权，并使用下表对应的授权范围。不要一次勾选三份策略后
   统一选择“所有资源”，否则 RDS/CES 也会获得所有区域的访问范围。[12]
4. 创建 IAM 用户，例如 `cloud-monitor-agent`，访问方式选择“编程访问”，
   凭证类型选择“访问密钥”，将用户加入上面的专用用户组。创建成功后获取该用户的 AK/SK。[16]
5. 将 AK/SK、账号 ID 和实际区域项目 ID 回填到 `config/config.yaml`，填写资源配置后再启用账号和任务。

| 建议的自定义策略名称 | 直接使用的文件 | 给用户组选择的授权范围 |
| --- | --- | --- |
| `CloudMonitorBillingRead` | [huawei-collector-billing.json](policies/huawei-collector-billing.json) | “所有资源”，以覆盖 BSS 要求的所有区域项目；策略内容仅含 3 个 BSS 授权项 |
| `CloudMonitorCertificateList` | [huawei-collector-global.json](policies/huawei-collector-global.json) | “全局服务资源” |
| `CloudMonitorRegionalRead` | [huawei-collector-project.json](policies/huawei-collector-project.json) | “指定区域项目资源”，仅勾选实际使用的项目，例如 `cn-north-4` |

三个文件遵循官方 1.1 自定义策略格式，使用 `Version`、`Statement`、`Effect`、`Action`。
未添加资源限定的 `Resource` 字段，授权范围由上述用户组授权操作确定，
与官方该类自定义策略示例一致。[1][17]

只开部分采集功能时，可从策略中删除对应未使用的授权项；不要删除正在运行任务所需的项。
例如不监控 RDS，就从正在使用的合并策略或区域策略中去掉 `rds:instance:list`；
不监控 SSL 证书，就不创建或不授予证书策略。
多个华为云主账号需要各自执行上述步骤，不能用主账号 A 创建的普通 IAM 用户直接读取主账号 B。

## 配置文件回填

```yaml
accounts:
  - id: huawei-main
    provider: huawei
    enabled: false
    access_key_id: "填写该 IAM 用户的 AK"
    access_key_secret: "填写该 IAM 用户的 SK"
    regions: [cn-north-4]
    project_ids:
      cn-north-4: "填写华北北京四对应的项目 ID"
    domain_id: "填写华为云账号 ID，不是 IAM 用户 ID"
    bss_region: cn-north-1
    scm_region: cn-north-4
    timeout_seconds: 20
```

区域名称、项目 ID 和资源所属项目要一致。`project_ids` 中的值是 IAM 项目 ID，
不是 `enterprise_project_id`；使用子项目时填写资源实际所属的子项目 ID。
账号 ID 和项目 ID 可由管理员在控制台“我的凭证 > API 凭证”中确认。

请明确填写 `domain_id`。当前 SDK 在缺少它时可能额外调用 IAM 自动查询账号信息；
填写后可避免为自动发现账号而追加 IAM 查询权限。
RDS/CES 的项目 ID 已由程序要求显式填写，因此上述策略没有添加 IAM 用户或项目管理权限。[18]

## 启用后排查权限

如某个任务出现 403 或权限不足，先按任务种类核对上表的授权项、用户组及授权范围。
余额、ECS/ELB 有效期和资源包任务失败时，尤其检查 BSS 是否覆盖所有区域项目；
RDS/CES 失败时，核对 `project_ids`、资源项目及该项目是否已授权。
SCM 检查是否授予全局服务资源权限，以及企业项目筛选是否与目标证书匹配。

通知权限与采集账号权限分别配置。钉钉和邮件不使用上述华为云 IAM 权限；
选择华为云短信时使用短信应用的 `app_key`/`app_secret`，详见 [通知配置](notifications.md)。

## 官方核验来源

1. [IAM：创建自定义策略](https://support.huaweicloud.com/usermanual-iam/iam_01_0605.html)。版本 `1.1`、JSON 视图、全局与项目服务不可混在同一自定义策略。
2. [BSS：权限和授权项说明](https://support.huaweicloud.com/api-oce/jac_000052.html)。两类授权模型。
3. [BSS：策略授权参考](https://support.huaweicloud.com/api-oce/jac_000053.html)。逐条核验三条 BSS 接口的 HTTP 方法、路径和授权项。
4. [查询账户余额](https://support.huaweicloud.com/api-oce/mac_00001.html)。当前接口及新版身份策略名称。
5. [查询客户包年包月资源列表](https://support.huaweicloud.com/api-oce/api_order_00021.html)。当前接口及新版身份策略名称。
6. [查询资源包列表](https://support.huaweicloud.com/api-oce/api_order_00027.html)。当前接口及新版身份策略名称。
7. [CES：查询监控数据](https://support.huaweicloud.com/api-ces/ces_03_0033.html)。当前接口及新版身份策略名称。
8. [RDS：策略授权参考](https://support.huaweicloud.com/api-rds/rds_10_0007.html)。`GET /v3/{project_id}/instances` 对应 `rds:instance:list` 及该授权项的覆盖范围。
9. [CCM：策略授权参考](https://support.huaweicloud.com/api-ccm/ccm_02_0055.html)。`scm:cert:list`，无额外依赖授权项。
10. [CES：监控数据管理接口授权项说明](https://support.huaweicloud.com/api-ces/ces_03_0050.html)。`ces:metricData:list`，支持 IAM 项目，不支持企业项目授权。
11. [IAM：系统权限](https://support.huaweicloud.com/permissions/iam_01_0001.html?product=billing)。BSS 为项目级服务，要求全局服务之外的所有区域权限。
12. [IAM：创建用户组并授权](https://support.huaweicloud.com/usermanual-iam/iam_03_0001.html)。所有资源、指定区域项目、全局服务资源三类范围。
13. [CCM：权限管理](https://support.huaweicloud.com/productdesc-ccm/ccm_01_0007.html)。CCM 为全局级服务。
14. [CES：权限管理](https://support.huaweicloud.com/productdesc-ces/ces_07_0009.html)。CES 为项目级服务。
15. [SCM：ListCertificates](https://support.huaweicloud.com/api-ccm/ListCertificates.html)。实际使用的 v3 证书列表接口。
16. [IAM：创建 IAM 用户](https://support.huaweicloud.com/usermanual-iam/iam_02_0001.html)。编程访问及访问密钥凭证。
17. [费用中心：通过 IAM 角色或策略授予权限](https://support.huaweicloud.com/usermanual-billing/authority_topic_0000003.html)。1.1 自定义策略示例。
18. [官方 Python SDK 凭据实现](https://github.com/huaweicloud/huaweicloud-sdk-python-v3/blob/master/huaweicloud-sdk-core/huaweicloudsdkcore/auth/credentials.py)。已同时对照本项目固定使用的 `3.1.213` 包核验显式账号/项目 ID 与自动发现行为。
19. [RDS：ListInstances](https://support.huaweicloud.com/api-rds/rds_01_0004.html)。当前实例列表接口及新版身份策略名称。
20. [ECS：生命周期管理策略授权参考](https://support.huaweicloud.com/api-ecs/ecs_06_0002.html)。`GET /v1/{project_id}/cloudservers/detail` 对应 `ecs:cloudServers:list`，与已废弃的 OpenStack 授权项区分；同时核验 [ListServersDetails](https://support.huaweicloud.com/api-ecs/zh-cn_topic_0094148850.html) 的 `server_id` 批量过滤及 `id/name` 响应字段。
