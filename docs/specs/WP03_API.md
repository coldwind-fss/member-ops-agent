# WP03 HTTP 契约

以下路径统一位于 `/api/v1`。运营读取需要会话。写入需要会话、CSRF 和 Idempotency-Key，重复请求返回原结果，请求键复用但内容不同返回 409。版本不匹配返回 VERSION_CONFLICT，非法状态返回 INVALID_STATE，缺失对象返回 404，字段无效返回 422。时间字段带时区。集合响应使用 `items`，小规模筛选返回完整命中与排除原因。

| 路径 | 输入 | 输出 |
| --- | --- | --- |
| GET /members | 可选 `group_id` | 已有画像的 id、渠道标识、核验状态、最近活动、群范围 |
| GET /members/{id} | 会员 UUID | 画像、带证据的记忆和候选、时间线、群资格 |
| POST /members/{id}/memories/{memory_id} | `action` confirm、correct、delete，`expected_version`，correct 时提供 `value` | 条目、新版本与审核状态 |
| POST /members/{id}/delete | `expected_version` | 删除结果及新画像版本 |
| GET /audiences/preview | 可选 `group_id`、`tag`、`interest`、`recent_days` | 每个画像的命中或排除及原因，和总人数 |
| GET /audiences | 无 | 已保存的候选范围 |
| POST /audiences | 同筛选条件，另需 `name`、`confirmed_member_ids` | 固化筛选条件与实际确认名单。确认前重新检查当前命中 |
| GET /activities | 无 | 活动、有效状态及参与计数 |
| POST /activities | `title, starts_at, ends_at, group_id, rule, checkin, question, allow_makeup, allow_edit` | 草稿及 version |
| GET /activities/{id} | 活动 UUID | 当前活动、规则版本、脱敏参与和进度 |
| POST /activities/{id} | `action` edit、publish、pause、resume、cancel，`expected_version`，edit 另需 `config` | 更新后的活动及 version |
| GET /knowledge | 无 | 知识条目、草稿和历史版本 |
| POST /knowledge/drafts | 可选 `knowledge_id`、`matter_id`、`signal_id`，`category, title, body, product, topic, aliases, keywords, valid_from, valid_until` | 草稿和预期的知识版本 |
| POST /knowledge/drafts/{id}/publish | `expected_version` | 发布条目和版本 |
| POST /knowledge/verify | `draft_id, questions, expected_text`，可选 `intent` | 各问题的实际回答、来源版本、结果和验证记录 id |
| GET /insights/groups | 无 | 已配置及测试渠道已出现的群 |
| POST /insights/groups | `connection_id, group_id, enabled` | 群的当前分析许可 |
| GET /insights | `connection_id, group_id, day`（上海业务日） | 可用范围、日聚合、原消息证据、活动进度 |
| POST /insights/signals/{id} | `action` confirm、correct、close、candidate，`expected_version`，correct 时提供 `topic` | 信号新状态与版本 |
| POST /dev/group-memberships | X-Dev-Token 和 `connection_id, group_id, sender_external_id, active` | 测试渠道的加入或退出结果 |

会员侧沿用规范消息入口。明确的本人偏好用例如「我平时穿 M 码」「我现在穿 L 码」「别再记我的尺码」。报名、打卡和问卷用例如「帮我报名晨跑挑战」「晨跑挑战打卡」「晨跑挑战问卷：跑了 5 公里」。问卷只能通过已核验的私密会话提交。活动资格采用已绑定会员及活动指定群的有效群资格。打卡周期按上海业务日，补打卡日期用 YYYY-MM-DD；同周期及同问卷重投由参与记录去重。后台参与列表不返回问卷正文。

配置了群分析才处理该群随后接收的消息。读取群日报时使用所选业务日聚合已记录信号，标明渠道覆盖、未解决线索和来源失效情况。原始消息保留 90 天，群分析及处理轨迹默认显示 365 天；删除画像留下防止旧消息回放重建的最小边界。候选意向默认 30 天有效，已确认偏好保留到纠正、删除或档案失效。
