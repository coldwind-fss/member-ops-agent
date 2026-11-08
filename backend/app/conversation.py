import re
from uuid import uuid4

from .activities import activity_information, activity_request, activity_result
from .agent import Intent, find_knowledge
from .commerce import COMMERCE_KINDS, checks, commerce_outcome, is_commerce_followup, result as business_result, safe_preview
from .identity import binding_current
from .insights import observe_group
from .members import guide_preferences, memory_reply, profile_current, remember
from .merchant import merchant_for_connection
from .outreach import group_settings
from .queue import enqueue, fence, finish
from .store import execute, json_data, many, one
from .subscriptions import apply_subscription, prepare_subscription, subscription_intent
from .working_hours import next_work_time


def scope_for(event):
    return {"connection": event["connection_id"], "kind": event["conversation"]["kind"],
            "scope": event["conversation"]["external_id"], "member": event["sender_external_id"]}


async def context_for(conn, event):
    scope = scope_for(event)
    candidates = await many(conn, """
        SELECT id,subject,public_subject,status,
            (SELECT r.intents[1] FROM agent_runs r WHERE r.matter_id=matters.id
             ORDER BY recorded_at DESC,id DESC LIMIT 1) AS last_intent
        FROM matters
        WHERE connection_id=:connection AND conversation_kind=:kind AND conversation_external_id=:scope
          AND participant_label=:member AND (status='human_handling'
              OR (status IN ('open','waiting_member') AND updated_at > now()-interval '24 hours'))
        ORDER BY updated_at DESC LIMIT 5
    """, **scope)
    quote = None
    reference = event["message"]["reply_to_external_id"]
    if reference:
        quote = await one(conn, """
            SELECT msg.kind, m.id AS matter_id, m.participant_label, m.subject, m.public_subject, m.status,
                (SELECT r.intents[1] FROM agent_runs r WHERE r.matter_id=m.id
                 ORDER BY recorded_at DESC,id DESC LIMIT 1) AS last_intent
            FROM messages msg
            LEFT JOIN LATERAL (
                SELECT linked.matter_id FROM matter_inputs linked WHERE linked.message_id=msg.id
                ORDER BY linked.item_index LIMIT 1
            ) linked ON true
            LEFT JOIN matters m ON m.id=COALESCE(msg.matter_id,linked.matter_id)
            WHERE msg.connection_id=:connection AND msg.conversation_kind=:kind
                AND msg.conversation_external_id=:scope AND msg.external_id=:reference
        """, **scope, reference=reference)
        if quote and quote["matter_id"] and quote["participant_label"] == scope["member"]:
            if all(candidate["id"] != quote["matter_id"] for candidate in candidates):
                candidates.insert(0, {"id": quote["matter_id"], "subject": quote["subject"],
                                      "public_subject": quote["public_subject"], "status": quote["status"],
                                      "last_intent": quote["last_intent"]})
    return candidates, quote


def choose_candidate(intent, candidates, quote, event, *, multiple):
    shopping = {"inventory_query", "product_advice"}
    member = event["sender_external_id"]
    subject = intent.object_reference or (quote["public_subject"] if quote else None)
    message = event["message"]["text"]

    def compatible(row):
        if subject and row["public_subject"] and subject != row["public_subject"]:
            return False
        return (intent.kind in {"unknown", "human_handoff", row["last_intent"]}
                or (intent.kind == "inventory_query" and row["last_intent"] == "product_advice"
                    and is_commerce_followup(message))
                or row["last_intent"] in {None, "unknown"})

    candidates = [row for row in candidates if compatible(row)]
    if intent.candidate_matter_id:
        return next((row["id"] for row in candidates if str(row["id"]) == intent.candidate_matter_id), None)
    if (not multiple and quote and quote["participant_label"] == member and compatible(quote)
            and (intent.kind == "human_handoff" or is_commerce_followup(message)
                 or re.search(r"这|那个|刚才|补充|还是|也|再|催|呢|可以吗", message))
            and (not intent.object_reference or not quote["public_subject"] or
                 intent.object_reference == quote["public_subject"])):
        return quote["matter_id"]
    if len(candidates) == 1 and not multiple:
        current = candidates[0]
        if intent.kind == "human_handoff" and intent.condition is None:
            return current["id"]
        new_guide = intent.kind == "product_advice" and re.search(r"推荐|比较", message) and not re.search(r"再|刚才|还是", message)
        if intent.kind in COMMERCE_KINDS and not new_guide and (is_commerce_followup(message) or (
            current["status"] == "waiting_member" and intent.kind in shopping
        )):
            return current["id"]
        if (current["status"] == "human_handling"
                and (intent.kind == "human_handoff" or re.search(r"补充|催|同一|刚才.*(售后|投诉)", message))):
            return current["id"]
        if (current["status"] == "waiting_member"
                and (intent.object_reference or intent.kind == "unknown")
                and len(message) <= 48 and not re.search(r"订单|活动|优惠|物流", message)):
            return current["id"]
    return None


async def prepare_matter(engine, job, event, event_id, index, candidate):
    async with engine.begin() as conn:
        if not await fence(conn, job):
            return None
        previous = await one(conn, """
            SELECT m.* FROM matter_inputs i JOIN matters m ON m.id=i.matter_id
            WHERE i.message_id=:message AND i.item_index=:index
        """, message=event_id, index=index)
        if previous:
            return previous
        scope = scope_for(event)
        matter = None
        if candidate:
            matter = await one(conn, """
                SELECT * FROM matters WHERE id=:id AND connection_id=:connection
                AND conversation_kind=:kind AND conversation_external_id=:scope AND participant_label=:member
            """, id=candidate, **scope)
        if not matter:
            matter = await one(conn, """
                INSERT INTO matters (id,connection_id,conversation_kind,conversation_external_id,
                    participant_label,subject,status)
                VALUES (:id,:connection,:kind,:scope,:member,:subject,'open') RETURNING *
            """, id=uuid4(), subject=safe_preview(event["message"]["text"], scope["kind"])[:160] or "空消息", **scope)
        await execute(conn, "INSERT INTO matter_inputs VALUES (:matter,:message,:index)",
                      matter=matter["id"], message=event_id, index=index)
        return matter


async def make_delivery(conn, matter, key, *, message_id=None, handoff_id=None, run_after=None,
                        authorization=None, private_body=None):
    kind = "reply" if message_id else "notification"
    grant = authorization or {}
    delivery = await one(conn, """
        INSERT INTO deliveries (id,operation_key,kind,matter_id,expected_version,message_id,handoff_id,
            binding_id,binding_version,private_body,profile_id,profile_version)
        VALUES (:id,:key,:kind,:matter,:version,:message,:handoff,:binding,:grant_version,:body,:profile,:profile_version) RETURNING id
    """, id=uuid4(), key=key, kind=kind, matter=matter["id"], version=matter["version"],
         message=message_id, handoff=handoff_id, binding=grant.get("id"),
         grant_version=grant.get("version"), body=private_body, profile=grant.get("profile_id"),
         profile_version=grant.get("profile_version"))
    await enqueue(conn, "deliver", key, f"delivery:{kind}:{matter['id']}", {"delivery_id": str(delivery["id"])}, run_after=run_after)
    return delivery["id"]


async def reply(conn, matter, text, key, original=None, *, authorization=None):
    message_id = uuid4()
    preview = "已生成本人业务查询答复（私密内容）" if authorization else text
    await execute(conn, """
        INSERT INTO messages (id,matter_id,kind,connection_id,conversation_kind,conversation_external_id,
            in_reply_to,preview,occurred_at,delivery_state)
        VALUES (:id,:matter,'assistant',:connection,:kind,:scope,:original,:text,now(),'pending')
    """, id=message_id, matter=matter["id"], connection=matter["connection_id"], kind=matter["conversation_kind"],
         scope=matter["conversation_external_id"], original=original, text=preview)
    await make_delivery(conn, matter, f"reply:{key}", message_id=message_id, authorization=authorization,
                        private_body=text if authorization else None)


async def handoff(conn, matter, summary, reason, key, original, *, acknowledge=True):
    existing = await one(conn, "SELECT * FROM handoffs WHERE matter_id=:id FOR UPDATE", id=matter["id"])
    if existing and existing["status"] == "pending":
        await execute(conn, "UPDATE handoffs SET version=version+1,updated_at=now() WHERE id=:id", id=existing["id"])
        return
    handoff_id = existing["id"] if existing else uuid4()
    await execute(conn, """
        INSERT INTO handoffs (id,matter_id,status,summary,reason,notification_state)
        VALUES (:id,:matter,'pending',:summary,:reason,'pending')
        ON CONFLICT (matter_id) DO UPDATE SET status='pending',summary=EXCLUDED.summary,
            reason=EXCLUDED.reason,notification_state='pending',resolution=NULL,
            version=handoffs.version+1,updated_at=now()
    """, id=handoff_id, matter=matter["id"], summary=summary[:600], reason=reason)
    run_after = next_work_time()
    delivery_id = await make_delivery(conn, matter, f"notification:{key}", handoff_id=handoff_id, run_after=run_after)
    await execute(conn, "UPDATE handoffs SET notification_delivery_id=:delivery WHERE id=:id",
                  delivery=delivery_id, id=handoff_id)
    await enqueue(conn, "prepare_handoff", f"prepare-handoff:{delivery_id}", f"matter:{matter['id']}",
                  {"matter_id": str(matter["id"]), "cycle": str(delivery_id)})
    notice = "已登记，运营将在工作时间跟进。" if run_after else "已登记给运营跟进。"
    if acknowledge:
        await reply(conn, matter, notice, key, original)


async def outcome_for(engine, answer_graph, commerce_graph, event, intent, matter, public_reference, model_error, *, multiple):
    question = intent.evidence if multiple or intent.kind == "unknown" else event["message"]["text"]
    if matter["status"] == "human_handling":
        return {"outcome": "handoff", "reason": "APPENDED_TO_HANDOFF", "evidence": [], "answer": None}
    if event["message"].get("image_present") and (not event["message"]["text"] or re.search(r"图片|图里|这张图", question)):
        return business_result("clarification", "当前无法从图片确认商品。请补充商品名称、型号或相关文字。",
                               reason="IMAGE_TEXT_REQUIRED")
    if model_error:
        return {"outcome": "unavailable", "reason": model_error, "evidence": [],
                "answer": "暂时无法确认这个问题。你可以稍后再问，或回复「找人工」请运营协助。"}
    if intent.kind == "memory_update":
        return await memory_reply(engine, event, intent)
    if intent.kind in {"subscribe", "unsubscribe"}:
        return await prepare_subscription(event, intent, matter, public_reference, question)
    if intent.kind == "activity_participation" and activity_request(question):
        return {**business_result("ignored", None), "activity_request": question}
    if intent.kind in {"activity_participation", "public_promotion", "unknown"}:
        info = await activity_information(engine, event, question)
        if info:
            return info
    if intent.kind == "activity_participation":
        return business_result("clarification", "请说明要查询或参加哪场活动。", reason="ACTIVITY_UNCLEAR")
    if intent.kind in COMMERCE_KINDS or (intent.condition == "requires_business_verification"
            and "order" in merchant_for_connection(event["connection_id"]).available):
        preferences, grant = await guide_preferences(engine, event) if intent.kind == "product_advice" else ({}, None)
        outcome = await commerce_outcome(engine, commerce_graph, event, intent, matter, public_reference, question,
                                         preferences=preferences)
        if outcome.get("used_preferences"):
            outcome["authorization"] = grant
        return outcome
    if intent.kind == "human_handoff":
        if intent.condition == "requires_business_verification":
            return {"outcome": "unavailable", "reason": "UNVERIFIED_CONDITION", "evidence": [],
                    "answer": "目前无法核实这个条件，尚未转人工。需要运营直接协助时，请回复「找人工」。"}
        if intent.condition == "knowledge_missing":
            hits, reason = await find_knowledge(engine, question, public_reference, "public_knowledge")
            if hits:
                intent = intent.model_copy(update={"kind": "public_knowledge", "condition": None})
            elif reason in {"MISSING_PRODUCT", "AMBIGUOUS_PRODUCT"}:
                return {"outcome": "clarification", "reason": reason, "evidence": [],
                        "answer": "请补充具体款式，我先核实对应资料。"}
        if intent.kind == "human_handoff":
            return {"outcome": "handoff", "reason": "HUMAN_REQUEST_OR_COMPLAINT", "evidence": [], "answer": None}
    if intent.kind not in {"public_knowledge", "public_after_sale_policy", "unknown"}:
        return {"outcome": "unavailable", "reason": "CAPABILITY_UNAVAILABLE", "evidence": [],
                "answer": "目前无法查询这项业务数据。请使用品牌官方服务入口，或回复「找人工」。"}
    if intent.kind == "unknown" and matter["status"] == "waiting_member" and len(question) <= 48:
        question = f"{matter['subject']} {question}"
    if intent.kind == "unknown" and not re.search(r"洗|保养|品牌|退换|烘干", question):
        return {"outcome": "clarification", "reason": "UNKNOWN_INTENT", "evidence": [],
                "answer": "你想了解哪款商品、品牌资料或售后规则？"}
    result = await answer_graph.ainvoke(
        {"question": question, "public_reference": public_reference or matter["public_subject"],
         "intent": intent.kind},
        {"configurable": {"thread_id": str(matter["id"])}},
    )
    if result["answer"]:
        return {"outcome": "answered", **result}
    if result["reason"] in {"MISSING_PRODUCT", "AMBIGUOUS_PRODUCT"}:
        return {"outcome": "clarification", **result, "answer": "请告诉我具体是哪款商品，我再查对应说明。"}
    return {"outcome": "unavailable", **result,
            "answer": "当前没有足够可靠的资料确认这个问题。需要进一步协助时，可以回复「找人工」。"}


async def apply_outcome(engine, job, event, matter, key, intent, result):
    async with engine.begin() as conn:
        if not await fence(conn, job):
            return
        if await one(conn, "SELECT id FROM agent_runs WHERE operation_key=:key", key=key):
            return
        current = await one(conn, "SELECT * FROM matters WHERE id=:id FOR UPDATE", id=matter["id"])
        outcome, reason = result["outcome"], result["reason"]
        answer, evidence = result["answer"], result["evidence"]
        authorization = result.get("authorization")
        identity = authorization or result.get("memory_identity")
        commerce_checks = result.get("checks", [])
        summary = safe_preview(event["message"]["text"], event["conversation"]["kind"])
        if current["status"] == "human_handling":
            outcome, reason, answer, evidence = "handoff", "APPENDED_TO_HANDOFF", None, []
        elif current["version"] != matter["version"]:
            outcome, reason, answer, evidence = "ignored", "STALE_MATTER", None, []
        elif identity and not await binding_current(conn, identity["id"], identity["version"]):
            outcome, reason, answer, evidence = "ignored", "BINDING_CHANGED", None, []
            commerce_checks = [*commerce_checks, checks("identity", "BINDING_CHANGED")]
        elif authorization and authorization.get("profile_id") and not await profile_current(conn, authorization):
            outcome, reason, answer, evidence = "ignored", "PROFILE_CHANGED", None, []
        elif request := result.get("activity_request"):
            result = await activity_result(conn, event, job["payload"]["event_id"], request)
            outcome, reason, answer = result["outcome"], result["reason"], result["answer"]
        elif change := result.get("subscription_change"):
            updated = await apply_subscription(conn, event, job["payload"]["event_id"], change)
            outcome, reason, answer = updated["outcome"], updated["reason"], updated["answer"]
        event_id = job["payload"]["event_id"]
        subject = result.get("public_subject") or next((source["product"] for source in evidence if source["product"]), None)
        if outcome != "ignored" and (intent.kind != "memory_update" or outcome == "answered"):
            recorded = await remember(conn, event, event_id, intent, outcome, subject)
            if intent.kind == "memory_update" and not recorded:
                outcome, reason, answer = "unavailable", "PROFILE_UNAVAILABLE", "当前会员档案不可用，可回复「找人工」。"
        await execute(conn, "UPDATE group_signals SET matter_id=:matter WHERE event_id=:event AND matter_id IS NULL",
                      matter=current["id"], event=event_id)
        run_id = uuid4()
        await execute(conn, """
            INSERT INTO agent_runs (id,operation_key,matter_id,intents,outcome,error_code)
            VALUES (:id,:key,:matter,:intents,:outcome,:reason)
        """, id=run_id, key=key, matter=current["id"], intents=[intent.kind], outcome=outcome, reason=reason)
        for check in commerce_checks:
            await execute(conn, """
                INSERT INTO commerce_checks (id,matter_id,run_id,operation,result_code,source,public_reference,checked_at)
                VALUES (:id,:matter,:run,:operation,:result_code,:source,:public_reference,:checked_at)
            """, id=uuid4(), matter=current["id"], run=run_id, **check)
        if outcome == "ignored":
            return
        if reason == "APPENDED_TO_HANDOFF":
            await handoff(conn, current, summary, reason, key, event["message"]["external_id"])
            await execute(conn, "UPDATE matters SET updated_at=now() WHERE id=:id", id=current["id"])
            return
        for source in evidence:
            await execute(conn, """
                INSERT INTO answer_evidence (run_id,knowledge_id,version) VALUES (:run,:knowledge,:version)
            """, run=run_id, knowledge=source["id"], version=source["version"])
        count = current["clarification_count"]
        if outcome == "clarification":
            count += 1
            if count >= 2:
                answer += " 如仍无法确认，可以回复「找人工」。"
        status = {"handoff": "human_handling", "clarification": "waiting_member"}.get(outcome, "open")
        context = result.get("commerce_context")
        current = await one(conn, """
            UPDATE matters SET status=:status,clarification_count=:count,
                public_subject=COALESCE(:product,public_subject),
                commerce_context=COALESCE(CAST(:context AS jsonb),commerce_context),version=version+1,updated_at=now()
            WHERE id=:id RETURNING *
        """, status=status, count=count if status == "waiting_member" else 0, product=subject,
             context=json_data(context) if context is not None else None, id=current["id"])
        original = event["message"]["external_id"]
        if outcome == "handoff":
            await handoff(conn, current, summary, reason, key, original, acknowledge=not result.get("silent"))
        elif answer and not result.get("silent"):
            await reply(conn, current, answer, key, original, authorization=authorization)


async def process_inbound(engine, intent_graph, answer_graph, commerce_graph, job):
    event_id = job["payload"]["event_id"]
    async with engine.connect() as conn:
        row = await one(conn, "SELECT payload,received_at FROM inbox_events WHERE id=:id", id=event_id)
        event = row["payload"]
        candidates, quote = await context_for(conn, event)
        group = event["conversation"]["external_id"] if event["conversation"]["kind"] == "group" else None
        settings = await group_settings(conn, event["connection_id"], group)
    signal = await observe_group(engine, job, event_id, event)
    addressed = (event["conversation"]["kind"] == "private" or event["message"]["mentions_bot"]
                 or (quote and quote["kind"] == "assistant"))
    paused = not settings["reply_enabled"] or (settings["reply_since"] and row["received_at"] < settings["reply_since"])
    control = addressed and paused and subscription_intent(event["message"]["text"]) == "unsubscribe"
    if (not addressed or paused) and not control and signal != "complaint":
        async with engine.begin() as conn:
            if await fence(conn, job):
                await finish(conn, job, "done", "REPLY_PAUSED" if paused else "IGNORED_NOT_ADDRESSED")
        return
    public_reference = quote["public_subject"] if quote else None
    decision = job["payload"].get("decision")
    if event["message"].get("image_present") and not event["message"]["text"]:
        decision = {"intents": [Intent(kind="unknown", evidence="图片").model_dump()], "error_code": None}
    elif signal == "complaint":
        decision = {"intents": [Intent(kind="human_handoff", evidence=event["message"]["text"]).model_dump()],
                    "error_code": None}
    elif decision is None:
        decision = await intent_graph.ainvoke({"message": event["message"]["text"], "public_reference": public_reference,
                                              "candidates": [{**dict(item), "id": str(item["id"])} for item in candidates]})
        async with engine.begin() as conn:
            if not await fence(conn, job):
                return
            await execute(conn, "UPDATE jobs SET payload=CAST(:payload AS jsonb) WHERE id=:id",
                          payload=json_data({**job["payload"], "decision": decision}), id=job["id"])
    multiple = len(decision["intents"]) > 1
    for index, raw in enumerate(decision["intents"]):
        intent = Intent.model_validate(raw)
        key = f"{job['operation_key']}:{index}"
        async with engine.connect() as conn:
            if await one(conn, "SELECT id FROM agent_runs WHERE operation_key=:key", key=key):
                continue
        candidate = choose_candidate(intent, candidates, quote, event, multiple=multiple)
        matter = await prepare_matter(engine, job, event, event_id, index, candidate)
        if not matter:
            return
        reference = public_reference
        if intent.kind in COMMERCE_KINDS and quote and quote["matter_id"] == matter["id"]:
            reference = None  # The selected matter already owns its shopping context.
        result = await outcome_for(engine, answer_graph, commerce_graph, event, intent, matter,
                                   intent.object_reference or reference, decision["error_code"], multiple=multiple)
        if not addressed or paused:
            result["silent"] = True
        await apply_outcome(engine, job, event, matter, key, intent, result)
    async with engine.begin() as conn:
        if await fence(conn, job):
            await finish(conn, job)
