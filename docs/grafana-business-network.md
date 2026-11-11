# 现有云业务看板的网络速率分区

2026-10-27 本次替换目标为现有 **云业务与开发 · 运行明细**，UID `cloud-business-runtime-v1`，项目中的当前 JSON 为 [cloud-business-runtime-v1.json](../grafana/cloud-business-runtime-v1.json)。原华为 ECS/RDS 四图已替换、保存并重新加载，不新增并行总量图；其他图、变量、导航、面板 ID、独立时间选择和布局保留。原服务器采集服务与网关也已替换且健康；四图实际 Prometheus 查询分别返回 33/33/3/3 条新鲜实例序列，核验结果及回滚见 [部署记录](server-update.md#2026-10-27-华为内网估算直接替换)。

[运行明细](http://192.168.120.28:3000/d/cloud-business-runtime-v1)

| 图表 | 面板 ID / 布局 | 数据口径 |
| --- | --- | --- |
| 阿里云 ECS 内网入站速率 | 6 / 第一行左侧 | `provider=aliyun, product=ecs, scope=intranet, metric=network_in_bps` |
| 阿里云 ECS 内网出站速率 | 7 / 第一行右侧 | `provider=aliyun, product=ecs, scope=intranet, metric=network_out_bps` |
| 华为云 ECS 内网入站速率（估算） | 8 / 第二行左侧 | `provider=huawei, product=ecs, scope=intranet_estimated, metric=network_in_bps` |
| 华为云 ECS 内网出站速率（估算） | 9 / 第二行右侧 | `provider=huawei, product=ecs, scope=intranet_estimated, metric=network_out_bps` |
| 阿里云 RDS 总网络入站吞吐量 | 10 / 第三行左侧 | `provider=aliyun, product=rds, scope=all, metric=network_in_bps` |
| 阿里云 RDS 总网络出站吞吐量 | 11 / 第三行右侧 | `provider=aliyun, product=rds, scope=all, metric=network_out_bps` |
| 华为云 RDS 内网入站吞吐量（估算） | 12 / 第四行左侧 | `provider=huawei, product=rds, scope=intranet_estimated, metric=network_in_bps` |
| 华为云 RDS 内网出站吞吐量（估算） | 13 / 第四行右侧 | `provider=huawei, product=rds, scope=intranet_estimated, metric=network_out_bps` |

使用现有 Prometheus 数据源，以及 `scrape_job`、`instance`、`account` 变量。
八个图表分别提供独立时间选择器，沿用同页现有趋势图的控件：5 分钟至 90 天快捷范围、自定义起止时间和「应用」。
初始范围为近 6 小时；每张图分别按面板 ID 保存会话选择，切换一张图不改变其他七张或页面顶部时间范围。
起止时间按 Asia/Shanghai 展示，结束时间必须晚于开始时间；刷新与切换账号时保留各图自己的时间选择。
只查询所选范围，超出 Prometheus 实际保留期的部分没有历史数据。
入站和出站分别查询 `network_in_bps`、`network_out_bps`，源单位 `bit/s`，运行明细保留已有 MB/s 展示：`MB/s = bit/s ÷ 8,000,000`，不调用 `rate()`，不相加。差分在采集器完成，图表只选择估算序列。
只显示有效样本和健康采集服务的数据，缺失数据不补零。后续新增账号尚未采集或没有有效样本时显示无数据。
图中的 P2/P1/P0 参考线数值不变，ECS 为 100/300/800 Mbps、RDS 为 50/100/200 Mbps，换算到 MB/s 分别是 12.5/37.5/100 和 6.25/12.5/25。对应华为业务规则直接替换为内网估算范围，原数值阈值和持续条件不变；通知显示“内网…（估算）”。
采样、延迟、支持范围和持续告警条件见 [网络监控说明](network-traffic.md)。

华为四图要求总量及全部关联 EIP 使用同一已闭合的 300 秒平均窗口、完全相同的源时间戳；缺失、过期、关联不全或负差时不展示，不补零或截断负数。估算任务非 `ok` 时也隐藏旧当前值。NAT/负载均衡等不经过已关联 EIP 的公网路径仍可能包含在差值中，因此标题保留“估算”。阿里云 ECS 是原生内网，阿里云 RDS 尚无核准的内外网分项，保留明确的总量标题，详见 [阿里云 RDS 核对与权限](aliyun-rds-network-scope-audit.md)。

华为云 ECS 名称由采集器在 CES 发现后通过 ECS 详情列表补齐，图例沿用“名称（ID）”。名称查询依赖 `ecs:cloudServers:list` 和新版采集镜像，见 [ECS 名称修复](server-update.md#华为云-ecs-实例名称修复)；无需再次改看板。旧历史样本仍携带 UUID，查看跨更新时段的趋势可能出现旧、新两套图例；新数据取控制台名称。

2026-10-23 起，可通过采集配置 `config/network-traffic.yaml` 中各 ECS 任务的 `options.instances` 统一限定采集、网络展示与网络告警。2026-10-26 当前项目的华为 ECS 任务已移除空清单，恢复按账号/任务指定地域自动发现；两个阿里云 ECS 任务保留各自清单，本地空清单仍需填写 ID、地域和可选名称后按 [只采集指定 ECS](network-traffic.md#只采集指定-ecs) 应用。入站与出站使用相同范围。移出清单后不再导出该实例的当前网络样本，所选时间范围包含配置生效前时仍会显示已有历史曲线。华为恢复采集后图表从新样本继续累积，停采期间不自动补历史；无需调整此看板、RDS 图表或独立时间控件。

## 运行明细四图替换

从运行明细「编辑 → 代码」导出最新 Grafana v2 JSON，在当前项目内生成更新结果：

```powershell
python scripts/update_huawei_runtime_intranet.py --input preview/validation/huawei-intranet-runtime-before.json --output grafana/cloud-business-runtime-v1.json
```

输入备份为此次修改前导出；后续维护先刷新该备份，避免用旧导出覆盖用户的新编辑。脚本严格校验 UID 和 8/9/12/13 四个面板，更新标题、说明、隐藏 PromQL、独立时间目标及内嵌 `getOption` 配置，重复运行不新增图表。将结果应用回同一运行明细看板并保存；不使用下面历史问题看板的追加或拆分命令。采集器、规则和网关需同步更新才能得到新数据，步骤见 [服务器更新](server-update.md)。

应用采集配置时，旧总量活动/候选状态和待发/失败消息会清理，不发迁移恢复通知；已发送记录和历史样本保留。新图只选 `intranet_estimated`，不会把保留的旧总量历史改名展示为内网。

2026-10-27 已完成运行明细四图保存/重新加载，实际四图查询分别返回 33/33/3/3 条新鲜序列。问题看板 `cloud-business-dev-v1` 的面板 2 主 ECharts 告警分页映射也已保存，退出编辑并刷新后确认分页、搜索、表格和“内网…（估算）”文案保留。现场截图：[华为 ECS](../preview/grafana-huawei-intranet-ecs.png)、[华为 RDS](../preview/grafana-huawei-intranet-rds.png)、[告警明细](../preview/grafana-huawei-intranet-alert.png)。

## 历史问题看板维护

下列 `scripts/append_business_network_panels.py` 命令仅维护原 **云业务与开发 · 问题看板**（UID `cloud-business-dev-v1`）的历史布局，其华为源仍为总量。它们用于历史八图追加、拆分和时间控件维护，不用于本次运行明细替换，也不会自动应用到 Grafana。

`scripts/append_business_network_panels.py` 只处理此 UID 的 Grafana v2 JSON。
先从现有看板「编辑 → 代码」导出 JSON。尚未添加网络分区时，生成包含八个独立方向图表的分区：

```powershell
python scripts/append_business_network_panels.py --input grafana/cloud-business-dev-original.json --output grafana/cloud-business-dev-updated.json
```

将结果应用到同一看板并保存。追加模式保留所有旧配置，并拒绝错误 UID、重复分区和覆盖输入文件。
不要将旧的 `grafana/cloud-resource-monitor.json` 导入到此 UID；它属于另一个历史看板。

对已存在的六个方向图，使用以下模式将两云合并的 RDS 图拆为按云厂商区分的四图，网络分区变为八图：

```powershell
python scripts/append_business_network_panels.py --split-rds-providers --input grafana/cloud-business-dev-original.json --output grafana/cloud-business-dev-updated.json
```

该模式也可直接将旧的三个入出站合并图迁移为八图，无需先执行方向拆分。
原 RDS 入站、出站面板 ID 保留为阿里云图，为华为云图分配新 ID，布局调整为四行两列。
每张图仅查询对应云厂商的一个方向，独立时间控件中的实际查询同步拆分；八张图的会话时间选择按各自 ID 保存。
重复运行 `--split-rds-providers` 不会增加重复图表；其他面板、变量和导航保留原配置。
该历史拆分模式仅更新原问题看板，无需为拆分操作重建 Docker 镜像、重启采集服务或修改告警阈值、通知配置；不适用于本次内网估算采集替换。

兼容原有三图仅按入出站方向拆为六图的模式：

```powershell
python scripts/append_business_network_panels.py --split-directions --input grafana/cloud-business-dev-original.json --output grafana/cloud-business-dev-updated.json
```

该模式保留原三个网络面板的 ID 作为入站图，为出站图分配新 ID，布局为三行两列，RDS 仍合并两云。
重复运行 `--split-directions` 不会增加重复图表，已有六图保持六图，已有八图保持八图。

仅更新已有网络图的独立时间控件时，使用：

```powershell
python scripts/append_business_network_panels.py --independent-time --input grafana/cloud-business-dev-original.json --output grafana/cloud-business-dev-updated.json
```

该模式兼容三个合并图、六个方向图或八个厂商方向图，复用现有「网络带宽使用率趋势」的 Business Charts 独立时间实现，
保留各图的网络查询、面板 ID 和三档阈值，将图表高度调整为与同页已有趋势图一致；不会执行方向或云厂商拆分。
