# 资源明细看板排查与更新

本次修正以下四张现有 Grafana 看板的数据查询和展示。更新应用到原 UID，不新建看板。

| 看板 | UID | 项目内维护文件 |
| --- | --- | --- |
| 容器资源明细 | `detail-9323b204dbc111` | `grafana/detail-9323b204dbc111.json` |
| Kafka 消息分区比较 | `detail-431e149b73b9b7` | `grafana/detail-431e149b73b9b7.json` |
| Kafka 消息处理明细 | `detail-852bebbd38276b` | `grafana/detail-852bebbd38276b.json` |
| 服务发现与配置中心 | `detail-7772a5bacd7ad7` | `grafana/detail-7772a5bacd7ad7.json` |

四张看板继续使用原数据源、账号和实例筛选、布局及各图独立时间控件。云业务看板的八张网络图和五列告警明细不在本次修改范围。

## 空白的原因与修正

### 容器采集服务版本

`cadvisor_version_info` 的版本、修订号、内核和操作系统位于标签中，数值本身通常为 `1`。原面板将整张表归约为均值，又隐藏了均值列，导致只剩字段名称，实际版本文本没有按实例展示。

修正后保留即时表格查询，移除均值归约，按实例展示账号、实例名称、cAdvisor 版本、修订号、内核版本及操作系统。时间、数值和内部辅助标签不作为版本信息展示，原账号与主机名称映射继续使用。

### Kafka 进度变化

写入和处理进度都取自 offset。原查询在 `$__rate_interval` 窗口内使用 `deriv(...[$__rate_interval:])`；省略子查询采样间隔时，间隔取决于 Prometheus 的规则计算配置。窗口内不足两个有效点时，`deriv` 不返回结果，即使 offset 和积压指标都有数据，进度图也可能空白。

两张 Kafka 看板的每秒写入、每分钟写入和每分钟处理进度查询，统一显式使用 `[$__rate_interval:15s]`。继续按分区去重，再计算斜率、汇总；每分钟图乘以 `60`。offset 回退仍显示负值，不按累计计数器重置处理，也不以零填补缺口。

显式的 15 秒子查询间隔增加了长时间范围下的查询计算量。排查时先查看最近 1 小时或 6 小时；查看较长历史时，需结合当前 Prometheus/Thanos 的实际保留期和查询性能判断。超出保留期、目标中断采集或窗口内仍不足两个有效样本时，仍可能没有数据。

### Nacos 耗时与异常次数

不同空白状态采用不同含义，不能统一补零：

| 项目 | 数据含义 | 展示原则 |
| --- | --- | --- |
| 通知平均耗时 | `nacos_timer_seconds_sum/count`，仅统计 `name="notifyRt"` | 当前窗口无有效事件时显示“当前窗口无通知样本”；缺少指标或不足以计算速率时显示“指标缺失或采样不足” |
| 平均推送耗时 | `nacos_monitor{name="avgPushCost"}` | 返回无效的 `-1` 时显示“暂无有效推送耗时”；缺少指标时显示“指标缺失或采样不足”；有效的 `0 ms` 保留 |
| 各类异常次数 | 对应 `nacos_exception_total{name="..."}` 的速率 | 已暴露且没有新增异常可显示 `0`；缺少对应时序显示指标缺失状态 |

原通知等待查询合并了多个不同计时器，现只统计 `notifyRt`，并将标题改为“通知平均耗时”，避免将配置读写、转储等耗时混入通知指标。计时器当前窗口计数未增长时，平均耗时无定义，不能显示成 `0 ms`。推送指标的标题同步改为“平均推送耗时”，避免将上报的处理成本误读为排队等待。

`avgPushCost` 曾返回 `-1`，它不是有效耗时。看板过滤负数，不将 `-1 ms` 当作性能结果，也不换算成零。无有效值本身不能证明当前没有推送，仍需结合 Nacos 版本及原始指标判断。

异常指标按 `name` 区分。某些名称没有时序，可能与版本、指标注册和采集情况有关，单凭看板无法确定原因；“指标未暴露/未采集”不等于“异常次数为零”。数据库异常或推送失败等已有有效零值的图继续显示真实零值。各图的独立时间区域沿用原控件，空结果时展示状态文字，避免只有空白图框。

推送失败和七类异常图采用简短中文图例，避免把整段 PromQL 显示在图例及悬浮提示中；数值查询保持原样。

## 维护与应用

`scripts/update_detail_dashboard_data.py` 仅处理上述四个 UID 的 Grafana v2 资源 JSON。重复处理已修正文件不应生成新面板或重复查询。修改前从每张原看板的「编辑 → 代码」导出当前资源，保存在项目内 `grafana/backups/<UID>.before.json`，再生成相应维护文件。以容器看板为例：

```powershell
python scripts/update_detail_dashboard_data.py grafana/backups/detail-9323b204dbc111.before.json grafana/detail-9323b204dbc111.json
```

其余三张看板使用各自 UID 的备份和输出路径，分别执行。备份与输出文件必须分开，避免覆盖现场配置；以后若原看板已有其他编辑，应重新导出最新 JSON 后生成，避免旧快照覆盖新增配置。

在 Grafana 打开对应原看板，进入编辑模式并选择「代码」，粘贴生成的完整 v2 JSON，点击 `Apply changes`，再点击「保存」。确认 UID 与表中一致，不通过“新建仪表板”导入。四张 JSON 各自应用到对应看板，不能混用。

保存后重新打开看板验证：容器版本表每个实例一行；两张 Kafka 看板在短时间和常用时间范围内有真实进度曲线；Nacos 无通知样本、无有效推送耗时和异常指标缺失时显示相应状态，已有零值不消失。独立时间范围、筛选器和其他面板应继续正常工作。若需要回滚，将对应 `before.json` 通过同一原看板的代码编辑器应用并保存。

本次只修改 Grafana 查询和展示，不增加采集任务、不调用新的云 API、不修改告警与通知配置。无需重建 Docker 镜像、重启采集器、重启 MySQL 或增加云权限。项目内 JSON 更新只表示本地维护文件已生成，生产是否生效以 Grafana 原看板已保存且重新打开后的结果为准。

## 验证记录

2026-10-20 已在现有 Grafana 的四个原 UID 保存并重新打开检查：容器表显示三台实例的完整版本文本，两张 Kafka 看板显示真实写入和处理进度；额外检查了 1 小时与 24 小时范围，并恢复为 6 小时。Nacos 通知与推送耗时分别显示无样本和无有效值状态，缺失异常指标显示缺失说明，已有推送失败零值继续绘制。

离线验证覆盖迁移范围、幂等、无关配置保留，以及真实 PromQL 求值。PromQL 使用本地已有 Prometheus 镜像，在禁止联网的临时容器内运行，不启动调度、通知或业务数据库：

```powershell
$env:RUN_PROMTOOL_TESTS = '1'
python -m pytest tests/test_detail_dashboard_data.py tests/test_detail_promql.py -q
python -m ruff check scripts/update_detail_dashboard_data.py tests/test_detail_dashboard_data.py tests/test_detail_promql.py
```

现场截图保存在项目 `preview/grafana-container-version-fixed.png`、`preview/grafana-kafka-progress-fixed.png` 和 `preview/grafana-nacos-missing-state-fixed.png`。
