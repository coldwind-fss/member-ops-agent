# WP04 HTTP 与事件契约

以下路径位于 /api/v1。运营读接口需会话，写接口需会话、CSRF 和 Idempotency-Key。版本冲突或非法转换返回 409，找不到对象返回 404，字段错误返回 422。时间使用带时区的 RFC 3339。

| 路径 | 请求 | 响应 |
| --- | --- | --- |
| GET /outreach/groups | 无 | 已知群及应答、分析、触达开关，欢迎和关怀文案 |
| POST /outreach/groups | connection_id、group_id、reply_enabled、analysis_enabled、outreach_enabled、welcome_text、care_text | 当前配置 |
| GET /outreach/plans | 无 | 计划及配置版本、目标、排期和确认状态 |
| POST /outreach/plans | title、body、connection_id、group_id、mode（group 或 personal）、scheduled_at、expires_at，可选 audience_id、activity_id、daily_limit（默认 1） | 草稿及版本 |
| POST /outreach/plans/{id} | action（edit、approve、cancel）、expected_version。edit 附 config，approve 附 member_ids | 新状态与实际批准名单 |
| GET /outreach/plans/{id}/preview | 无 | 当下候选、命中或排除原因。仅从所选已确认人群中取人 |
| POST /outreach/draft | 可选 activity_id、knowledge_id 与请求主题 | 基于有效资料的文案建议及引用，保存和发布需另行确认 |
| GET /outreach/subscriptions | 无 | 订阅规格、授权状态、期限和后续记录 |
| POST /outreach/subscriptions/{id}/cancel | expected_version | 取消订阅及关联待办 |
| GET /outreach/suggestions | 无 | 尚有依据的购买意向与处理状态 |
| POST /outreach/suggestions/{id} | action（approve、close）、expected_version、group_id；approve 附 body，close 附 reason | 实际审批、拒绝、已购或失效结果 |
| POST /outreach/care | connection_id、group_id、profile_id、order_id、body | 在已核实订单范围内登记关怀任务 |
| GET /outreach/records | 可选 state、limit、cursor | 发送证据、尝试数、原因、关联业务对象及人工待办 |
| POST /outreach/records/{id}/resolve | expected_version、state、evidence、可选 provider_ref | 依据人工联系或供应商回执核实，禁止无证据重发 |
| POST /dev/business-events | X-Dev-Token 与下表事件 | 202、event_id、duplicate。相同键且数据不一致返回 409 |

事件共用 connection_id（本机 fixture-channel）、external_event_id、event_type、occurred_at 和 historical。真实供应商认证和字段映射通过接入适配器确定。

| event_type | 额外数据 |
| --- | --- |
| member.joined、member.left | group_id、sender_external_id |
| stock.changed | product_id、color、size、quantity。只有从无货变为有货或首次可信有货才安排提醒 |
| order.signed、order.returned、order.cancelled、order.after_sale | group_id、sender_external_id、order_id |
| delivery.receipt | operation_key、state（accepted、sent、delivered、failed）、provider_ref |

成员消息例如「风行跑步上衣黑色 XL 到货提醒我」「取消风行跑步上衣黑色 XL 到货提醒」「不要购后关怀」。订阅有效期 90 天。任务持久领取后重查开关、批准版本、绑定、群资格、活动有效期、订阅、频率和最新业务证据。暂停期间保留有效订阅并延迟执行；计划超过排期 5 分钟须由运营重新排期并确认。普通营销默认同一群或同一会员每个上海业务日最多一条，关联活动的计划可以在确认时调整。供应商明确暂时失败允许重试一次；交付中断视作待核实，等待有效回执或人工证据。
