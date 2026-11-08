# 本机运行与演示

命令从仓库根目录执行，除非该步骤另行切换目录。使用虚构样例即可体验完整业务流。

## 本机安装与初始化

使用 Windows PowerShell、Python 3.14、Node.js 24 和本机 PostgreSQL 18。为项目准备一个专用数据库及拥有迁移权限的账号。运行以下命令前，将连接示例替换为实际数据库，密码中的特殊字符使用 URL 编码。应用直接读取环境变量。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.lock
npm.cmd --prefix frontend ci

$env:DATABASE_URL = 'postgresql+psycopg://private_ops:YOUR_PASSWORD@127.0.0.1:5432/private_ops_dev'
Set-Location backend
..\.venv\Scripts\python.exe -m alembic upgrade head
..\.venv\Scripts\python.exe -m app.manage checkpoints
..\.venv\Scripts\python.exe -m app.manage operator operator
..\.venv\Scripts\python.exe -m app.manage seed
```

创建运营账号时输入至少 12 个字符的密码。重复执行同一个账号的初始化命令会更新密码、撤销已有会话。最后一条命令加载用于本机演示的虚构知识，可重复执行。

## 启动 API、Worker 和后台

在 API 与 Worker 两个窗口中分别设置相同的 `DATABASE_URL`。要演示测试消息，在两个窗口及提交事件的窗口中设置相同的 `DEV_EVENT_TOKEN`。可用 `[guid]::NewGuid().ToString('N')` 生成一次本机演示凭据，再为各窗口设置 `$env:DEV_EVENT_TOKEN`。测试入口默认关闭，只接受回环地址上的已配置测试连接。

在 API 与 Worker 两个窗口启用虚构模型和商家数据：

```powershell
$env:AGENT_MODEL_MODE = 'fixture'
$env:COMMERCE_MODE = 'fixture'
```

API 窗口从项目根目录执行：

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m app.server
```

Worker 窗口再启用通知替身并启动处理进程。

```powershell
$env:NOTIFICATION_MODE = 'fixture'

Set-Location backend
..\.venv\Scripts\python.exe -m app.worker
```

前端窗口从项目根目录运行 `npm.cmd --prefix frontend run dev`，然后打开 [运营工作台](http://127.0.0.1:5173/) 并登录。可以查看会话、引用版本、人工待办和处理状态。各窗口用 Ctrl+C 停止进程。

如需使用官方 DeepSeek，将 Worker 的 `AGENT_MODEL_MODE` 改为 `deepseek`，并用 `$env:DEEPSEEK_API_KEY = Read-Host 'DeepSeek API Key' -MaskInput` 设置密钥。默认模型是 `deepseek-flash`。知识后台的答复验证如需使用真实模型，在 API 进程也设置相同的模型配置。

运营邮件由 Worker 读取 `SMTP_HOST`、`SMTP_PORT`、`SMTP_FROM` 和 `OPERATOR_EMAIL`，可选 `SMTP_USERNAME`、`SMTP_PASSWORD`。邮件走 STARTTLS。未配置时，后台仍登记人工待办并显示通知结果。默认仅在运营工作时段发送提醒。

## 提交一条本机演示消息

在设好 `DEV_EVENT_TOKEN` 的 PowerShell 中提交：

```powershell
$eventId = [guid]::NewGuid().ToString()
$event = @{
  connection_id = 'fixture-channel'
  external_event_id = $eventId
  event_type = 'message.received'
  occurred_at = [DateTimeOffset]::UtcNow.ToString('o')
  conversation = @{ kind = 'group'; external_id = 'demo-group-1' }
  sender_external_id = 'demo-member-a'
  message = @{
    external_id = $eventId
    text = '风行跑步上衣怎么洗？'
    mentions_bot = $true
    reply_to_external_id = $null
  }
}
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/dev/inbound-events' `
  -Headers @{ 'X-Dev-Token' = $env:DEV_EVENT_TOKEN } `
  -ContentType 'application/json; charset=utf-8' -Body ($event | ConvertTo-Json -Depth 5)
```

收到 202 后，Worker 异步处理，后台可刷新查看。用新的事件 ID 发送「鞋收到就开胶了，找人工」，可以演示待办。运营可登记结果，也可明确交回助手回复。测试渠道的发送状态代表替身结果。

## 演示身份核验和商家查询

在 API 和 Worker 两个窗口中设置 `$env:COMMERCE_MODE = 'fixture'` 并重启，保留相同的 `DEV_EVENT_TOKEN`。用上节的事件格式提交「风行跑步上衣黑色 M 有货吗」或「会员日活动有哪些优惠」，在会话详情查看查询依据。

用本机核验入口将上节的发送者关联到虚构会员 A：

```powershell
$binding = @{
  action = 'bind'
  connection_id = 'fixture-channel'
  sender_external_id = 'demo-member-a'
  proof = 'wp02-proof-a'
  private_external_id = 'private-demo-a'
}
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/dev/commerce/bindings' `
  -Headers @{ 'X-Dev-Token' = $env:DEV_EVENT_TOKEN } `
  -ContentType 'application/json; charset=utf-8' -Body ($binding | ConvertTo-Json)
```

用新的事件与消息 ID，把会话改为 `@{ kind = 'private'; external_id = 'private-demo-a' }`，把 `mentions_bot` 改为 `$false`，发送「查我的订单」。群内只给核验或私密查询引导，后台“业务连接”显示核验状态，会话详情只展示脱敏记录。解绑时提交同一地址，保留 `connection_id`、`sender_external_id`，将 `action` 改为 `unbind`，移除 `proof` 和 `private_external_id`。有品牌官方办理地址时，可在 API 和 Worker 配置 `OFFICIAL_SERVICE_URL`。

## 验证和排错

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/live
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/ready
npm.cmd --prefix frontend run build
```

- `live` 表示 API 进程存活，`ready` 同时检查数据库连通和业务迁移版本。Worker 启动另外检查 LangGraph 快照表。
- 集成验收使用单独、名称以 `_test` 结尾的 PostgreSQL 数据库。分别配置 `WP01_TEST_DATABASE_URL` 至 `WP04_TEST_DATABASE_URL`，从根目录执行 `.\.venv\Scripts\python.exe -m pytest backend/tests -q`。使用干净测试库。验收会初始化测试运营账号、加载样例并写入测试事件。默认 API 端口是 8000，也可通过 `PRIVATE_OPS_PORT` 使用独立端口。
- 目前商家查询和消息发送通过显式启用的测试适配器运行。真实渠道协议和商家接口按供应商授权联调。

## 演示会员经营、互动与群复盘

1. 按前面的步骤完成本机核验。用同一个已核验发送者在测试群发「我平时穿 M 码」，再到已核验私密会话问「推荐风行跑步上衣」。在“会员经营”查看证据、更正尺码、删除或预览候选人群。
2. 在“活动管理”新建并发布一场当前开放的活动。测试事件可发送「帮我报名晨跑挑战」「晨跑挑战打卡」。将“晨跑挑战”换成实际活动名。问卷使用已核验的私密会话，格式为「晨跑挑战问卷：跑了 5 公里」。后台查看参与计数及使用的规则版本。
3. 在“群复盘”为测试群启用分析，再发送同主题的公开群消息。选择上海业务日查看报告。明确的未 @ 投诉进入现有人工待办，可从复盘打开原事项或建立知识纠错草稿。
4. 在“知识管理”发布经过确认的新版本，再用原问题和改写问题运行答复验证。会话详情保留答复实际引用的版本。

新增接口和权限见 [WP03 HTTP 契约](specs/WP03_API.md)。

## 演示运营触达

在“运营触达”的“群与模板”登记测试群、启用触达和保存欢迎文案。创建计划、查看目标并明确确认后才会排期。会员可发送「风行跑步上衣黑色 XL 到货提醒我」。通过 [WP04 事件契约](specs/WP04_API.md) 的开发入口提交规范补货、入群、签收和回执事件，在“发送记录”核对处理结果。

服务器访问、发布目录与健康检查见 [部署说明](DEPLOYMENT.md)。
