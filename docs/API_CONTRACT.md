# 共享接口契约

| 项目 | 内容 |
| --- | --- |
| 版本 | v1.0 · 2026-11-07 |
| 依据 | [产品基线](PRODUCT_BASELINE.md)与[技术基线](TECHNICAL_BASELINE.md) |
| HTTP 契约 | [WP01 OpenAPI 3.1](openapi.wp01.json)、[WP02 OpenAPI 3.1](openapi.wp02.json)、[WP03 HTTP 契约](specs/WP03_API.md)、[WP04 HTTP 与事件契约](specs/WP04_API.md) |
| 状态 | WP01–WP04 HTTP 契约已实现。真实供应商协议按授权联调 |

## 1. HTTP 共享约定

| 项目 | 约定 |
| --- | --- |
| 地址与格式 | 后台 API 为 /api/v1，JSON 和 UTF-8。成功响应直接返回资源或列表对象，错误使用统一错误对象 |
| 命名 | JSON 字段用 snake_case，内部资源标识用 UUID。外部标识用不透明字符串，仅在所属渠道或业务连接下有意义 |
| 时间与金额 | 时间为带时区 RFC 3339，服务端输出 UTC。业务日默认按 Asia/Shanghai。货币用整数最小单位及币种 |
| 分页 | limit 默认 20，范围 1–100。列表按创建或接收时间及 ID 稳定倒序。返回 items 和可空 next_cursor。游标不透明并绑定筛选条件 |
| 请求关联 | 服务端返回 X-Request-ID，错误体同时返回 request_id。请求关联标识不承担幂等语义 |
| 认证 | 后台用服务端会话 Cookie，同源使用。HttpOnly、SameSite，HTTPS 环境使用 Secure。登录检查 Origin。会话写操作校验 X-CSRF-Token。登录响应和会话查询返回 CSRF token，登出清除会话 |
| 写入并发 | 有状态变更的资源返回整数 version。客户端提交 expected_version，冲突返回 409。GET 列表和详情不产生副作用 |
| 幂等键 | 改业务状态的 POST 要求 Idempotency-Key，长度 8–128。按运营身份、路由动作及键去重。同键同请求返回已提交结果，同键不同请求返回 409。重放前仍须验证授权。已有成功结果先于资源版本校验返回 |
| 验证 | 缺失或格式错误的必要请求头返回 400，字段或格式校验失败返回 422。未知资源 404，禁止访问时按资源可见性返回 403 或 404 |
| 变更 | 可选字段的兼容扩展保留 v1，改变已有字段含义、状态语义或必要输入时审查调用方并版本化 |

错误体示例：

```json
{
  "code": "VERSION_CONFLICT",
  "message": "事项已更新，请刷新后重试",
  "request_id": "req-01",
  "details": { "current_version": 3 }
}
```

400 用 BAD_REQUEST；401 用 UNAUTHENTICATED；403 用 FORBIDDEN；404 用 NOT_FOUND；409 用 VERSION_CONFLICT、IDEMPOTENCY_CONFLICT、EVENT_CONFLICT 或 INVALID_STATE；422 用 VALIDATION_ERROR；429 用 RATE_LIMITED；503 用 DEPENDENCY_UNAVAILABLE。未预期错误使用 500 INTERNAL_ERROR。业务响应不得泄露其他会员资源是否存在。错误细节仅包含当前调用方可见的信息。

## 2. WP01 范围与端点

WP01 覆盖 F01、F03、F14、F17 的基础路径。验收以测试适配器提交消息开始，经过分流、有效品牌资料或转人工，再到运营后台查看事项。使用虚构知识种子数据。完整的知识编辑、真实订单、真实微信群、画像和活动接口按后续工作包补充。运营唯一，无人工认领动作。

| 方法与路径 | 请求与返回摘要 |
| --- | --- |
| POST /api/v1/auth/login | 账号及密码，建立会话，200 返回运营身份和 CSRF token |
| GET /api/v1/auth/session | 读取当前会话，200 返回运营身份和 CSRF token |
| POST /api/v1/auth/logout | 验证会话和 CSRF，撤销会话，204 |
| POST /api/v1/dev/inbound-events | 仅本机开发和测试配置开启。独立测试凭据，提交规范化消息，入库和任务事务提交后返回 202 |
| GET /api/v1/matters | 按群、状态分页读取事项摘要 |
| GET /api/v1/matters/{matter_id} | 读取事项、消息、意图、知识引用、处理状态与可核对的执行记录 |
| GET /api/v1/handoffs | 分页读取待跟进或已处理事项 |
| POST /api/v1/handoffs/{handoff_id}/resolve | 校验会话、CSRF、幂等键和版本，保存处理结果。可明确交回助手续办 |
| GET /api/v1/health/live | 本机进程存活检查，无业务或配置详情 |
| GET /api/v1/health/ready | 关键持久依赖可用时返回 200，否则 503。模型及真实商家能力属于可降级能力，另由内部状态显示 |

开发入口的规范事件示例：

```json
{
  "connection_id": "fixture-channel",
  "external_event_id": "message-001",
  "event_type": "message.received",
  "occurred_at": "2026-11-07T02:00:00Z",
  "conversation": { "kind": "group", "external_id": "demo-group-1" },
  "sender_external_id": "member-a",
  "message": {
    "external_id": "message-001",
    "text": "这款怎么洗？",
    "mentions_bot": true,
    "reply_to_external_id": null
  }
}
```

202 仅表示事件已持久接收并安排处理，返回 event_id 和 duplicate。同一连接的相同外部事件键及相同规范化内容复用 event_id，duplicate 为 true。键相同且内容不一致返回 409。来源无法核实时拒绝。真实供应商适配器将验签、事件归属和可靠的 @/引用信息映射到此边界。测试入口仅接收允许的测试连接，启用时监听回环，使用独立 X-Dev-Token，默认关闭，非开发配置返回 404。它不提供生产供应商协议，也不依赖模拟群页面。

转人工结束请求：

```json
{
  "expected_version": 2,
  "resolution": "已联系会员并提供换货办理入口",
  "resume_agent": false
}
```

resume_agent 默认为 false。为 true 时，运营提供助手下一步所需的确认结果，应用确认事项可以交回后原子提交待办结果和续办任务。已经处理的待办以相同幂等请求重放时返回原结果。不同动作再次提交按资源状态返回冲突。

## 3. 应用内部与外部业务端口

端口是应用与供应商之间的业务接口。以下名称表达语义，具体供应商请求字段、签名和回调 ACK 按接入工作包定义。端口执行前由应用完成调用权限校验，返回结果再按场景裁剪。

| 端口 | 输入约定 | 返回及降级 |
| --- | --- | --- |
| 渠道事件入口 | 可信连接、来源事件键、发生时间、会话、发送者、消息与引用或业务事件数据 | 规范事件或校验失败。保留入口的外部标识映射 |
| 渠道发送 | 稳定 operation_key、目标连接和会话、原消息引用、已审定内容及可见范围 | accepted、sent、delivered、failed 或 unknown，附供应商引用与证据时间。能力不支持返回 unavailable |
| 渠道状态核实 | 原 operation_key 和已知供应商引用 | 提供可证实的原发送状态。无法核实返回 unknown |
| 会员身份核验 | 渠道及外部身份、商家认可的验证凭据或已建立绑定 | verified、unbound、forbidden、unavailable 或 temporary_failure，verified 附受信会员 ID |
| 订单与物流 | 已验证会员 ID、订单引用或本人订单筛选 | 返回允许展示的状态摘要及来源时间，或 not_found、forbidden、unavailable、temporary_failure |
| 商品、库存与价格 | 商品及规格引用、所需字段 | 返回当前业务数据、结果时间及来源。无匹配、能力缺失和暂时错误分别返回 |
| 活动优惠与权益 | 活动、商品、规则请求，个人权益查询附已验证会员 | 返回当前规则、资格或优惠及时间。个人资格核验失败时仅返回结果类别 |
| 负责人通知 | 稳定 operation_key、已配置的负责人、事项摘要及可用链接 | 返回实际通知结果、供应商引用或失败类别。后台待办独立提交 |

读取成功返回明确数据与来源。查询结果为空、未配置、权限不足与暂时失败不得合并成空对象。发送 accepted 只代表供应商受理。没有成功证据的异常映射为 unknown。可重试与否由产品规则、失败类型和供应商幂等能力共同决定。

后续业务事件沿用连接、外部事件键、发生时间和归属。入退群需要群与成员标识；补货需要可确认的商品和规格及可用库存依据；签收需要订单及可信状态；活动结果需要活动、参与者和实际状态。实时事件与历史导入分别标识。各事件的具体模式由对应工作包补入契约。

## 4. 意图节点契约

- **输入**：当前规范消息、经授权的公共引用、同会员候选事项摘要、允许的意图集合、提示词版本。身份和资源权限由应用提供。
- **输出**：按顺序排列的 intents，每个意图包含 kind、evidence、object_reference、missing_fields、condition，以及适用时的 candidate_matter_id。WP02 可附带可选 `commerce` 槽位，包含本轮提及的商品、用途、规格、活动、订单号及个人查询子类。授权服务独立核实身份和对象。图应用侧另记录解析状态。
- **意图枚举**：public_knowledge、product_advice、inventory_query、personal_query、public_promotion、activity_participation、subscribe、unsubscribe、public_after_sale_policy、human_handoff、memory_update、unknown。
- **校验**：证据须对应本次消息或授权引用。候选事项只能来自输入白名单。条件以有限业务条件表达，包含依赖的前置意图或对象。未知条件先澄清。模型不生成可信身份，不直接调用写操作。
- **失败**：按技术基线限制重试。重试仍失败时输出可追踪的 failure，走澄清或人工路径。仅一条明确人工请求可由可信的显式命令分支直接处理。否定、转述和条件句仍走语义判断。
- **范围**：WP01 优先验证公开知识、一般售后、明确找人工及 unknown 的分支。后续工作包激活其余能力，并使用同一份授权和校验机制。

## 5. WP02 接口与个人查询边界

- 运营会话可读取 GET /api/v1/commerce/capabilities、GET /api/v1/commerce/bindings、GET /api/v1/matters/{matter_id}/commerce-checks。查询依据只返回动作、结果分类、来源、时间及必要公开引用，个人订单、物流及权益明细不进入运营会话详情。
- POST /api/v1/dev/commerce/bindings 复用开发凭据，只用于配置了 fixture 商家能力的本机连接。bind 需要提供 fixture 验证证明，可提供私密会话标识。unbind 解除当前关联。相同操作复用版本，私密会话归属冲突返回 409。
- 私密请求必须同时匹配已核验的渠道身份与指定私密会话。业务查询结果在写入和发送前分别校验绑定版本及事项状态。解绑后取消尚未开始发送的个人答复。发送结束或取消时清除队列中的私密正文，运营后台保留脱敏摘要和查询依据。
- 查询端口区分成功、未找到、无权限、未配置、暂时失败与规则冲突。公开商品、价格、规格和活动规则按业务来源返回，群聊只给本人查询引导。外部商家 API 的具体认证和字段在实际接入时确定。

## 6. WP03 运营、群分析与成员操作

会员档案绑定到商家已核验身份。成员偏好修改及活动参与由规范入站消息触发，后台管理写入复用会话、CSRF、幂等键及版本检查。候选人群保存确认时范围，实际投递前重查在 WP04 接入。群分析需要群级许可。日报按上海业务日汇总允许分析的已收消息。详见 [WP03 HTTP 契约](specs/WP03_API.md)。

## 7. WP04 触达与规范业务事件

运营确认版本、目标和排期后生成待发记录。群、会员、活动、订单和库存等资格在开始发送前复核。已受理、已发送与已送达独立记录。超时或重领到曾经开始的发送进入待核实。会员侧入站支持 `message.image_present`，当前测试渠道无图片内容时提示补充型号或文字。

## 8. 验证与版本维护

WP01 验证：同一测试事件重复提交、相同键不同事件冲突、两位会员同群交叉消息、明确人工和带条件人工、空模型输出、越权候选事项、后台会话和 CSRF、版本冲突、通知失败及重启后恢复。DB 事务、业务结果和模型表现分别记录证据。

共享约定由本文维护。WP01 和 WP02 的请求和响应以各自的 OpenAPI 文件为准。后续工作包在进入实现前补充具体字段、示例、权限及验收。真实渠道回调契约以供应商实际授权能力和联调记录为依据。参考 [OpenAPI 3.1 规范](https://spec.openapis.org/oas/v3.1.1.html)。
