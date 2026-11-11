# 阿里云 SDK 在 Python 3.12 下的 SNI 兼容修复

## 原因与修复

项目使用 `aliyun-python-sdk-core==2.16.0`。该 SDK 自带的旧版 urllib3 在同一个 `try` 中导入 `ssl.wrap_socket`、`CERT_NONE`、`HAS_SNI`，并初始化协议常量。Python 3.12 删除了旧的 `ssl.wrap_socket`，导致后面的初始化也被跳过。

Python 和系统本身支持 SNI，但旧组件的 `HAS_SNI` 留在 `False`。它因而没有把域名传给 TLS 连接，并输出 `SNIMissingWarning`。本次短信统计调用仍成功返回空列表，所以诊断状态为 `no_data`；没有统计记录与 SNI 警告是两件独立的事。

项目中的 `cloud_monitor/aliyun_compat.py` 在阿里云客户端发请求前补齐这几个初始化值。修复只在原生 TLS 支持 SNI、旧函数已移除、SDK 处于已确认的错误初始化状态时执行；已经正常初始化的环境和其他 TLS 实现不会被覆盖。初始化受锁保护，可以重复、并发调用。

修复使用原有的 SSLContext 和 HTTPS 校验流程，不屏蔽警告、不关闭证书或域名校验、不修改系统 `ssl` 模块，也不需要切换 Python 版本或新增依赖。

## 项目内实现

兼容处理已经包含在完整项目中，相关实现如下：

| 文件 | 职责 |
| --- | --- |
| `cloud_monitor/aliyun_compat.py` | 兼容处理 |
| `cloud_monitor/providers/aliyun.py` | 采集请求前调用兼容处理 |
| `cloud_monitor/notifiers.py` | 保留的阿里云短信适配器调用同一处理 |

第三方 HTTP 短信、华为云短信、钉钉和邮件不依赖此兼容处理。该处理也不修改配置或数据库，不会重新开启已关闭的华为云证书任务。

项目中的 `tests/test_aliyun_compat.py`、`tests/test_aliyun_tls.py` 和 `tests/test_notifiers.py` 覆盖相关行为。后续维护直接修改项目内实现与测试，服务器使用完整项目部署。

## 部署与复查

按照[项目部署说明](../README.md)同步完整项目，保留服务器已有配置、`.env` 和 MySQL 数据，然后在服务器项目根目录执行：

```sh
docker compose build cloud-monitor
docker compose run --rm --no-deps cloud-monitor --config /app/config/config.yaml diagnose --job aliyun-sms-statistics
docker compose up -d --no-deps --force-recreate cloud-monitor notification-gateway
```

诊断命令不发送通知、不写数据库。查询没有短信记录时，仍可能正常显示 `no_data`，但不再出现 `SNIMissingWarning`。有完整短信统计记录时可以显示 `ok`，以接口真实返回为准。

两个应用服务共用项目镜像，上述命令使它们加载同一版实现。使用阿里云短信适配器的旧版兼容配置也会应用该修复；HTTP 短信不经过阿里云 SDK。重建应用容器不需要删除 MySQL 数据卷。

## 验证

- 使用动态生成的测试 CA 与本机 HTTPS 服务，实际走阿里云 SDK 请求路径，验证服务器收到正确的 SNI 域名。
- 可信且域名匹配的证书允许连接，无 `SNIMissingWarning`。
- 域名不匹配和不受信任 CA 的证书仍被明确拒绝，HTTP 请求不会发送到服务端。
- 兼容处理幂等、支持并发初始化；正常环境和替代 TLS 实现不会被改动。
- 已用真实阿里云短信统计接口做只读复查，返回 `no_data`，没有该 SNI 警告。未发送真实短信、钉钉或邮件。

旧 SDK 的通用 `PROTOCOL_TLS` 在测试中可能另有 `DeprecationWarning`，这属于其原有接口弃用提示，与本次 SNI 初始化错误不同；本修复未全局屏蔽它。
