# 按资源配置告警

可以为同一采集任务中的每个资源单独决定是否告警。例如，只给仍在使用的证书域名发临期提醒，关闭历史证书的提醒；只监控指定资源包的到期和额度告警。

配置入口是现有 `jobs[]` 任务下的 `alerting`，与 `options`、`rules` 同级。它只决定业务指标是否参与告警，不改变采集范围、采集频率、MySQL 样本保存、Grafana 资源数据或云 API 调用量。关闭某个资源的告警后，仍可在面板中查看该资源的实际到期时间、额度等数据。

不添加 `alerting` 时，保持原有全部资源参与告警的行为。已有账号、任务和通知渠道开关、规则阈值都继续生效。采集失败与 API 探测异常告警不受资源开关影响，避免把接口故障误当成资源没有问题。

## 证书：只给指定域名告警

在需要调整的证书任务中添加下面的 `alerting`。此处是**任务内部的字段片段**，应合并到已有任务下，不要新增同 ID 的任务，也不要重复添加 `jobs`：

```yaml
    alerting:
      default_enabled: false
      resources:
        - domain: api.example.com
          enabled: true
        - domain: "*.example.com"
          enabled: true
        - resource_id: "cert-old-001"
          enabled: false
```

这表示只有域名匹配 `api.example.com` 或 `*.example.com` 的证书才参与告警，其余证书不告警；其中旧证书 `cert-old-001` 始终不告警，即使它也包含前述域名。用真实域名和证书 ID 替换示例值。

域名选择会匹配证书主域名及采集结果中的 SAN 域名。域名忽略大小写和末尾的点，例如 `API.EXAMPLE.COM.` 与 `api.example.com` 匹配。除此之外都是精确匹配：`*.example.com` 是证书上的字面域名标识，不是任意子域名匹配表达式，不能因此选中仅包含 `www.example.com` 的另一张证书。YAML 中以 `*` 开头的域名必须加引号。

按域名启用时，同一域名的新旧证书都会匹配；续签后只要新证书继续包含该域名，就会自动参与告警。需要排除历史证书时，再用 `resource_id` 明确关闭旧证书 ID。希望严格限定某一张证书时，直接使用该证书的 `resource_id`。

一个证书可能包含多个域名，但到期告警对应整张证书。如果其中任意匹配项设置了 `enabled: false`，这张证书的告警会整体关闭。

## 当前 yuanzhu 证书告警范围

2026-10-23 在 `192.168.120.28` 通过阿里云只读接口核对 `aliyun-yuanzhu`：共采集 55 张证书，其中 15 张尚未过期、40 张已过期。`aliyun-yuanzhu-certificates` 沿用 `aliyun-hycen` 的证书 ID 白名单方式，将 `default_enabled` 设为 `false`，仅对以下 15 个 ID 设置 `enabled: true`。

| 证书 ID | 证书域名 | 到期时间（北京时间） |
| --- | --- | --- |
| `21137472` | `zhuhepay.com` | 2026-10-25 07:59:59 |
| `21415310` | `app.lemanghuandian.com` | 2026-11-10 07:59:59 |
| `21477406` | `super.lemanghuandian.com` | 2026-11-13 07:59:59 |
| `21747353` | `cnt6lvu2.lemanghuandian.com` | 2026-11-29 07:59:59 |
| `21799378` | `image.lemanghuandian.com` | 2026-12-02 07:59:59 |
| `27123457` | `gateway.demonpower.cn` | 2026-12-07 07:59:59 |
| `27123959` | `jump.demonpower.cn` | 2026-12-07 07:59:59 |
| `22139860` | `oss.demonpower.cn` | 2026-12-15 07:59:59 |
| `25521126` | `*.mannengpao.com` | 2026-12-26 07:59:59 |
| `27539307` | `lemanghuandian.com` | 2026-12-28 07:59:59 |
| `22563410` | `gatway.lemanghuandian.com` | 2027-01-04 07:59:59 |
| `22695803` | `mnp.lemanghuandian.com` | 2027-01-12 07:59:59 |
| `23081355` | `demonpower.cn` | 2027-01-23 07:59:59 |
| `26795991` | `image-private.lemanghuandian.com` | 2027-02-23 07:59:59 |
| `26926493` | `link.lemanghuandian.com` | 2027-03-02 07:59:59 |

选择依据为本次查询时准确的到期时间，使用未取整的 `expiry_days > 0`，不是仅筛选 30 天内临期证书。清单内证书以后到期仍继续参与告警；这不是自动排除已过期证书的动态条件。续签或新增证书产生新 ID 时，需要按需另行加入，避免扩大本次授权范围。已过期证书仍采集和展示，但不参与该任务的到期业务告警。

任务保持每天检查一次，并沿用 `expiry` 规则的 30 天内 P1、7 天内 P0。此次未调整其他账号、任务、通知渠道或凭据。脱敏发现清单和范围校验记录保存在 `preview/validation/aliyun-yuanzhu-certificate-alerts-2026-10-23.json`；配置检查不写业务数据库、不发送测试通知，部署时重启已有采集容器加载新配置。

## 证书：默认告警，只排除部分资源

需要保留新发现资源的提醒时，使用默认开启并列出排除项：

```yaml
    alerting:
      default_enabled: true
      resources:
        - domain: retired.example.com
          enabled: false
        - resource_id: "cert-unused-002"
          enabled: false
```

这样只关闭包含 `retired.example.com` 的证书及 ID 为 `cert-unused-002` 的证书，其余已采集的证书继续按原阈值告警。

证书域名与注册域名是不同资源。如果修改的是阿里云 `domains` 注册域名到期任务，使用该任务的域名资源 ID，例如：

```yaml
    alerting:
      default_enabled: false
      resources:
        - resource_id: "example.com"
          enabled: true
```

`resource_id` 按采集结果原样精确匹配，不做域名大小写或末尾点的归一化。这个片段控制注册域名到期提醒，不会替代证书任务中的设置。

## 资源包：按包决定是否告警

在相应资源包任务下使用 `package_id`，即可同时控制该包的到期及额度相关告警：

```yaml
    alerting:
      default_enabled: false
      resources:
        - package_id: "pkg-production-001"
          enabled: true
        - package_id: "pkg-production-002"
          enabled: true
```

只有这两个资源包参与告警，其余包仍正常采集和展示。希望默认给所有资源包告警、仅排除旧包时，改为：

```yaml
    alerting:
      default_enabled: true
      resources:
        - package_id: "pkg-expired-003"
          enabled: false
```

包 ID 使用云平台实际资源包实例 ID，不能填商品代码、产品名称或 `product_code`。阿里云资源包的指标资源 ID 通常就是包 ID。华为云使用已购买资源包的 `order_instance_id`，额度项可能另有自己的资源 ID；使用 `package_id` 可以一起选中父资源包到期指标及其下属额度项。也可以用 `resource_id` 精确选择单个额度项，此时不会自动包含同包的其他指标。

证书 ID、资源包实例 ID 可在对应云平台控制台查看，也可从 Grafana 表格的“资源 ID”或“资源包 ID”列核对。填写额度项的 `resource_id` 时，使用该额度指标对应的资源 ID，不要替换成父资源包 ID。

如果同时设置“整个包启用”和“某个额度项关闭”，被明确关闭的额度项不告警，其余匹配该包的指标继续告警。如果整个包关闭，则其中额度项的启用条目不能覆盖关闭设置。

## 字段与匹配规则

| 字段 | 含义 |
| --- | --- |
| `default_enabled` | 未命中任何条目的资源是否参与告警，默认 `true` |
| `resources` | 资源选择条目列表，默认空列表 |
| `resources[].enabled` | 该条目是否启用告警，必须填写 YAML 布尔值 `true` 或 `false` |
| `resources[].domain` | 证书主域名或 SAN 域名，忽略大小写及末尾点；不支持通配表达式 |
| `resources[].resource_id` | 指标中的资源 ID，原样精确匹配，例如证书 ID、注册域名或额度项 ID |
| `resources[].package_id` | 优先匹配指标的 `dimensions.package_id`，没有包维度时匹配其 `resource_id`；原样精确匹配 |

每个 `resources` 条目只能包含 `domain`、`resource_id`、`package_id` 三者之一，并且必须设置 `enabled`。ID 建议都加引号，避免纯数字 ID 被 YAML 解析为数字。选择条目不支持前缀、子串或正则表达式。

同一种选择字段不能重复填写相同的值，即使两项的 `enabled` 不同也会被配置检查拒绝。域名归一化后相同也属于重复，例如 `API.EXAMPLE.COM.` 与 `api.example.com`；需要改变开关时直接修改已有条目。

同一资源命中多个条目时，**关闭优先**，与条目排列顺序无关；没有关闭条目且至少命中一个启用条目时，启用告警；没有任何匹配时才使用 `default_enabled`。`default_enabled: false` 配合空列表会关闭该任务所有资源业务告警，采集异常及 API 探测异常仍保留。

配置只作用于所属任务。不同账号或不同任务即使使用同一个域名或包 ID，也需要在各自任务下分别设置。

原有 `options` 仍先决定实际采集哪些资源。例如阿里云的 `certificate_ids`、`domain_names`、`package_ids`，以及华为云证书的 `resource_ids` 等采集筛选条件仍然有效。被采集条件排除的资源没有样本，不能仅靠 `alerting` 启用告警。如果需要“全量展示、部分告警”，先确认采集范围包含这些资源，再用 `alerting` 控制提醒。

## 生效与已有告警

修改配置后需要重启采集服务。启动加载新配置时，对已关闭告警的资源，会取消其尚未发送或发送失败的旧通知，并清除其活动告警、连续超阈值等候选状态；这一清理过程不发送恢复通知。已经发送的通知保留原记录，无法撤回。

**首次升级的证书状态兼容处理：** 如果 `certificates` 任务使用了任意 `domain` 选择项，而旧到期告警状态还没有完整的 `domains` 域名元数据，升级后首次加载这些状态时，会静默清除这些旧状态并取消对应的待发或失败通知。下一次新采集取得主域名及 SAN 后重新判断，避免旧通知在 SAN 筛选生效前发出。仍然启用且满足告警条件的证书可能因此重新发送一次首次告警。已经保存完整域名元数据的启用证书，在普通重启时保留原有告警状态，不会因重启重复执行这项迁移。

之后重新启用该资源时，从新的采集样本重新判断告警，按原规则计算连续次数、持续时间与阈值，不继承关闭前的活动状态。调整 `default_enabled` 或移除资源条目也会按新配置重新确定资源是否参与告警。

已有运行实例会继续使用它启动时的配置，因此应在变更后及时完成重启。通知已经发出或正在外部渠道处理时，不能依靠本地配置将其撤回。

## 检查与部署

在项目目录执行离线配置检查。这不会调用云 API、连接生产 MySQL 或发送通知：

```sh
python -m cloud_monitor --config config/config.yaml check
```

首次升级到支持 `alerting` 的版本时，先同步本项目源码并构建采集服务镜像，再检查配置和更新采集容器：

```sh
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check
docker compose up -d --no-deps cloud-monitor
```

后续只修改资源告警配置时，无需再次构建镜像，检查通过后重启 `cloud-monitor`：

```sh
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check
docker compose restart cloud-monitor
```

这些变更不需要修改独立通知网关、MySQL 或通知渠道。重启后的正常采集仍会按实际启用的资源、规则和渠道处理真实告警；配置检查本身不会发送测试消息。
