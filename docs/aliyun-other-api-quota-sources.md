# 阿里云其他采集 API 配额核验说明

核验日期：2026-09-08。本文补充说明 ECS、RDS、域名、证书、费用中心和短信统计 API 的限制；云监控 `DescribeMetricList` 的月度免费额度及调用量测算请参见项目中的月度采集评估文档。

## 结论与适用范围

不能把所有阿里云产品的查询 API 加在一起，统一套用云监控的月度免费调用额度。月度免费额度、超额计费、每秒或每分钟限流，是不同的规则。

本次核查中，证书服务公开了本项目所用接口的每秒限制；ECS 公开说明限制按账号、地域和具体接口分别计算，具体阈值应从账号的 API 速率配额页面查询。其他已阅读接口文档没有给出可直接用于本项目测算的月度调用总量上限；这不等于认定这些接口没有限制，也不等于承诺所有查询永久免费。

目前访问密钥待回填，未读取实际账号的已用额度、定制配额或其他程序产生的调用量。以下结论仅针对本项目的查询方式及已核实的公开文档。

## 已核实与待核实项目

| 产品 / 采集操作 | 官方文档中可核实的限制 | 本项目的判断 |
| --- | --- | --- |
| ECS `DescribeInstances` | ECS 所有 API 都有访问流控；按同一账号、地域、具体接口分别计算。具体数值从 API 速率配额页查询；目前仅支持查询部分接口的阈值 | 每日巡检的月度总量较低，但分页集中执行仍需考虑瞬时速率。不能以“每天一次”直接承诺不会限流 |
| RDS `DescribeDBInstances` | 已阅读的接口文档未给出可用于本次测算的明确月度总量或固定每秒阈值 | 按实际分页数计算调用量；上线前核对账号配额和限流返回 |
| 域名 `QueryDomainList` | 本次未核实到该接口的明确月度调用总量和具体速率数值 | 保留每日查询；不能套用云监控免费池，也不能声称无限制 |
| 证书 `ListUserCertificateOrder` | 官方接口说明：单用户 QPS 为 10 次/秒 | 本项目每轮分别列举有效证书和已过期证书，分页也计为独立请求；应控制这些请求的瞬时速率 |
| 证书 `GetUserCertificateDetail` | 官方接口说明：单用户 QPS 为 100 次/秒 | 每轮对需检查的每张证书查询一次详情；证书数影响总量和批次耗时 |
| 费用中心 `QueryResourcePackageInstances` | 已阅读的接口文档未明确公布月度查询总量；每页默认 20、最大 300 条属于分页参数限制 | 每个资源包产品、每轮、每页均是一次查询；不能把页面大小当成速率配额 |
| 费用中心 `QueryAvailableInstances` | 已阅读的接口文档未明确公布月度查询总量；每页默认 20、最大 300 条 | 负载均衡有效期按分页计算；与云监控带宽指标查询分开统计 |
| 费用中心 `QueryAccountBalance` | 已阅读的接口文档未明确公布月度查询总量 | 每个账号每小时一次，即 31 天约 744 次，不按资源数量重复查询 |
| 短信 `QuerySendStatistics` | 已阅读的接口文档未明确公布月度查询总量；每页 1～50 条属于分页参数限制 | 每小时查询日发送量统计；分页独立计数。它不是发送短信的接口 |

“未明确公布”只描述本文实际阅读的页面，不排除其他官方文件、账号设置或服务端存在补充规则。RAM 权限、产品开通状态和频率限制也可能影响调用能否成功。

## 计算和使用时要保留的余量

1. 31 天内，每 24 小时执行约 31 轮，每 12 小时约 62 轮，每小时约 744 轮。每轮调用次数取决于账号、地域、分页和具体资源数量，不能统一算成一次。
2. ECS 官方说明，同账号的控制台操作、RAM 用户、自建平台、Terraform、Ansible 等都包含在同一接口相关的流控范围内。本服务应为其他调用方留出余量。
3. 每月累计调用不多，仍可能因一次巡检中的连续分页或证书详情查询而触发每秒限制。限流应按实际响应处理；若扩大资源规模，应检查请求间隔及退避策略。
4. SDK 重试、服务重启后立即执行、人工触发巡检、多副本重复运行，可能额外增加请求。本文列出的轮数是正常连续运行的基准值。
5. P0 告警发送短信属于短信发送业务，需要另计短信费用及发送限额，不能把它视为数据采集的免费调用。本地接收短信回执，以及读取本地回执数据计算成功率，不是主动调用阿里云查询 API。

## 官方来源

- [ECS API 流量控制](https://help.aliyun.com/zh/ecs/developer-reference/api-throttling)：说明账号、地域、接口维度，以及控制台和其他调用方共享流量的范围。
- [ECS DescribeInstances](https://api.aliyun.com/api/Ecs/2014-05-26/DescribeInstances)：本项目查询 ECS 实例有效期的操作。
- [RDS DescribeDBInstances](https://help.aliyun.com/zh/rds/developer-reference/api-rds-2014-08-15-describedbinstances)：本项目查询 MySQL / PostgreSQL 实例的操作。
- [域名 QueryDomainList](https://api.aliyun.com/api/Domain/2018-01-29/QueryDomainList)：域名查询接口入口，具体速率在本次核验中未确认。
- [证书 ListUserCertificateOrder](https://help.aliyun.com/zh/ssl-certificate/developer-reference/api-cas-2020-04-07-listusercertificateorder)：接口说明明确为单用户 10 次/秒。
- [证书 GetUserCertificateDetail](https://help.aliyun.com/zh/ssl-certificate/developer-reference/api-cas-2020-04-07-getusercertificatedetail)：接口说明明确为单用户 100 次/秒。
- [费用中心 QueryResourcePackageInstances](https://help.aliyun.com/zh/user-center/developer-reference/api-bssopenapi-2017-12-14-queryresourcepackageinstances)：资源包查询和分页定义。
- [费用中心 QueryAvailableInstances](https://help.aliyun.com/zh/user-center/developer-reference/api-bssopenapi-2017-12-14-queryavailableinstances)：实例查询和分页定义。
- [费用中心 QueryAccountBalance](https://help.aliyun.com/zh/user-center/developer-reference/api-bssopenapi-2017-12-14-queryaccountbalance)：余额查询。
- [短信 QuerySendStatistics](https://help.aliyun.com/zh/sms/developer-reference/api-dysmsapi-2017-05-25-querysendstatistics)：日发送统计查询和分页定义。

官方文档及账号配额可能调整，上线时应以所使用站点、实际账号和当时文档为准。
