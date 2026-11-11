# 采集报错排查

## 2026-11-03 失败与恢复邮件反复出现

通过 SSH 密码弹框在 `192.168.120.28` 原部署目录完成只读核查。
本节时间均为北京时间，邮件计数截至约 09:50；`sent` 代表通知流程接受提交，
不能单独证明收件端最终送达。调查阶段没有修改线上代码、配置、账号或渠道开关，
没有重启服务、写入监控数据库、启动额外调度器或发送测试通知。
随后按用户确认，于 09:59 完成下述十项任务的失败确认次数调整，
仅重启原采集服务；详见 [部署记录](server-update.md#2026-11-03-采集失败连续两次确认)。

### 邮件数量和触发规则

| 日期 | 全部邮件记录 | 采集失败/恢复 | 其中华为 ECS 网络 | 其中阿里云任务 |
| --- | ---: | ---: | ---: | ---: |
| 2026-11-01 | 57 | 50 | 24 | 24 |
| 2026-11-02 | 96 | 90 | 60 | 30 |
| 2026-11-03，截至约 09:50 | 37 | 33 | 31 | 2 |

11 月 1 日另外两条采集通知来自七牛资源包。近七天查询共有 384 条
采集失败/恢复邮件记录，全部 `sent`、`attempts=0`；当前全部渠道没有
`pending` 或 `failed` 队列积压。11 月 2 日华为 ECS 的 60 条为 30 次触发、
30 次恢复；11 月 3 日为 16 次触发、15 次恢复。数据支持反复发生新告警周期，
没有监控发送队列失败重试造成重复提交的证据。

调整前线上为 `collection_error_samples=1`、`recovery_samples=2`、
`notify_recovery=true`。华为 ECS 每五分钟采集，单轮失败就提醒，连续两轮
查询健康就发恢复；恢复后再失败会再次提醒。P2 的 24 小时重复间隔只约束
持续未恢复的告警。例如 11 月 3 日 `05:16:26` 触发、`05:31:26` 恢复，
`05:41:26` 再触发、`06:01:26` 再恢复。

### 已确认的失败原因

Docker 日志实际覆盖约 11 月 2 日 `09:08` 至 11 月 3 日 `09:49`，
受 `10m x 3` 日志轮转限制，不是完整七天。在该保留窗口中，华为 ECS 网络
有 48 轮 `error`，逐资源错误共 90 条，涉及七台 ECS，全部为：

```text
Huawei network estimate sources are stale or in the future
```

估算要求总入站、总出站和关联 EIP 公网双向序列具有完全相同的源时间。
线上 `max_age_seconds=900`；最新共同源点超过 15 分钟便拒绝估算。
此路径还限制源点必须位于已经闭合的历史查询窗口，因此本次错误指向源数据陈旧，
不代表已经证实 ECS 网络或业务服务发生中断。历史日志没有保留失败时各源的时间戳，
尚不能确定是 ECS 总量还是 EIP 公网序列延迟，也不能确定云端上报/聚合延迟的更深原因。

独立只读查询随后成功取得全部 33 台 ECS 的 66 个双向样本，
`failed_resources=0`、`unavailable_resources=0`；24 台的共同源点年龄约 322 秒，
九台约 622 秒。该结果说明本次复查时查询已正常，不证明以后不再发生延迟。
保留日志另有 17 轮 `degraded`；10 月 30 日的有限负差处理仍在生效，
不能把这次陈旧数据错误归为原负差问题。

阿里云任务同期有短暂超时、连接重置、一次 DNS 解析失败等传输异常；
11 月 3 日 `08:13:37` 的负载均衡带宽查询明确返回 HTTP `503` /
`ServiceUnavailable`，下一轮成功，`08:15:27` 生成恢复通知。
具体网络设备、DNS 服务或云端节点责任尚未确认。采集容器自 10 月 30 日
启动后 `restart_count=0`、无 OOM，检查时健康。

### 后续处理边界

使用原有 `Service._collection_health` 和 `Engine` 对保留日志做离线回放，
存储全部在内存中，恢复始终要求两次健康；默认阈值下每个任务的触发/恢复数量
均与同窗监控库记录一致。窗口为 11 月 2 日约 09:08 至 11 月 3 日约 09:49，
与上面的自然日统计范围不同：

| 回放范围 | 一次失败触发（调整前） | 连续两次失败触发 | 连续三次失败触发 |
| --- | ---: | ---: | ---: |
| 华为 ECS 网络 | 67 | 18 | 0 |
| 阿里云九个发生失败的任务 | 32 | 2 | 0 |
| 合计失败/恢复邮件数 | 99 | 20 | 0 |

华为 48 个失败轮次由 30 段单轮失败和九段连续两轮失败组成，最长两轮。
阿里云仅 `aliyun-yuanzhu-mysql-cloudmonitor` 有一段连续两轮失败，其余均单轮。
三次确认会过滤掉本窗口全部事件，不能据此认为故障已消失。

用户已确认采用逐任务 `collection_error_samples=2`，仅调整本窗口出现失败的
华为 ECS 网络和九项阿里云采集任务，保持两次健康恢复；该窗口的回放邮件数减少约 79.8%。
这会分别把首轮失败后的通知确认推迟约五分钟、一分钟，属于通知确认策略调整，
不能使陈旧数据变新或修复云 API。该调整已于 2026-11-03 09:59 上线，
本地项目同步保存十项设置；运行镜像不变，采集服务健康，MySQL 和通知网关未重启。
仍应保留真实错误状态和原有数据新鲜度校验；没有证据支持直接放宽 15 分钟上限、
把陈旧值当正常值、补零或关闭任务/邮件渠道。

取证脚本为 `work/inspect_mail_flapping_20261103.sh`，脱敏结果为
`work/mail-flapping-evidence-20261103.jsonl`（每行以记录类别和 JSON 内容组成）。
脚本只读监控库并回滚事务；云查询使用独立进程，不创建 `Service` / `Store`。
再次执行会重新查询云 API，应通过密码弹框按需运行。
离线回放为 `work/replay_collection_health_20261103.py`，结果保存在
`work/mail-flapping-replay-20261103.json`。该回放不加载配置或凭据、连接数据库、
启动服务或发送通知，可在项目目录执行：

```powershell
.\.venv\Scripts\python.exe -m work.replay_collection_health_20261103 work/mail-flapping-evidence-20261103.jsonl --output work/mail-flapping-replay-20261103.json
```

## 2026-10-30 频繁采集失败邮件

通过密码弹框 SSH 登录 `192.168.120.28`，在原目录
`/data/monitor/cloud-resource-monitor` 的现有容器中完成只读核查。
以下时间均为北京时间。没有改线上配置、重启服务、写监控数据库、
启动新调度器或发送测试通知。检查了容器状态、保留的 Docker 日志、
监控库只读事务和独立进程中的只读云 API。

### 邮件来源和告警频率

截至 10 月 30 日约 14:15，监控库近七天记录了 186 条
`collection-error` 邮件提交，均为 `sent`，队列 `attempts=0`。
其中 `huawei-ecs-network` 为 46 次告警、46 次恢复，共 92 条，
约占 49.5%。10 月 29 日全天各任务合计 60 条，10 月 30 日截至核查时
合计 28 条；后者包含华为 ECS 网络 18 条、华为 ELB 6 条和两项阿里云
RDS 性能任务各 2 条。`sent` 表示通知流程已接受提交，不能单独证明终端送达。

线上 `collection_error_samples=1`、`recovery_samples=2`、
`notify_recovery=true`。单轮失败即可产生 P2 邮件，连续两轮正常后产生恢复邮件；
恢复后再次失败会新建告警，不受持续故障的 P2 24 小时重复提醒间隔限制。
这些记录反映多次告警与恢复，监控队列没有发生失败重试。

容器日志实际仅保留了 10 月 29 日 14:18 至 30 日 14:13 左右，
不是完整七天。该窗口内华为 ECS 网络任务共 287 轮，249 轮正常、
38 轮部分失败，每轮涉及 1 至 2 台资源。八个相关采集与诊断源码文件
的 SHA-256（统一 LF 后）在排查时均与当时本地项目一致。

### 已复现：华为 ECS 内网估算出现负差

`huawei-ecs-network` 使用 `scope: intranet_estimated`，
每 300 秒采集，以总网络速率减全部关联 EIP 公网速率。
独立只读进程连续两轮均复现 `prometheus` 实例的以下错误，
其余 32 台 ECS 仍返回 64 个双向样本：

```text
HuaweiCollectionError: Huawei network estimate has a negative or non-finite difference
```

14:18 再次核验该实例的源数据，确认差值有限但为负。
以下数据均经过实际源单位换算为 `bit/s`，采用 `period=300`、
`filter=average`，在完全相同的源时间戳比较，实例关联一个 EIP：

| 源时间 | 方向 | ECS 总速率 | EIP 公网速率 | 内网估算差值 |
| --- | --- | ---: | ---: | ---: |
| 14:05 | 出站 | 746,128.08 | 4,448.48 | 741,679.60 |
| 14:10 | 出站 | 60,701.44 | 638,741.87 | -578,040.43 |
| 14:10 | 入站 | 951,365.68 | 6,842.80 | 944,522.88 |

该负差不是舍入误差。不同测量来源即使统一周期和返回时间戳，
也不能保证每个窗口都满足“公网量不大于实例总量”；此次数据不足以进一步
确定云端测量位置、采样或聚合时序中哪一项造成差异。
排查时的线上版本按原有约定拒绝负差，并同时放弃该实例两个方向的估算，
随后把任务记为部分失败，触发采集健康告警。这不代表已经证实该实例网络故障。

历史部分失败日志仅记录 `failed_resources`，未记录被捕获的逐资源异常。
本次复现确认了当前负差问题，不能把保留窗口内全部 38 次部分失败都归为负差。
随后 14:26 定向复查 12 小时原始数据，确认同一五分钟聚合桶内，
ECS 只有一个桶末附近的原始速率样本，EIP 为五个分钟样本的均值；
14:10 桶中的公网分钟突发使两者相减为负。单位转换和当前唯一绑定已核对，
固定平移五分钟未改善整体比较。原始样本、复算和证据边界见
[负差原因专项核查](huawei-network-negative-difference-research.md#2026-10-30-1426-现场验证结果)。
后续修复按下节将“估算不可用”与 API 失败分开处理，采集代码已于同日部署。
不能无条件把负差置零，也不能把这一窗口当作有效零内网流量。
口径边界见 [华为网络口径核对](huawei-network-scope-audit.md)。

### 已部署修复：保留估算，负差单列不可用

保持现有指标来源、`period=300`、`filter=average`、源时间策略、配置与业务阈值。
只有**有限负差**被单列为 `degraded`（估算不可用），不会因此新增
`collection-error`；若同时发生 API、字段、非有限值、缺失/陈旧/不对齐数据
或映射错误，任务仍为 `error`，沿用原采集失败通知。
只有负差的查询轮次属于查询成功，可按原连续次数确认已有采集健康告警恢复，
但不表示业务阈值告警或受影响资源估算已经恢复。

有限负差时，不输出该资源任一方向的估算，不用零值或上轮旧值替代。
持久化该资源的无效源时间；重启服务、重复旧点或其他资源成功均不能解除。
必须取得同一任务、地域、资源严格更新的有效双向配对点，才恢复该资源。
不可用期间已有业务活动告警保留，超限候选和业务恢复连续性中断，
之后从新的有效源样本重新累计。其他可用资源继续保存、展示与判断。
真实采集错误或任务陈旧则隐藏该估算任务的全部当前值。

新负差水位会取消同一资源已排队或失败重试的业务恢复通知，
仅限恢复依据源时间不晚于该水位的消息；不清除业务活动告警，
也不因此取消业务触发（`firing`）通知。
完整成功的目标发现会记录当前资源范围，已移出范围的资源不计入 `degraded`，
不导出其可用性或旧值；历史水位保留，重新发现后仍须严格更新的有效双向配对点恢复。
不完整或失败的目标发现不能据此移除资源。

`/readyz` 接受 `degraded`，真实错误仍不通过；Prometheus 增加
`cloud_monitor_network_estimate_available`，按资源显示 `0` 不可用、`1` 可用，
曾负差资源的 `1` 还要求严格更新的有效配对点。
指标覆盖已取得有效配对点或有负差记录的资源，不替代新鲜度与任务状态。
通用 Grafana 看板有独立“内网估算可用性”表和橙色“估算不可用”任务状态。
详细恢复条件与标签见 [估算可用性](network-traffic.md#估算可用性)。

采样试验未证明更换均值或对齐方式可修复估算：
将 EIP 改取 ECS 源点所在分钟，旧方案 14 个负差桶中 12 个消失、2 个仍在，
同时新增 13 个负差桶，候选共 15 个；不能将其说成改善。
改用 Agent 原生分钟样本再算五分钟均值仍有 15 个负差，
因此没有据此切换生产数据源。完整取证见
[负差专项核查](huawei-network-negative-difference-research.md)。

随后对华为三台 RDS 及阿里云 ECS/RDS 追加统一 24 小时只读复查：
华为 RDS 的 1,728 个双向五分钟差值均非负，原始分钟点与聚合复算仅有舍入差；
阿里云原生内网/总吞吐源未见负值、非法值或冲突，部分 ECS 历史分钟点存在缺口。
阿里云近七天 26 条网络采集失败/恢复邮件均在该指标窗口之前，
缺少对应轮次异常日志，不能将分钟缺口或其他性能任务的连接重置当作已确认原因。
范围、数值、当前绑定与历史证据边界见
[华为 RDS 与阿里云网络复查](network-scope-followup-20261030.md)。

### 其他错误及当前状态

- 华为 ELB 带宽在 10 月 30 日 01:20、01:32、01:40 的请求出现超时，
  每次下一分钟成功，随后第二轮成功确认恢复，共产生 6 条告警及恢复邮件。
- 阿里云 `aliyun-mysql-iot-performance` 和
  `aliyun-yuanzhu-mysql-national-readonly-performance` 在 11:07 遇到
  SDK 传输异常，底层日志包含连接重置；11:08 成功、11:09 确认恢复，
  共产生 4 条告警及恢复邮件。现有证据不能定位重置发生在网络链路哪一端。
- `huawei-rds-expiry` 持久化状态仍为 10 月 29 日 18:20 的
  `HuaweiCollectionError: Missing time field in Huawei response`。
  该路径读取包年包月 RDS 的 `expiration_time`。本次只读复查中
  `rds-yj-pms`、`rds-PMS`、`rds-IOT` 均有字符串到期时间，返回 3 个样本成功。
  历史日志没有保留缺字段的实例，不能确定当时具体哪台返回异常。
  只读诊断不更新告警状态；该任务每天运行一次，后续需要正常调度累计两次成功
  才按现有配置确认恢复，不应运行 `once` 来刷新状态。

服务器采集容器与通知网关均健康，采集容器没有重启或 OOM 记录；
系统无失败的 systemd 单元，根分区使用率 23%，内存充足。
最初的服务器排查仅取证，未改变线上采集、告警或部署行为。
上述估算质量区分已部署到 `192.168.120.28`，正式首轮 ECS 33 台、RDS 3 台均为可用估算，共导出 72 条网络序列及 36 条可用性序列；配置、MySQL 和通知网关保持原状。线上现用 Grafana 看板尚未应用新增可用性表，已同步通用看板文件。部署细节及回滚见 [上线记录](server-update.md#2026-10-30-华为内网估算质量处理)。

## 2026-10-19 短信统计不足

本次针对 `aliyun-hycen` / `aliyun-sms-statistics` 的“短信发送量统计采集”失败，已在同账号、同日期窗口的只读复查中确认：`QuerySendStatistics` 返回 `OK`，2026-10-11 至 2026-10-18 只返回 10 月 15 日发送量 2 条，其他七天未返回。旧代码要求八天齐全，将缺少日期抛为采集异常，进而使用了“请检查权限和接口”的通用通知。该复查确认了相同输入可复现的原因，没有读取 01:03:51 那轮原始错误日志，因此不能据此排除历史某一轮还存在其他异常。

当前版本区分以下结果，只有所有分页成功、响应校验通过后才判断统计是否足够：

| 返回结果 | 任务状态 | 增长率与告警处理 |
| --- | --- | --- |
| 八天齐全，前七日均值大于零 | `ok` | 沿用原增长率计算和业务规则 |
| 八天明确都为零 | `ok` | 增长率为零 |
| 全部统计为空 | `no_data` | 不生成增长率，不假设每天零发送 |
| 有统计但窗口日期缺失 | `insufficient_data` | 安全详情列出缺失日期，不补零 |
| 前七日明确为零，昨日有发送量 | `insufficient_data` | 零基线无法计算百分比增长 |
| API、分页、字段或数值异常 | `error` | 保持采集失败告警，详情用于继续排查 |

`insufficient_data` 是“查询成功、统计不足”，不再触发权限/接口失败通知。历史采集失败告警在连续两次正常查询结果后按默认配置恢复，统计不足也计入；历史短信业务告警继续保留，只有新的有效业务指标才能参与恢复判断。旧增长率当前值立即隐藏，避免展示上一次有效结果误导判断。Grafana 任务状态显示橙色“统计不足”，并计入“异常 / 待采集任务”。`/readyz` 接受该状态，表示查询可用，不保证统计完整。

短信增长率只有在最近持久化任务状态为 `ok` 且样本仍新鲜时才作为当前值导出。无数据、统计不足和真实采集错误都隐藏旧值；先统计不足、下一轮接口失败也不会重新显示旧增长率，必须等待新的完整有效统计。华为内网估算另按前述逐资源可用性及任务状态处理；其余任务遇到接口失败仍可展示尚未过期的旧样本。

本次只修改本地完整项目，未部署服务器或发送真实通知。后续同步源码时保留服务器 `.env`、整个 `config/`、原 Compose 配置与 MySQL 数据；在服务器项目目录逐条执行：

```sh
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml diagnose --job aliyun-sms-statistics
docker compose up -d --no-deps --force-recreate cloud-monitor
docker compose logs --since 5m --tail=150 cloud-monitor
```

`check --live` 是离线配置检查。`diagnose` 不连接监控数据库、不写队列、不发送通知，只调用真实只读云 API；统计仍不足时应返回 `status: insufficient_data`、`samples: 0` 和安全详情，退出 0。任一检查失败时先处理具体错误再替换容器。任务和账号须原已启用；正常服务继续按原任务、通知开关运行，已有采集失败告警的恢复默认等待两次正常查询。只重建 `cloud-monitor`，无需重建通知网关或重启 MySQL；完整上传和回滚流程见 [服务器更新](server-update.md)。

## 2026-10-13 RDS 云监控单轮 503

根据服务器历史采集日志和同一时间段的阿里云 SDK 错误日志，已确认北京时间凌晨 `01:49:32` 开始的 `aliyun-mysql-cloudmonitor` 采集请求返回 **HTTP 503 / `ServiceUnavailable`**。这是云监控接口该次请求暂时不可用；日志没有显示权限拒绝、业务数据库故障或昨日行锁累计值的小数校验错误。

该任务属于 `aliyun-hycen`，使用 CMS 云监控补充采集两台 MySQL 的 `MySQL_SlowQueries`，与原生 RDS 行锁采集任务分开。原日志未保留失败的操作名，不能进一步确定该次请求来自 `DescribeMetricList` 还是元数据校验 `DescribeMetricMetaList`。

### 现场证据

以下时间均为 Asia/Shanghai。采集通知记录本轮开始时间，日志记录本轮结束时的时间，因此告警显示 `01:49:32`，失败日志出现在 `01:49:42`。

| 日志时间 | 任务结果 | 说明 |
| --- | --- | --- |
| 01:45:32 至 01:48:32 | 每轮 `ok samples=30` | 失败前正常 |
| 01:49:42.421 | `RdsCloudMonitorError` | 本轮请求失败，SDK 记录 HTTP 503 |
| 01:50:36.274 | `ok samples=30` | 下一轮已经恢复成功，累计 1 个健康样本 |
| 01:51:32.507 | `ok samples=30` | 连续第 2 个健康样本，确认恢复并生成恢复通知 |
| 01:52:32 至 01:54:32 | 每轮 `ok`，样本数 29 或 30 | 后续继续成功，样本数受返回序列影响 |

SDK 记录中的安全字段：

```text
HTTP Status: 503
Error:ServiceUnavailable
RequestID: 01A1313B-E956-5C8A-9586-4D27B906C430
```

`01:48` 至 `01:52` 的其他任务失败与服务启动日志筛选结果，只有本任务这一次失败。此次是**单轮失败**，两分钟后的恢复通知不表示连续失败了两分钟：任务每 60 秒采集一次，`collection_error_samples` 默认为 1，全局 `recovery_samples` 为 2。通知中的“民用数据库、iot-kernel-mysql”是配置范围，不表示两台业务数据库均发生故障。

故障约 10 秒后返回并不足以判定超时；实际查到的 SDK HTTP 状态及错误码是 503 和 `ServiceUnavailable`。这些记录不能说明阿里云服务端更深层的故障原因；向阿里云支持核查时可使用上面的请求编号和时间。

### 当前日志改进

此前 RDS 云监控包装层将所有请求异常转为同一条“check cloud permissions and connectivity”消息，导致任务日志看不到底层 SDK 类别和云错误码。当前版本在任务日志及只读 `diagnose` 输出中保留以下经过白名单校验的字段：

- `action`：`DescribeMetricList` 或 `DescribeMetricMetaList`。
- `error_type`：已知 SDK / 本地异常类型，或失败响应的固定类型 `APIResponseError`。
- `api_code`：已知云 / SDK 错误码，如 `ServiceUnavailable`、`Throttling.User`、`Forbidden.RAM`、`SDK.HttpError`。
- `http_status`：SDK 服务端异常提供的合法整数 HTTP 状态。响应 JSON 的 `Code` 不冒充 HTTP 状态。

离线复现同类服务端错误时，任务错误详情为：

```text
RdsCloudMonitorError: RDS CloudMonitor request failed; check cloud permissions and connectivity; action=DescribeMetricList; error_type=ServerException; api_code=ServiceUnavailable; http_status=503
```

应用生成的详情不包含原始异常消息、响应体、请求头、签名、AK/SK 或请求编号；未知错误码和未知异常类名不直接输出。阿里云 SDK 自身的日志不受这层包装控制，不应把 SDK 原始错误消息直接公开。`SDK.HttpError` 只能确定 SDK 将错误归为传输异常，不能仅凭该码断言超时、DNS 或证书问题。

此次改动只增加诊断详情，采集失败仍按原规则处理，不增加 API 调用、重试或告警等待次数。失败后两次正常采集才确认恢复的行为继续保留。

### 服务器更新与核查

本次异常已经自行恢复，无需为此变更云权限、AK/SK 或业务数据库配置。将当前项目的采集源码同步到服务器原目录，保留 `.env`、整个 `config/`、服务器 Compose 设置和 MySQL 数据，再执行：

```sh
cd /data/monitor/cloud-resource-monitor
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose up -d --no-deps --force-recreate cloud-monitor
docker compose logs --since 5m --tail=150 cloud-monitor 2>&1 | grep -F 'collection job=aliyun-mysql-cloudmonitor'
```

`check --live` 是离线配置检查，不调用云 API；检查失败时先处理错误再替换运行容器。容器替换后继续按现有配置运行正常调度和通知，本次无需重建通知网关或重启 MySQL。完整上传和回滚步骤见 [服务器更新](server-update.md)。

历史事件核查优先读取日志，使用带 `+08:00` 的明确时间范围：

```sh
docker compose logs --no-color --timestamps --since '2026-10-13T01:45:00+08:00' --until '2026-10-13T01:55:00+08:00' cloud-monitor 2>&1 | grep -F 'collection job=aliyun-mysql-cloudmonitor'
```

服务器本次查到的 SDK 状态可通过只输出固定字段的筛选重查，避免公开整个 SDK 消息：

```sh
docker compose logs --no-color --since '2026-10-13T01:49:25+08:00' --until '2026-10-13T01:49:50+08:00' cloud-monitor 2>&1 | grep -oE 'HTTP Status: [1-5][0-9]{2}|Error:ServiceUnavailable|RequestID: [0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}'
```

成功采集会覆盖任务表中的最新失败详情，所以当前 `status` 或新一轮 `diagnose` 不能还原凌晨的历史错误。无需为了已恢复的事件执行 `once`，它会写入告警队列。

## 2026-10-12 RDS 行锁采集失败

本次已根据用户提供的服务器日志和真实只读 API 返回，确认“RDS 数据库性能采集（配置范围：民用数据库）”的连续失败来自行锁累计值的整数校验。13:55 至 14:00 每分钟为 `ok samples=89`，14:01:22 至 14:04:22 每分钟出现以下错误：

```text
RdsPerformanceError: RDS performance response has an invalid or negative metric value; metric=db_row_lock_waits_per_second
```

失败字段为 `MySQL_ROW_LOCK.innodb_row_lock_waits`。此前现场返回的单位为 `int`，但不能据此要求云端采样后的累计值必须是整数；本次时间按 Asia/Shanghai 展示：

| 采样时间 | 原始 waits 值 | 判断 |
| --- | --- | --- |
| 13:45 至 13:59 | `824.0` | 旧版接受等于整数的值 |
| 14:00 | `826.9` | 非负且有限，但旧版因不是整数而报错 |
| 14:01 至 14:04 | `827.0` | 最新值恢复为整数，整轮仍因回看窗口中的 `826.9` 报错 |

任务每轮回看最近 15 分钟，旧版会检查返回序列中的每个点。一个 `826.9` 会在多轮查询中重复出现，因此单次小数采样可能导致约 15 分钟连续失败；CPU、内存等其他指标正常也无法使该任务成功。这个错误文案不表示已经确认数据库返回负数，本次实际问题是代码错误地拒绝了合法小数。

当前版本接受非负、有限的小数累计值，保留十进制精度先差分，再转换为每秒速率，不取整、不补零。原有整数累计值与大整数的精确小增量继续兼容。负数、`NaN`、无限值及非法格式仍报错；累计值下降、采样间隔过大仍重新建立基线，没有有效或新鲜差分仍报告采集异常。原 MySQL 死锁规则与行锁等待规则继续分别保留，账号、任务和通知开关无需变更。

### 服务器最小更新

将当前完整项目中的代码改动同步到服务器原项目目录，保留服务器的 `.env`、云账号与资源信息、任务开关、通知凭据及 MySQL 数据。在该目录执行：

```sh
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml diagnose --job aliyun-mysql-civil-performance
docker compose up -d --no-deps --force-recreate cloud-monitor
docker compose logs --tail=100 cloud-monitor
```

第二条命令使用新镜像，只调用真实只读云 API，不写监控数据库、不创建告警、不发送通知，也不启动调度器；要求服务器上该账号和任务已启用。确认输出 `status: ok` 后执行后两条命令，替换运行容器并查看正常采集日志。样本数取决于实际返回序列，`89` 是此次历史记录，不是必须匹配的固定值。

只重新构建镜像不会替换现有容器；本次仅需更新 `cloud-monitor`，无需重新构建通知网关或变更 MySQL 数据卷。正常服务重建后继续按服务器原有任务和通知开关运行。无需更换 AK/SK、增加业务数据库账号、关闭行锁采集或调整告警阈值。指标含义和时序约束见 [RDS 性能监控](rds-performance.md#mysql-行锁等待)。

## 2026-09-09 日志核查结果

以下记录来自 2026-09-09 的只读云 API 核查，当时没有连接生产数据库、修改云资源或发送通知。修复已纳入当前项目；表格是当时结果，不代表当前云端状态。旧版服务器遇到同类错误时，按下文核对配置后使用完整项目重新部署。

| 任务 | 原因 | 修正后结果 |
| --- | --- | --- |
| `huawei-ecs-expiry` | BSS 接入区域误填 `cn-east-3`，SDK 在请求前抛 `KeyError` | `ok`，33 个样本 |
| `huawei-balance` | 同上 | `ok`，1 个样本 |
| `huawei-slb-expiry` | 同上 | `no_data`，接口成功但未返回匹配的包年包月资源 |
| `huawei-obs-packages` | 同上 | `no_data`，接口成功但未返回匹配套餐 |
| `huawei-certificates` | SCM 接入区域误填；修正后发现另缺证书读取授权 | HTTP `401` / `SCM.4002`，需要 IAM 授权 |
| `huawei-elb-bandwidth` | 当前 ID 对应带宽监控对象，却配置在 `publicip_id` 维度下 | 改为 `bandwidth_id` 后 `ok`，2 个样本 |
| `aliyun-sms-statistics` | 当时统计接口成功返回 `TargetList: []`、`TotalSize: 0`，旧代码报缺少日期 | 该次完全空响应修正为 `no_data`，不伪造增长率；2026-10-19 的部分日期响应见上文 |

`notification ... accepted` 表示通知渠道已接受提交，不证明终端最终送达；`skipped channel_disabled` 表示所选渠道关闭，当前短信关闭时属于预期行为。调度器的 `executed successfully` 只说明调度函数结束，应以 `collection job=... status=...` 为准。

## 旧版配置兼容核对

仅在旧版配置仍使用错误的全局接入区域时，调整 `config/config.yaml` 中华为云账号的以下字段，缩进与 `regions`、`project_ids` 相同：

```yaml
bss_region: cn-north-1
scm_region: cn-north-4
```

这是全局接口的接入区域。业务 `regions`、`project_ids` 以及 CES 指标的 `region` 继续使用资源实际所在的 `cn-east-3`。新版 `check --live` 会在启动前检查这些全局接入区域。

如果旧版 `huawei-elb-bandwidth` 仍将本次核实的带宽 ID 填在 `publicip_id` 下，将两条指标的维度键改为 `bandwidth_id`，原有 ID 字符串保持不变：

```yaml
dimensions: {bandwidth_id: "原配置中的同一个 ID"}
```

此修改基于本次实测：同一 ID 在 `publicip_id` 下近 24 小时无数据，在 `bandwidth_id` 下最近 15 分钟有数据。不要将这一结论套用到其他资源：填写 EIP ID 时应使用 `publicip_id`，填写带宽 ID 时使用 `bandwidth_id`。

两条指标继续使用 `SYS.VPC`、`upstream_bandwidth` / `downstream_bandwidth`、`period: 60`、`multiplier: 1`。当前 `limit: 40000000` 表示 40 Mbps，本轮没有更改上限。上限需与该带宽资源的实际配置一致。

## 按需启用证书采集

没有华为云证书且已关闭 `huawei-certificates` 时，无需为诊断启用该任务。以下权限步骤只适用于需要采集华为云证书的账号。

华为官方错误码说明：`SCM.4002` 表示“您的权限不足。缺少策略”。本项目已有的 [证书全局策略](policies/huawei-collector-global.json) 使用 `scm:cert:list`，无需扩大到证书管理权限。

1. 在华为云 IAM 中进入“角色与策略”，创建自定义策略，使用该 JSON 文件内容。
2. 将策略授权给当前配置 AK 所属 IAM 用户加入的用户组。
3. 授权范围选择“全局服务资源”。只授权区域项目资源，或只使用已有 BSS/RDS/CES 合并策略，不能代替证书全局授权。
4. 授权生效、证书任务及账号启用后，运行证书诊断命令复查。

```sh
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml diagnose --job huawei-certificates
```

不需要更换通知提供方、修改通知服务配置或开通短信测试。

## 服务器更新

按[项目部署说明](../README.md)同步完整项目，保留服务器已有配置、`.env` 和 MySQL 数据，并按上文核对与当前错误有关的配置。以下命令从服务器项目根目录执行；诊断列表只填写已启用且需要检查的任务。

```sh
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml diagnose --job aliyun-sms-statistics --job huawei-ecs-expiry --job huawei-slb-expiry --job huawei-balance --job huawei-obs-packages --job huawei-elb-bandwidth
docker compose up -d --no-deps --force-recreate cloud-monitor notification-gateway
docker compose logs --tail=100 cloud-monitor
```

仅重新构建镜像不会替换正在运行的容器，因此需要执行 `up -d`。两个应用服务共用项目镜像，上述命令使它们加载同一版实现，MySQL 数据卷保持不变。已启用证书任务但权限尚未补齐时，该任务仍会报错，其他采集任务可以运行。

## 诊断命令说明

`diagnose` 不实例化数据库或通知客户端，支持在正常监控运行期间执行。它会调用真实只读云 API，占用相应查询额度。返回逐行 JSON，仅输出任务 ID、账号标识、状态、样本数及已知安全错误详情，不输出指标原始值、AK/SK、请求头或原始接口响应。

- `--job` 可以重复；不指定时检查所有启用账号下的启用任务。
- 指定不存在或禁用任务、或必要采集参数缺失时，先完成全部预检查再决定是否发出请求，避免部分任务已开始查询。
- 短信回执任务依赖本地数据，会返回 `skipped: true`，无需连接数据库。
- 退出码 `0` 表示所选任务成功、无数据或统计不足，`1` 表示存在采集失败，`2` 表示配置预检查失败。
- `no_data` 仅表示没有生成指标，不等于资源健康、额度为零或历史告警已经恢复。
- `insufficient_data` 表示查询成功但无法计算短信增长率，返回 `samples: 0` 和安全原因，不会伪造零值或恢复历史短信业务告警。
- 对未知异常仍只记录类型，避免供应商异常消息携带敏感字段；已知错误会保留操作名、HTTP 状态、错误码或本地验证原因。

需要排查时优先使用 `diagnose`。`once` 会写入数据库及告警队列，后续正常服务可能发送这些通知。

## 官方依据

- [华为云 SCM 错误码](https://support.huaweicloud.com/api-ccm/scm.html)：`SCM.4002` 的原因与处理方法。
- [ListCertificates](https://support.huaweicloud.com/api-ccm/ListCertificates.html)：需要 `scm:cert:list`。
- [CCM 权限管理](https://support.huaweicloud.com/productdesc-ccm/ccm_01_0007.html)：授权范围为全局服务。
- [弹性公网 IP 监控指标](https://support.huaweicloud.com/usermanual-eip/monitor_0002.html)：`SYS.VPC` 指标、`bit/s` 单位和 `publicip_id` / `bandwidth_id` 维度。
