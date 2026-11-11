# 通知配置

通知已迁移到独立服务，当前使用入口见 [独立通知服务](notification-gateway.md)。配置文件为
`config/notifications/notifications.yaml`，默认 P0 使用短信、P1 使用钉钉、P2 使用邮件，
映射可在该文件的 `routes` 中调整。短信默认使用供应商无关的 HTTP 适配，支持第三方平台。
本页保留各具体适配器的详细配置，其中阿里云与华为云短信适配均为可选项。

下列片段放入独立通知配置文件的 `notifications` 节点。每个渠道可配置 `enabled: false`，
暂不填写其凭据；手动测试接口可在补齐配置后单独调用，属于实际发送。自动化单元测试和离线演示不会发真实消息。

## 钉钉群机器人

```yaml
notifications:
  dingtalk:
    webhook: "" # 机器人完整 HTTPS Webhook，包含 access_token
    secret: ""  # 机器人选择“加签”时回填 SEC 开头的密钥
    keyword: "云资源告警" # 选择关键词校验时，应与机器人配置一致
    timeout_seconds: 15
```

每次请求计算时间戳与 HMAC-SHA256 签名，消息包含关键词、级别、触发或恢复状态、云账号、资源、指标值、采集时间和事件编号。收到 HTTP 成功响应但 `errcode` 非 0 仍视为失败。使用 IP 白名单时，还需放行运行容器所在服务器的出口 IP。

## 邮件

```yaml
notifications:
  email:
    host: "smtp.example.com"
    port: 465
    security: "ssl" # ssl 通常为 465；starttls 通常为 587
    username: ""
    password: "" # 填写 SMTP 授权码或密码，按邮件供应商要求
    from_address: "alerts@example.com"
    to: ["operations@example.com"]
    timeout_seconds: 15
```

邮件使用 UTF-8 编码，并校验服务端 TLS 证书。`username` 与 `password` 必须同时填写或同时省略；支持无需认证但仍采用 TLS 的内部邮件中继，不提供明文 SMTP。任一收件人被 SMTP 服务端拒收都会记录为发送失败；重试可能使已接受邮件的收件人收到重复消息。

## 阿里云短信

```yaml
notifications:
  sms:
    provider: "aliyun"
    access_key_id: ""
    access_key_secret: ""
    region_id: "cn-hangzhou"
    sign_name: "" # 审核通过的短信签名
    template_code: "" # 审核通过的 SMS_ 开头模板编号
    phone_numbers: ["13800000000"]
    template_params:
      resource: "{resource_name}"
      metric: "{metric}"
      value: "{value}"
      state: "{phase}"
    max_param_length: 20
    timeout_seconds: 15
```

使用阿里云官方 Python SDK 调用 `dysmsapi.aliyuncs.com` 的 `SendSms`，API 版本为 `2017-05-25`。短信 RAM 身份需要 `dysms:SendSms` 权限；建议单独创建仅可发送短信的身份。模板变量名必须与已审核模板一致，示例的 `resource`、`metric`、`value`、`state` 不代表云端已存在此模板。国内号码按阿里云短信要求填写，国际号码和模板需开通相应业务。一个配置可以填写多个号码，受供应商单次请求号码数与发送频率限制约束。

可导入的权限文件见 [阿里云短信发送策略](policies/aliyun-sms-sender.json)，授权步骤见 [阿里云权限配置](aliyun-permissions.md)。该策略应授权给 `notifications.sms` 所用密钥对应的 RAM 用户；仅负责采集其他账号数据的用户无需授予短信发送权限。

## 华为云短信

华为云消息&短信发送接口使用应用的 `app_key`、`app_secret`，与监控接口的 IAM AK/SK 不同。从华为云消息&短信控制台获取应用接入地址、APP Key、APP Secret、签名通道号和已审核模板编号。

```yaml
notifications:
  sms:
    provider: "huawei"
    api_endpoint: "" # 完整短信发送 URL，通常为控制台接入地址 + /sms/batchSendSms/v1
    app_key: ""
    app_secret: ""
    sender: "" # 国内短信填写签名通道号
    signature: "" # 按模板要求填写签名；不需要时留空
    template_id: ""
    phone_numbers: ["+8613800000000"]
    template_params: # 华为云模板变量为有序数组，顺序必须与审核模板一致
      - "{resource_name}"
      - "{metric}"
      - "{value}"
      - "{phase}"
    max_param_length: 20
    timeout_seconds: 15
```

请求使用 WSSE 应用鉴权与 HTTPS。接口总体返回码、逐号码返回码均需为 `000000` 才判定接受成功。任何号码发送失败都会进入上层失败重试，因此批量请求部分成功时，已成功号码可能收到重复通知。

WSSE 签名根据华为云[短信 X-WSSE 鉴权官方示例](https://support.huaweicloud.com/devg-msgsms/sms_04_0002.html)实现：对 `Nonce + Created + AppSecret` 计算 SHA-256，再对原始摘要字节进行 Base64 编码。

## 模板、失败与密钥

短信模板支持以下事件字段：`{id}`、`{phase}`、`{severity}`、`{provider}`、`{account}`、`{resource_id}`、`{resource_name}`、`{metric}`、`{value}`、`{unit}`、`{summary}`、`{observed_at}`。`phase` 为 `firing` 或 `recovery`。模板只进行字段替换，不执行表达式；属性访问、下标、格式说明和未知字段会在配置校验时拒绝。

`max_param_length` 按字符数截断每个模板参数，默认 64，允许 1 至 1000。实际限制以所选短信签名、模板、地区和供应商规则为准，示例 20 需要结合审核模板调整。短信宜使用短资源名与数值，完整事件信息保留在本地记录。若恢复也发短信，审核模板应能表达恢复状态，例如明确包含 `phase` 对应变量。

所有网络和 SMTP 操作具有可配置超时，范围为大于 0 且不超过 300 秒；错误不会回显 Webhook、签名、密钥、SMTP 密码或供应商原始响应。通知模块一次调用仅发一次，失败重试和去重由服务的持久化通知队列负责。云短信、钉钉、邮件服务接受请求不等于终端最终送达；供应商后续回执与短信状态回调未在本项目中接入。遇到响应超时，供应商可能已接收，重试存在重复通知的可能。

不要将已回填凭据的配置提交版本库或放入镜像。部署时通过只读配置挂载或配置支持的环境变量注入；限制宿主机配置文件的读取权限。短信会计费，正式启用前核实签名、模板、联系人与余额。
