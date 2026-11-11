# 最新代码与服务器更新

更新日期：2026-11-03。最新采集失败确认次数调整、估算质量处理及此前华为 ECS/RDS 内网估算替换见下文。华为 ECS 空清单停采的恢复、指定实例范围、RDS 单位兼容和首次接入步骤保留在后续章节。告警表显示修正见 [告警明细](grafana-alert-details.md)，指标口径、支持范围与初始阈值见 [网络监控](network-traffic.md)。所有命令逐条执行，失败时先处理，不要继续上线。

## 2026-11-03 采集失败连续两次确认

按用户确认，于北京时间 **2026-11-03 09:59:03** 在 `192.168.120.28`
原目录 `/data/monitor/cloud-resource-monitor` 完成配置更新并验证健康。
以下十项任务设置 `collection_error_samples: 2`，其他任务继续原设置：

```text
config/config.yaml:
  aliyun-slb-bandwidth
config/rds-performance.yaml:
  aliyun-mysql-civil-performance
  aliyun-mysql-iot-performance
  aliyun-mysql-cloudmonitor
  aliyun-yuanzhu-mysql-changsha-readonly-performance
  aliyun-yuanzhu-mysql-national-readonly-performance
  aliyun-yuanzhu-mysql-modian-performance
  aliyun-yuanzhu-mysql-wuhan-performance
  aliyun-yuanzhu-mysql-cloudmonitor
config/network-traffic.yaml:
  huawei-ecs-network
```

恢复继续使用 `recovery_samples: 2` 和 `notify_recovery: true`。
华为五分钟任务的首轮失败后提醒确认比之前约晚五分钟；阿里云一分钟任务约晚一分钟。
真实采集错误仍记录为错误，原数据新鲜度限制保持不变，华为源数据仍为 900 秒。
已有告警和队列未手动清理；已触发的告警仍按原连续健康次数确认恢复。
这次调整减少瞬时波动邮件，不能修复源数据延迟；依据见 [排查与回放](troubleshooting-collection.md#2026-11-03-失败与恢复邮件反复出现)。

| 核验 | 结果 |
| --- | --- |
| 本地离线测试 | `test_engine.py` 与 `test_network_estimate_quality.py` 合计 29 通过 |
| 配置修改范围 | YAML 结构比较确认仅指定十项字段；凭据、开关、清单、业务规则、周期和新鲜度不变 |
| 服务器离线配置检查 | 修改前、修改后、激活后 `check --live` 均通过：8 账号、71 任务、59 规则 |
| 激活 | 仅重启原 `cloud-monitor`，健康状态 `healthy`，镜像未变 |
| 依赖服务 | MySQL 和通知网关容器及启动时间未变 |

部署入口 [deploy_collection_confirmation.ps1](../scripts/deploy_collection_confirmation.ps1)
使用 SSH 密码框，以服务器现有 YAML 为基础修改字段，不上传本地完整配置覆盖服务器。
写入前创建受限权限备份，写后验证其他配置文件与 `.env` 指纹不变；失败时尝试恢复原配置。
本次没有构建镜像、运行 `once`、调用云 API 做验证、启动额外调度器或发送测试通知。
正式采集服务重启后继续正常调度和通知，后续实际邮件数量取决于真实故障情况。

服务器备份为 `.deploy-backups/collection-confirmation-20261103-095850`，包含原三份配置、
配置指纹及 `activated` 验证标记。本地脱敏部署输出为
`work/collection-confirmation-deploy-20261103.txt`。

需要回滚且这三份配置之后未发生其他修改时，可在原服务器目录逐条执行：

```sh
cd /data/monitor/cloud-resource-monitor
cp .deploy-backups/collection-confirmation-20261103-095850/config/config.yaml config/config.yaml
cp .deploy-backups/collection-confirmation-20261103-095850/config/rds-performance.yaml config/rds-performance.yaml
cp .deploy-backups/collection-confirmation-20261103-095850/config/network-traffic.yaml config/network-traffic.yaml
docker compose exec -T cloud-monitor python -m cloud_monitor --config /app/config/config.yaml check --live
docker compose restart -t 120 cloud-monitor
docker compose ps cloud-monitor notification-gateway mysql
```

若之后有其他配置变动，应只回退上述十项的失败确认次数，不能整份覆盖旧备份。

## 2026-10-30 华为内网估算质量处理

已在 `192.168.120.28` 的 `/data/monitor/cloud-resource-monitor` 更新并只替换 `cloud-monitor` 容器。有限负差单列 `degraded`，不生成零值或采集失败通知；真实 API、格式、非有限值、缺失/过期数据及映射错误仍为采集失败。持久化按地域与资源隔离，重启、旧样本及其他资源成功不能清除无效水位；新有效双向配对点恢复估算时重新累计业务告警连续性。完整规则见 [估算可用性](network-traffic.md#估算可用性)。

| 核验 | 结果 |
| --- | --- |
| 离线测试 | 1,770 通过、18 跳过；Ruff、Grafana 生成检查通过 |
| 候选配置 | `check --live` 通过，8 个账号、71 项任务、59 条规则 |
| 只读云诊断 | ECS `ok`、66 个样本；RDS `ok`、6 个样本 |
| 正式服务首轮 | ECS 33 台、RDS 3 台均可用；72 条网络当前序列，36 条质量序列 |
| 新质量路径 | 首轮未遇负差；使用真实负差回放及离线回归验证不可用、重启、严格恢复和通知取消 |
| 容器和配置 | 采集服务健康；MySQL、通知网关容器 ID 未变；原 `.env` 和 `config/` 文件指纹一致 |
| Grafana | 通用 JSON 已同步新增可用性表；电脑操作工具无法可靠识别浏览器 URL，未修改线上现用看板 |

没有运行 `once`、额外生产调度器或测试通知。正式替换后恢复既有调度，真实通知仍按原配置发送。源、周期、阈值、账号/任务/渠道开关和数据库结构均未改动。

部署入口为 [deploy_network_estimate_quality.ps1](../scripts/deploy_network_estimate_quality.ps1)，使用原生 SSH 密码框。首次普通 Dockerfile 构建因 PyPI 下载超时中止，旧服务未被替换；之后校验 `pyproject.toml` 与原镜像完全相同，复用原镜像的依赖，只覆盖八个变更模块，再通过只读检查后激活。该入口只适用于依赖未改变的本次代码更新，不安装新依赖。首次失败后的续跑方式如下；日后重新部署省略 `-ResumeBackup`，自动创建新备份：

```powershell
.\scripts\deploy_network_estimate_quality.ps1 -ResumeBackup '.deploy-backups/network-quality-20261030-150442'
```

上述续跑仅适用于原容器尚未替换的阶段；正式上线后不再以该备份续跑。服务器回滚备份为 `.deploy-backups/network-quality-20261030-150442`，旧镜像为 `cloud-resource-monitor:before-network-quality-20261030-150442`。需要回退时，保留配置、MySQL、通知网关和质量侧记录，仅回退采集镜像：

```bash
cd /data/monitor/cloud-resource-monitor
docker image tag cloud-resource-monitor:before-network-quality-20261030-150442 cloud-resource-monitor:1.0.0
docker compose up -d --no-deps --no-build --pull never --force-recreate cloud-monitor
docker compose ps cloud-monitor notification-gateway mysql
```

直接重建源码会再次使用新代码，不能代替回滚镜像。历史采样及已发送通知不回写；已取消的过时业务恢复通知不会自动重建。通用看板文件位于 `grafana/cloud-resource-monitor.json`，未将其整体覆盖到现用业务看板。

## 2026-10-27 华为内网估算直接替换

已于北京时间 **2026-10-27 18:19:52 至 18:20 左右**在 `192.168.120.28` 的 `/data/monitor/cloud-resource-monitor` 替换 `cloud-monitor` 和独立的 `notification-gateway`。原华为 ECS/RDS 两项任务与四条网络业务规则直接使用 `scope=intranet_estimated`，不再并行保留总量业务告警；原任务、规则 ID、阈值数值与持续时间不变。

部署以服务器当前配置为基础，只改华为两项任务的范围、300 秒周期及 RDS 1800 秒回看，并改四条规则的范围和中文摘要。没有上传本地完整配置覆盖线上文件，账号/任务开关、渠道和凭据保留；两个阿里云 ECS 清单仍分别为 `aliyun-ecs-network: 0`、`aliyun-yuanzhu-ecs-network: 3`。MySQL 容器沿用原实例，没有重建或清空数据卷。

| 核验阶段 | 已确认结果 |
| --- | --- |
| 候选镜像配置检查 | `check --live` 通过 |
| 候选镜像只读诊断 | ECS 33 台、66 个双向样本；RDS 3 台、6 个双向样本；均为 `ok`、`partial_failures=0` |
| 候选样本口径 | 全部 `scope=intranet_estimated`、`source=total_minus_eip` |
| 正式采集 | 生产日志连续两轮均为 ECS 66、RDS 6 个样本，任务均 `ok` |
| 服务状态 | `cloud-monitor`、`notification-gateway` 健康；MySQL 容器未变 |
| 指标导出 | 72 条网络当前序列，全部 `scope=intranet_estimated`、`source=total_minus_eip`，不再导出这两项任务的旧总量当前值 |
| Prometheus 实际图表查询 | 运行明细面板 8/9 各返回 33 条，12/13 各返回 3 条新鲜序列 |
| 监控库只读 SQL | 旧总量网络告警/候选状态 0，旧总量待发送/失败重试消息 0；MySQL 容器 ID 与操作前一致 |
| Grafana | 运行明细原 8/9/12/13 四图已保存并重新加载，标题和查询均改为内网估算，MB/s、参考线与独立时间控件保留 |

上述候选诊断、两轮正式采集、实际图表 Prometheus 查询及监控库只读 SQL 核验均已通过。本次未为验证额外发送测试通知。问题看板 `cloud-business-dev-v1` 的面板 2 主 ECharts 已保存，退出编辑器并刷新后确认分页、搜索、表格和内网估算文案持久化；实际新告警示例为“RDS 内网出站（估算）：355.27 Mbps；达到 200 Mbps，持续 5 分钟”。浏览器复核截图：[ECS 四图中的华为部分](../preview/grafana-huawei-intranet-ecs.png)、[RDS](../preview/grafana-huawei-intranet-rds.png)、[告警明细](../preview/grafana-huawei-intranet-alert.png)。最终离线测试为 1,717 通过、18 跳过，Ruff 通过。

阿里云后续只读探测也已成功：两个账号合计 7 台 RDS，5 个公网 IP 在各自账号/地域可见的 EIP 清单中均无匹配，另 2 台未返回公网地址；报告没有缺失权限动作。本次无需再追加查询权限。该结果仍未取得可用于相减的独立公网吞吐，阿里云 RDS 继续明确显示总量；证据边界见 [阿里云 RDS 核对](aliyun-rds-network-scope-audit.md)。

计算仍遵循同方向、同一已闭合的 300 秒平均窗口：总入站减全部已关联 EIP 公网入站，总出站减全部已关联 EIP 公网出站。完整关联、所有源时间戳及单位严格校验，缺失、过期、冲突或负差不输出、不补零；未经过这些 EIP 的公网路径仍可能留在差值中，故保留“估算”。公式与限制见 [网络监控](network-traffic.md)，IAM 与现场证据见 [内外网核对](huawei-network-scope-audit.md)。

### 本次使用的部署入口

当前项目保存了 [deploy_huawei_intranet.ps1](../scripts/deploy_huawei_intranet.ps1)，使用原生 SSH 密码弹框输入密码，不把密码写到参数或聊天。三个阶段应按顺序运行，上一阶段失败时先处理，不继续激活：

```powershell
.\scripts\deploy_huawei_intranet.ps1 -Phase Stage
.\scripts\deploy_huawei_intranet.ps1 -Phase Activate
.\scripts\deploy_huawei_intranet.ps1 -Phase Verify
```

`Stage` 备份原配置、相关源码、容器标识和旧镜像，上传限定文件，结构化修改现有华为任务/规则，再构建和只读诊断，不额外启动调度器。`Activate` 正式重建采集与网关两个容器，恢复原调度并沿已有启用渠道处理真实告警；`Verify` 只读检查容器、采集日志、指标接口、Prometheus 和监控库状态，不触发测试通知。辅助的阿里云 RDS/EIP 探测若缺权限，会明确输出未完成和所需动作，不因此把阿里 RDS 标为内网。

### 备份与回滚

本次服务器备份目录为 **`.deploy-backups/huawei-intranet-20261027-181047`**，网络配置备份为其中的 `network-traffic.yaml`，已有源码按原相对路径备份。旧镜像标签为 **`cloud-resource-monitor:before-huawei-intranet-20261027-181047`**。运行明细修改前导出保存在项目 [huawei-intranet-runtime-before.json](../preview/validation/huawei-intranet-runtime-before.json)，应用前后 JSON 均在本项目内。

需要回退采集口径时，在服务器原项目目录逐条执行。以下沿用现有 Compose 项目及 `cloud-resource-monitor:1.0.0` 服务镜像标签，不重新构建新源码：

```bash
cd /data/monitor/cloud-resource-monitor
cp -p .deploy-backups/huawei-intranet-20261027-181047/network-traffic.yaml config/network-traffic.yaml
docker image tag cloud-resource-monitor:before-huawei-intranet-20261027-181047 cloud-resource-monitor:1.0.0
docker compose config --quiet
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose up -d --no-deps --no-build --pull never --force-recreate cloud-monitor notification-gateway
docker compose ps cloud-monitor notification-gateway mysql
```

回滚配置与镜像后，如需恢复旧图，把修改前运行明细导出应用回同一 UID 并保存；不要将总量数据重命名为内网。源码备份用于后续明确需要恢复旧源码再构建时使用，直接执行构建会重新使用当前源码，不能视为已回到备份镜像。

新版本启动时会清理旧总量活动/候选状态及未发送/失败重试消息，不产生迁移恢复通知；已发送记录和历史采样保留。回滚不会自动还原已经清理的告警状态，原总量规则需从回滚后的新有效样本重新累计。无需停止 MySQL 或执行 `docker compose down`，不要删除数据卷或回写整个历史数据库。

## 2026-10-26 恢复华为 ECS 自动发现

线上只读核查确认，`huawei-ecs-network` 已启用，但 `options.instances: []` 明确选择零台实例，最近每轮均为 `no_data samples=0`。最后有效源样本为北京时间 2026-10-23 10:05，网络配置最后修改于同日 10:05:38。空清单会跳过云 API，不能据此判断华为流量接口故障；同期华为 RDS 网络、余额和 ELB 仍正常采集。

本次仅移除华为 ECS 任务的 `options.instances` 字段，恢复账号/任务指定地域自动发现，保留原五分钟周期、900 秒最大年龄和既有规则。两个阿里云 ECS 任务继续使用各自清单，不能用本地模板覆盖生产配置。无需修改凭据、账号/任务开关、RDS、阈值、通知渠道或 Grafana，也无需构建新镜像。

在服务器原项目目录备份当前配置，然后只编辑华为 ECS 的这一字段。以下沿用现有 Compose 项目设置：

```bash
cd /data/monitor/cloud-resource-monitor
umask 077
huawei_ecs_backup=".deploy-backups/huawei-ecs-restore-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$huawei_ecs_backup"
cp -p config/network-traffic.yaml "$huawei_ecs_backup/network-traffic.yaml"
vi config/network-traffic.yaml
docker compose config --quiet
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml diagnose --job huawei-ecs-network
```

`check --live` 仅作离线配置检查；`diagnose` 查询真实只读云 API，不连接监控数据库、不写通知队列、不发送通知、不启动调度器。诊断应返回 `status: ok` 且非零 `samples`，失败时先处理错误，不继续替换容器。通过后复用已有镜像，只重新创建采集服务：

```bash
docker compose up -d --no-deps --no-build --pull never --force-recreate cloud-monitor
docker compose ps cloud-monitor
docker compose logs --no-color --since 10m cloud-monitor 2>&1 | grep -F 'collection job=huawei-ecs-network'
```

这一步正式恢复正常调度，可能沿原渠道发送实际告警或恢复通知；不额外发送测试通知。核对任务为 `ok`、样本数大于零，及 Prometheus 中该任务的新鲜入站、出站序列。图表从恢复后的新样本继续累积，停采期间不自动补历史。需要回退时，将上面记录的备份文件恢复到 `config/network-traffic.yaml`，离线校验后以相同命令重新创建采集服务。

本次已于北京时间 2026-10-26 09:51 左右在 `192.168.120.28` 应用，配置备份为 `.deploy-backups/huawei-ecs-discovery-20261026-095053/network-traffic.yaml`。服务器离线检查通过，只读诊断和正式采集均为 `ok`、66 个样本；09:52 复查 Prometheus 及本项目华为 ECS 两张图的完整查询，入站、出站各返回 33 条新鲜实例序列，最新源样本为 09:50。`/healthz` 返回 `ok`，沿用原采集镜像，MySQL 与通知网关容器 ID 在操作前后相同。本地仅同步华为发现方式，两个阿里云任务的生产清单未被本地模板覆盖。

## 2026-10-23 补齐 yuanzhu MySQL 性能

已在 `192.168.120.28` 的 `/data/monitor/cloud-resource-monitor` 部署配置补充。该账号的密钥和账号开关均已生效，但此前只有关闭且未填实例的 PostgreSQL 模板；实际云端是五台 MySQL 8.0。AccessKey 不会自动生成性能任务，实例与任务清单见 [RDS 性能配置](rds-performance.md#配置和开关)。

本次仅向服务器的 `config/rds-performance.yaml` 增加五个独立原生性能任务和一个批量慢查询任务，再重建 `cloud-monitor` 容器载入配置，沿用原镜像。五台均采集 CPU、内存、磁盘、连接、行锁及慢查询，三台已验证的高可用主实例还采集复制延迟。两台只读实例的复制延迟返回单位不受支持，暂未启用该项。原有账号、任务开关、告警阈值和通知渠道保留。

`aliyun-yuanzhu-rds-network` 原本已正常运行，不需要增加重复任务。服务器 Prometheus 已确认该账号五台实例的性能和入站、出站数据均为新鲜样本；六个新增任务和原 RDS 网络任务均为 `ok`。实际趋势图位于看板 UID `cloud-business-runtime-v1`（云业务与开发 · 运行明细），其 CPU、内存、磁盘、连接和阿里云 RDS 入站、出站六张图的实际查询在选择该账号时均返回五条实例序列。

原性能配置及原镜像已备份，配置路径为 `.deploy-backups/yuanzhu-rds-20261023-103611/rds-performance.yaml`，镜像标签为 `cloud-resource-monitor:before-yuanzhu-rds-20261023-103611`。此次无需构建镜像或重建通知网关和 MySQL。需要回退本次配置时，在服务器项目目录执行以下两条命令：

```bash
cp -p .deploy-backups/yuanzhu-rds-20261023-103611/rds-performance.yaml config/rds-performance.yaml
docker compose up -d --no-deps --no-build --pull never --force-recreate cloud-monitor
```

验证包括离线配置检查、服务器 `check --live`、六项只读云 API 诊断、正式服务首轮采集、Prometheus 新鲜指标及看板实际查询。未发送测试通知，未连接业务数据库，也未额外启动生产调度器。

## ECS 网络速率按实例清单采集

2026-10-23：ECS 网络任务支持 `options.instances: []` 明确选择零台，非空清单只采集所列实例的入站、出站速率。省略此字段会自动发现；需限定范围时在对应任务填写清单，见 [配置示例](network-traffic.md#只采集指定-ecs)。2026-10-26 当前项目的华为 ECS 已恢复自动发现，两个阿里云 ECS 任务保留各自清单，本地阿里云空清单请填写后再部署；空清单不采集任何 ECS。

以下适用于服务器已经运行网络监控的环境，原项目路径按 `/data/monitor/cloud-resource-monitor` 编写，路径不同则替换为实际目录。所有 Compose 命令沿用原有 `-p`、`-f` 或 `COMPOSE_PROJECT_NAME`；镜像标签默认是 `cloud-resource-monitor:1.0.0`，自定义标签时使用实际标签。如果尚未接入网络任务，改用后面的 [首次接入步骤](#网络监控首次接入)。

1. **服务器：先备份现有 ECS 清单和当前采集镜像。** 不停止正在运行的容器，逐条执行，记录最后输出的备份 ID：

```bash
cd /data/monitor/cloud-resource-monitor
docker compose ps cloud-monitor notification-gateway mysql
umask 077
ecs_selection_stamp=$(date +%Y%m%d-%H%M%S)
ecs_selection_backup=".deploy-backups/ecs-selection-$ecs_selection_stamp"
mkdir -p "$ecs_selection_backup"
chmod 700 "$ecs_selection_backup"
cp -p config/network-traffic.yaml "$ecs_selection_backup/network-traffic.yaml"
docker image tag "$(docker inspect --format '{{.Image}}' "$(docker compose ps -q cloud-monitor)")" "cloud-resource-monitor:before-ecs-selection-$ecs_selection_stamp"
printf 'ECS selection backup ID: %s\n' "$ecs_selection_stamp"
```

备份或当前容器检查失败时先处理，不要继续上传。不要执行 `docker compose down -v`。

2. **本机 Windows PowerShell：上传源码和依赖文件到原项目。** 将 SSH 地址替换为实际地址，非默认 SSH 端口为每条 `scp` 增加 `-P 实际端口`：

```powershell
Set-Location 'C:\Users\77528\Documents\Codex\2026-09-08\new-chat\outputs\cloud-resource-monitor'
$DeployTarget = 'root@服务器IP'
scp .\cloud_monitor\config.py .\cloud_monitor\resource_selection.py .\cloud_monitor\alerting.py .\cloud_monitor\store.py .\cloud_monitor\prometheus.py .\cloud_monitor\service.py "${DeployTarget}:/data/monitor/cloud-resource-monitor/cloud_monitor/"
scp .\cloud_monitor\providers\network_traffic.py .\cloud_monitor\providers\huawei.py "${DeployTarget}:/data/monitor/cloud-resource-monitor/cloud_monitor/providers/"
scp .\pyproject.toml "${DeployTarget}:/data/monitor/cloud-resource-monitor/pyproject.toml"
```

共 9 个文件。`service.py` 包含服务初始化时的旧告警清理调用，不能仅更新 `store.py`。同时上传 `huawei.py` 与 `pyproject.toml`，使尚未部署华为 ECS 名称修复的服务器具备兼容代码和 SDK 依赖。如在服务器使用估算工具，另上传 `scripts/estimate_api_usage.py`。保留远端其它源码文件及 `__init__.py`，无需删除文件。

3. **服务器：编辑现有网络配置。**

```bash
cd /data/monitor/cloud-resource-monitor
vi config/network-traffic.yaml
```

在对应 ECS 任务的现有 `options` 中添加或替换 `instances`，入站、出站共用清单。例如 `aliyun-ecs-network`：

```yaml
    options:
      product: ecs
      instances:
        - resource_id: i-your-instance
          resource_name: 应用服务器
          region: cn-hangzhou
      period: 60
      inventory_cache_seconds: 300
      max_age_seconds: 300
```

`aliyun-ecs-network`、`aliyun-yuanzhu-ecs-network`、`huawei-ecs-network` 分别填写各自账号的实例；不采集某个任务的 ECS 时设 `instances: []`，删除字段会恢复自动发现。华为云保留原采样周期及最大年龄，填写 UUID 和正确地域，不照抄阿里云示例的时间参数。详细字段见 [只采集指定 ECS](network-traffic.md#只采集指定-ecs)。

只编辑 ECS 清单，保留账号/任务开关、RDS、阈值和渠道配置。不要上传本地空清单覆盖生产文件，不上传 `.env`、主配置、通知配置或本地 Compose。生产主配置应已引用 `network-traffic.yaml`，Compose 应已只读挂载该文件；两处缺失时按首次接入步骤追加。

4. **服务器：构建并检查，然后替换采集容器。** 逐条执行，检查失败时先修复，不继续上线：

```bash
docker compose config --quiet
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
```

`check --live` 应返回 `"valid": true`；它只校验实际配置和凭据是否填写，不查询云 API、不连接数据库、不发送通知，也不证明云权限或数据库连通性。若需核对已启用且填写了非空清单的 ECS 任务，可在替换前运行只读诊断，例如：

```bash
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml diagnose --job aliyun-ecs-network
```

仅选择实际启用、已填写清单的任务。诊断查询只读云 API，不写数据库、不评估告警、不启动调度器、不发送通知；期望有数据时应返回 `status: ok` 且 `samples` 大于零。空清单的 `no_data` 是正常结果。检查通过后上线：

```bash
docker compose up -d --no-deps --no-build --pull never --force-recreate cloud-monitor
docker compose ps cloud-monitor notification-gateway mysql
docker compose logs --since 10m --tail=100 cloud-monitor
```

这一步应用清单并恢复正常调度，可能沿已有渠道发送真实告警。等待约一分钟再查看容器健康；阿里云 ECS 按分钟采样，华为云 ECS 按五分钟采样。非空清单的日志应出现对应任务 `status=ok` 和非零 `samples`；空清单则为 `status=no_data`、`samples=0`。

移出清单实例的旧网络告警和未发送重试在初始化时取消，不发送恢复通知；采集健康告警保留。当前值在下一次 Prometheus 抓取后更新，已有历史曲线不删除；看板无需重新导入。通知网关、MySQL、中转和 Prometheus 无需替换。后续只改实例清单时可省略构建，先检查再重新创建 `cloud-monitor`，不能仅刷新 Grafana。

5. **需要回滚时：恢复备份清单和旧采集镜像。** 将 ID 换成第 1 步记录的实际备份 ID，逐条执行，任何检查失败都先停止：

```bash
cd /data/monitor/cloud-resource-monitor
ecs_selection_stamp=YYYYMMDD-HHMMSS
ecs_selection_backup=".deploy-backups/ecs-selection-$ecs_selection_stamp"
test -f "$ecs_selection_backup/network-traffic.yaml"
docker image inspect "cloud-resource-monitor:before-ecs-selection-$ecs_selection_stamp" >/dev/null
cp -p "$ecs_selection_backup/network-traffic.yaml" config/network-traffic.yaml
docker image tag "cloud-resource-monitor:before-ecs-selection-$ecs_selection_stamp" cloud-resource-monitor:1.0.0
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose up -d --no-deps --no-build --pull never --force-recreate cloud-monitor
```

回滚恢复的是旧容器代码和旧采集范围。已取消的待发送通知与已清理的告警候选不会由镜像回滚自动恢复，后续按正常新采样重新判断。服务器源码仍为新版，后续重新构建时会再次生成新版镜像。

本次只在本地修改并离线验证，未操作生产服务。

## 七牛余额告警去重

现金余额与可用余额是官方接口的不同字段，原来两条规则使用相同的充值阈值。此次仅保留 `qiniu-available-low`：CNY 可用余额 ≤500 元为 P1、≤100 元为 P0。`qiniu-cash-low` 已删除并从 `qiniu-balance.rules` 解绑，现金余额仍继续采集展示；账号、任务及通知渠道开关保持原配置。

只改配置会留下历史现金余额告警和重试通知，因此同时更新启动清理逻辑：采集服务启动时清理已删除或已解绑业务规则的旧状态，取消其 `pending`、`failed` 通知，不发送恢复通知；`collection-error` 采集失败告警保留。已发送消息和采集历史数据仍保留。本次只在本地修改与离线验证，尚未部署生产。

先将 `cloud_monitor/store.py`、`cloud_monitor/service.py` 同步到服务器原项目的对应路径，并在服务器现有 `config/integrations/qiniu.yaml` 中移除 `qiniu-cash-low` 规则、将 `qiniu-balance.rules` 改为 `[qiniu-available-low]`。保留服务器的中转地址、令牌、采集参数及所有开关；不要用示例配置覆盖生产凭据。所有 Compose 命令沿用原有的 `-p`、`-f` 或 `COMPOSE_PROJECT_NAME`。

在服务器逐条构建、校验并替换采集容器：

```bash
cd /data/monitor/cloud-resource-monitor
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose up -d --no-deps --no-build --pull never --force-recreate cloud-monitor
docker compose ps cloud-monitor notification-gateway mysql
docker compose logs --since 10m --tail=100 cloud-monitor
```

`check --live` 仅校验配置，不查询云 API、不写告警状态、不发通知，应先返回 `valid: true`。替换容器后才应用启动清理并恢复正常调度，后续符合条件的可用余额告警仍按已有渠道发送。默认两个服务共用镜像标签，本次只替换 `cloud-monitor`；通知网关、中转、MySQL、Grafana 和 Prometheus 无需重启。无需重新导入看板，等待下一次 Prometheus 抓取后刷新，确认现金余额旧告警消失；可用余额仍超限时应保留对应告警。

## 华为云 ECS 实例名称修复

现有网络图已经按 `resource_name (resource_id)` 展示。此前华为云 ECS 的 CES 发现只返回实例 ID，程序又把 ID 填进名称，所以同一个 UUID 显示两次。本次补充 ECS `ListServersDetails`，按地域和 ID 批量取控制台名称。默认随清单缓存 300 秒；图例和新采样使用名称，资源唯一键仍使用 ID。历史 UUID 标签不会回写，所选范围包含更新前数据时可能同时出现旧图例。

仅需更新采集服务的三个文件：`cloud_monitor/providers/huawei.py`、`cloud_monitor/providers/network_traffic.py`、`pyproject.toml`。依赖新增 `huaweicloudsdkecs==3.1.213`，不能只上传两个 Python 文件后沿用旧依赖。无需重新导入 Grafana、修改流量单位、阈值或业务数据库；通知网关也无需替换。以下步骤适用于已完成网络监控首发的环境，保留原 Compose 项目名和所有生产配置。

1. 由有权管理 IAM 的管理员，在采集用户已有的华为云 **1.1 自定义策略**中追加只读项 `ecs:cloudServers:list`，授权到资源所属区域项目。可对照 [合并策略](policies/huawei-collector-combined.json) 与 [权限说明](huawei-permissions.md)。不要替换原有授权项；服务器上的 JSON 文件不会自动更改云端权限。
2. 先在服务器保留当前镜像的回滚标签：

```bash
cd /data/monitor/cloud-resource-monitor
ecs_name_stamp=$(date +%Y%m%d-%H%M%S)
docker image tag "$(docker inspect --format '{{.Image}}' "$(docker compose ps -q cloud-monitor)")" "cloud-resource-monitor:before-ecs-name-$ecs_name_stamp"
printf 'ECS name backup ID: %s\n' "$ecs_name_stamp"
```

3. 在本机 PowerShell 上传到原源码目录，替换 SSH 地址，不覆盖生产配置：

```powershell
Set-Location 'C:\Users\77528\Documents\Codex\2026-09-08\new-chat\outputs\cloud-resource-monitor'
$DeployTarget = 'root@服务器IP'
scp .\cloud_monitor\providers\huawei.py .\cloud_monitor\providers\network_traffic.py "${DeployTarget}:/data/monitor/cloud-resource-monitor/cloud_monitor/providers/"
scp .\pyproject.toml "${DeployTarget}:/data/monitor/cloud-resource-monitor/pyproject.toml"
```

4. 在服务器构建并只读诊断；这些命令不启动生产调度、不写业务数据库、不发通知：

```bash
cd /data/monitor/cloud-resource-monitor
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml diagnose --job huawei-ecs-network
```

`check --live` 应返回 `valid: true`，诊断应有有效样本。名称是附加元数据，查询失败仍会保留流量，所以仅 `status: ok` 不能证明名称查询成功：同时检查输出是否有 `name lookup failed` 或 `names missing`，前者应排查新增权限、项目及 ECS API 连通性，后者也可能是已删除实例。`options.instances` 显式清单跳过查询，需要自行填写 `resource_name`。

5. 检查通过后，正式替换采集容器。这一步会恢复调度，并可能触发已有告警及通知：

```bash
docker compose up -d --no-deps --no-build --pull never --force-recreate cloud-monitor
docker compose ps cloud-monitor notification-gateway mysql
docker compose logs --since 10m --tail=100 cloud-monitor
```

在现有“云业务与开发 · 问题看板”的华为云 ECS 网络图选择最近范围并刷新，核对新采样的图例为实例名称和 ID。实例后续改名默认最多等待一个清单缓存周期。仅需回滚本次采集代码时，将 `ecs_name_stamp` 换为记录的 ID，确认备份镜像存在，再把它标记回原镜像名并重建指定容器；配置和数据卷保持原状：

```bash
ecs_name_stamp=YYYYMMDD-HHMMSS
docker image inspect "cloud-resource-monitor:before-ecs-name-$ecs_name_stamp" >/dev/null || exit 1
docker image tag "cloud-resource-monitor:before-ecs-name-$ecs_name_stamp" cloud-resource-monitor:1.0.0
docker compose up -d --no-deps --no-build --pull never --force-recreate cloud-monitor
```

本次在本地离线验证，未修改云端 IAM、部署服务器或发送真实通知。

## 华为云 RDS 单位兼容修复

生产只读诊断已取得 `SYS.RDS` 的 `rds004_bytes_in` 数据，单位为 `byte/s`，随后旧代码报 `CES network rate has an unexpected unit`。这说明当前失败点是返回单位校验，补 IAM 权限不能解决该错误。现按每个数据点的实际单位换算：`Byte/s`、`B/s`、`byte/s`、`Bytes/s`、`bytes/s` 乘 8；`KiB/s` 及本 RDS 已兼容的 `KB/s`、`KByte/s` 乘 8192。未知或缺失单位继续报错，输出仍为 bit/s，原告警阈值不变。

本节适用于网络配置、挂载和新版通知网关已经部署的环境。只更新采集服务，无需改云端 IAM、生产配置、数据库、通知网关或 Grafana。尚未完成网络首发时，改用下方第 1-6 节，并同步本次修复后的源码。所有 Compose 命令继续沿用原 `-p`、`-f` 或 `COMPOSE_PROJECT_NAME`；默认镜像标签如下，自定义标签时替换为实际标签。

先在服务器为当前采集镜像保留回滚标签，记录输出的 ID；无需停止正在运行的服务：

```bash
cd /data/monitor/cloud-resource-monitor
unit_fix_stamp=$(date +%Y%m%d-%H%M%S)
docker image tag "$(docker inspect --format '{{.Image}}' "$(docker compose ps -q cloud-monitor)")" "cloud-resource-monitor:before-rds-unit-$unit_fix_stamp"
printf 'RDS unit backup ID: %s\n' "$unit_fix_stamp"
```

然后在本机 Windows PowerShell 上传以下三个文件到服务器原源码目录，把 `服务器IP` 换为实际 SSH 地址；保留远端 `__init__.py` 和其它文件。当前源码已包含 ECS 名称查询，需同步依赖文件，以便镜像安装 ECS SDK：

```powershell
Set-Location 'C:\Users\77528\Documents\Codex\2026-09-08\new-chat\outputs\cloud-resource-monitor'
$DeployTarget = 'root@服务器IP'
scp .\cloud_monitor\providers\huawei.py .\cloud_monitor\providers\network_traffic.py "${DeployTarget}:/data/monitor/cloud-resource-monitor/cloud_monitor/providers/"
scp .\pyproject.toml "${DeployTarget}:/data/monitor/cloud-resource-monitor/pyproject.toml"
```

回到服务器逐条构建、校验和只读诊断：

```bash
cd /data/monitor/cloud-resource-monitor
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml diagnose --job huawei-rds-network
```

`check --live` 应返回 `"valid": true`，不调用云 API。`diagnose` 会查询真实只读云 API，但不写数据库、不发通知、不启动调度器；本例任务应返回 `status: ok` 且 `samples` 大于零。`no_data` 不能证明修复成功，仍报 `error` 时先处理，不要继续替换。成功后再上线采集服务：

```bash
docker compose up -d --no-deps --no-build --pull never --force-recreate cloud-monitor
docker compose ps cloud-monitor notification-gateway mysql
docker compose logs --since 10m --tail=100 cloud-monitor
```

这一步会正式启动调度，并可能沿现有渠道发送告警和恢复通知。默认两服务共用镜像标签，但只替换指定的 `cloud-monitor` 容器；已运行的网关继续使用原容器。无需重启 MySQL。观察后续 `huawei-rds-network` 周期采集成功；只读诊断本身不会清除已有采集失败告警，恢复仍按既有连续正常采集次数判断。Grafana 继续使用现有 **云业务与开发 · 问题看板**（UID `cloud-business-dev-v1`）底部网络分区，无需重新导入。

仅回滚这次单位修复时，将备份 ID 换成上面的实际值；配置没有变化，因此保留现有网络配置和数据卷：

```bash
unit_fix_stamp=YYYYMMDD-HHMMSS
docker image inspect "cloud-resource-monitor:before-rds-unit-$unit_fix_stamp" >/dev/null || exit 1
docker image tag "cloud-resource-monitor:before-rds-unit-$unit_fix_stamp" cloud-resource-monitor:1.0.0
docker compose up -d --no-deps --no-build --pull never --force-recreate cloud-monitor
```

当前源码仍含修复，后续构建会再次使用修复后代码。此处回滚只适用于已支持 `network_traffic` 的修复前版本；首次网络接入的回滚见文末。

## 网络监控首次接入

首次接入需要新增一个配置片段、一个主配置引用和一个 Compose 只读挂载，并更新采集服务与通知网关代码。**不覆盖生产密钥、旧任务与渠道开关，不重启 MySQL，不新增业务数据库账号。** 本地只做离线验证，未操作服务器或发送真实通知。

## 1. 确认原部署并备份

服务器沿用原目录 `/data/monitor/cloud-resource-monitor`、原 Compose 项目名和 MySQL 数据卷。如果原命令带 `-p`、`-f`，或使用 `COMPOSE_PROJECT_NAME`，下文所有 Compose 命令都必须保留这些设置。以下镜像命令按项目默认标签 `cloud-resource-monitor:1.0.0` 编写；生产自定义过标签时，换为生产实际标签。

先在服务器终端执行。备份仅包含两个配置文件及旧镜像标签，不建立另一份运行项目；备份含有凭据，限制访问，不要上传或公开。

```bash
cd /data/monitor/cloud-resource-monitor
docker compose ps cloud-monitor notification-gateway mysql
umask 077
stamp=$(date +%Y%m%d-%H%M%S)
backup_dir=".deploy-backups/$stamp"
mkdir -p "$backup_dir"
chmod 700 .deploy-backups "$backup_dir"
cp -p config/config.yaml "$backup_dir/config.yaml"
cp -p docker-compose.yml "$backup_dir/docker-compose.yml"
docker image tag "$(docker inspect --format '{{.Image}}' "$(docker compose ps -q cloud-monitor)")" "cloud-resource-monitor:rollback-monitor-$stamp"
docker image tag "$(docker inspect --format '{{.Image}}' "$(docker compose ps -q notification-gateway)")" "cloud-resource-monitor:rollback-gateway-$stamp"
printf 'Backup ID: %s\n' "$stamp"
```

记录输出的备份 ID。备份目录使用 `0700` 限制访问，文件通过 `cp -p` 保留原所有者和权限，回滚后容器内的非 root 用户才能继续读取；不要将备份文件改为 root 仅自己可读后再用 `cp -p` 恢复。若任一容器或镜像不存在，先核对部署位置与服务名称，不要继续构建。`.env`、通知配置与数据卷全程保留。不要执行 `docker compose down -v`。如果服务器已存在 `config/network-traffic.yaml`，先另行备份该文件并比较已有规则，按差异合并，避免覆盖线上调整。

## 2. 从 Windows 上传文件

在本地 Windows PowerShell 执行，把 `服务器IP` 换成实际 SSH 地址或主机别名。这里只上传源码和新增片段，不上传 `.env`、生产主配置、通知配置或本地 Compose：

```powershell
Set-Location 'C:\Users\77528\Documents\Codex\2026-09-08\new-chat\outputs\cloud-resource-monitor'
$DeployTarget = 'root@服务器IP'
scp -r .\cloud_monitor .\scripts .\tests .\docs .\grafana .\prometheus .\preview "${DeployTarget}:/data/monitor/cloud-resource-monitor/"
scp .\pyproject.toml .\Dockerfile .\README.md .\.dockerignore .\.gitignore .\AGENTS.md "${DeployTarget}:/data/monitor/cloud-resource-monitor/"
scp .\config\network-traffic.yaml "${DeployTarget}:/data/monitor/cloud-resource-monitor/config/network-traffic.yaml"
```

非默认 SSH 端口时，为三条 `scp` 命令增加 `-P 实际端口`。WinSCP / SFTP 上传同一清单，覆盖原目录中的同名源码；不要造成 `cloud-resource-monitor/cloud-resource-monitor` 嵌套，也不要用旧更新包覆盖当前代码。当前更新不需要删除旧源码文件。

## 3. 追加生产配置与权限

在服务器原 `config/config.yaml` 的 `monitoring_files` 列表中追加一项，保留所有原条目，不要重复创建同名键：

```yaml
monitoring_files:
  # 原有条目全部保留，在列表末尾追加：
  - network-traffic.yaml
```

在服务器原 `docker-compose.yml` 的 `services.cloud-monitor.volumes` 中追加一行，保留原端口、网络、环境变量和其它挂载：

```yaml
      - ./config/network-traffic.yaml:/app/config/network-traffic.yaml:ro
```

配置文件不打进镜像，缺少此挂载会找不到片段并导致校验或启动失败。不要把新挂载加到 MySQL 或通知网关下。若使用自定义 Compose 文件，请追加到实际定义采集服务的文件，并备份所有被修改的 Compose 文件。

核对新片段的任务 `account` 与生产主配置 ID 一致。默认使用 `aliyun-hycen`、`aliyun-yuanzhu`、`huawei-hycen`；新任务沿用账号地域，华为云所选地域需有正确 `project_ids`。任务自身启用也不会启用原来关闭的账号。需要限范围时设置 `options.instances`，见 [资源发现](network-traffic.md#资源发现)。

华为云 ECS 自动发现另需在**云端 IAM 策略**补充 `ces:metrics:list`；只把 JSON 上传到服务器不会改变云端授权。[合并策略](policies/huawei-collector-combined.json) 和 [权限说明](huawei-permissions.md) 已更新，保留现有权限，按区域项目授权。阿里云复用现有 ECS/RDS 列表和 CMS 读取权限，详见 [阿里云权限](aliyun-permissions.md)。

P0/P1/P2 初始阈值已包含在新片段，入、出方向分别判断，具体持续时间见 [三档告警](network-traffic.md#三档告警)。通知仍走原路由：默认 P0 短信、P1 钉钉、P2 邮件。**原来关闭的渠道继续关闭，生成相应等级告警也不会自动启用渠道或降级发送。** 不需要修改通知供应商配置。

## 4. 构建并校验

回到服务器原项目目录，逐条执行：

```bash
cd /data/monitor/cloud-resource-monitor
docker compose config --quiet
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose run --rm --no-deps notification-gateway --config /app/config/notifications.yaml check
```

`cloud-monitor` 与 `notification-gateway` 默认共用同一个镜像标签，构建一次即可获得两者的新代码。自定义部署若使用不同标签，需要分别构建对应服务。不要用不带 `--quiet` 的 `docker compose config` 分享排错输出，它可能包含展开后的凭据。

两个 `check` 都应输出 `"valid": true`。`check --live` 名称中的 `live` 仅表示检查实际配置与凭据是否填写，不调用云 API；网关 `check` 也不会发送通知。这些检查不代表数据库连通、云端授权和实际通知送达已经验证。

再执行只读云诊断，以下四个任务适用于 `aliyun-hycen`、`huawei-hycen` 及相应任务已启用的部署：

```bash
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml diagnose --job aliyun-ecs-network --job aliyun-rds-network --job huawei-ecs-network --job huawei-rds-network
```

只列生产中实际启用的任务；关闭账号的任务会直接报错，不要为了诊断开启账号。若 `aliyun-yuanzhu` 本来已启用，再加 `--job aliyun-yuanzhu-ecs-network --job aliyun-yuanzhu-rds-network`。

`diagnose` 会调用真实只读云 API，但不连接业务数据库、不写监控数据库、不评估或发送通知、不启动调度器。预期有资源的任务应返回 `status: ok` 且 `samples` 大于零；若出现 `error`，先解决权限、地域、拓扑或源数据问题。`no_data` 可退出 0，但只代表未发现目标，不证明指标接入成功。华为云 RDS 源指标可能延迟 5-10 分钟。不要使用 `once` 代替诊断，它会写入监控数据并处理告警。

## 5. 上线并观察

从这里开始替换生产容器，采集服务启动后会立即执行已启用任务，并可能通过现有渠道发送真实告警、恢复和积压通知。先更新网关：

```bash
docker compose up -d --no-deps --no-build --pull never --force-recreate notification-gateway
docker compose ps notification-gateway
```

等待网关显示 `healthy`，再更新采集服务：

```bash
docker compose up -d --no-deps --no-build --pull never --force-recreate cloud-monitor
docker compose ps cloud-monitor notification-gateway mysql
docker compose logs --since 10m --tail=150 cloud-monitor notification-gateway
```

网关包含新的中文指标名称和 Kbit/s、Mbit/s、Gbit/s 格式，因此本次也要替换。`--no-deps` 保持 MySQL 和其它服务运行，数据库不需要迁移。仅执行 `restart` 不会换成新镜像。

健康检查有启动等待期，约一分钟后再次查看 `ps`。采集日志应陆续出现新任务 `status=ok`、非零 `samples`；华为云 ECS 五分钟采一次，建议观察至少 10-15 分钟。`healthy` 只表示进程健康，不代表每个任务采集成功。可以单独筛选新任务日志：

```bash
docker compose logs --since 15m cloud-monitor 2>&1 | grep -E 'collection job=(aliyun(-yuanzhu)?|huawei)-(ecs|rds)-network'
```

继续检查任务状态、采集错误和数据新鲜度。持续超限告警从上线后的有效源样本开始计时，不在启动时回放历史告警；华为云云端数据延迟仍会影响通知时间。原有失败告警按连续正常采集次数恢复，一次只读诊断不会改变告警状态。

## 6. 更新 Grafana

网络分区已追加并保存到现有 **云业务与开发 · 问题看板**（UID `cloud-business-dev-v1`）原开发分区之后，当前环境无需重新导入。布局与维护脚本见 [现有看板网络分区](grafana-business-network.md)，保留原有图表和筛选。不要为本次部署恢复已删除的 `cloud-resource-monitor` 历史仪表板。已有 Prometheus 抓取地址不变，无需另建数据源或重启 Prometheus。

网络分区按阿里云 ECS **内网**、华为云 ECS **公网与内网合计**、阿里云 RDS **总网络吞吐**、华为云 RDS **总网络吞吐**排列四行，每行左侧入站、右侧出站，共八图，各图保留独立时间选择器。已有三图或六图可使用 [看板维护脚本的 `--split-rds-providers` 模式](grafana-business-network.md#维护方式) 迁移，本次拆图无需重建或重启服务。华为云 ECS 和 RDS 不能据此还原 ECS 到 RDS 的纯内网通信量。图表从上线后的 Prometheus 抓取开始累积，不回放历史云端数据；无资源或未启用任务显示无数据。

## 回滚

旧镜像不支持 `network_traffic`，不能只换镜像而保留新主配置引用。以下命令恢复备份的主配置与 Compose，并分别恢复网关、采集服务的旧镜像，不删除 MySQL 数据。先将备份 ID 换成第 1 步输出的实际值：

```bash
cd /data/monitor/cloud-resource-monitor
stamp=YYYYMMDD-HHMMSS
backup_dir=".deploy-backups/$stamp"
test -f "$backup_dir/config.yaml" || exit 1
test -f "$backup_dir/docker-compose.yml" || exit 1
docker image inspect "cloud-resource-monitor:rollback-monitor-$stamp" >/dev/null || exit 1
docker image inspect "cloud-resource-monitor:rollback-gateway-$stamp" >/dev/null || exit 1
docker compose stop cloud-monitor
cp -p "$backup_dir/config.yaml" config/config.yaml
cp -p "$backup_dir/docker-compose.yml" docker-compose.yml
docker compose config --quiet
docker image tag "cloud-resource-monitor:rollback-gateway-$stamp" cloud-resource-monitor:1.0.0
docker compose up -d --no-deps --no-build --pull never --force-recreate notification-gateway
docker image tag "cloud-resource-monitor:rollback-monitor-$stamp" cloud-resource-monitor:1.0.0
docker compose up -d --no-deps --no-build --pull never --force-recreate cloud-monitor
docker compose ps cloud-monitor notification-gateway mysql
```

两服务分别保留旧镜像标签，兼容它们更新前实际运行不同版本的情况。自定义 Compose 文件、服务镜像标签也需按原部署恢复。若备份之后另有合法配置调整，先核对差异再合并，避免覆盖后续操作。

新增网络片段和当前源码可以留在原目录；恢复后的主配置不引用该片段。此时运行的是旧镜像，后续重新构建会再次使用目录中的新源码。回滚不自动撤回 Grafana 面板，可按需恢复 Grafana 自带的上一版本；原有面板 ID 保留。

历史修复说明：[短信统计不足](troubleshooting-collection.md#2026-10-19-短信统计不足)、[RDS 云监控单轮 503](troubleshooting-collection.md#2026-10-13-rds-云监控单轮-503)、[RDS 行锁采集失败](troubleshooting-collection.md#2026-10-12-rds-行锁采集失败)。
