from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from pydantic import AwareDatetime, Field, StringConstraints

from .auth import Operator, Writer
from .http import ApiError
from .ingress import Input
from .operator_writes import operator_write
from .store import execute, json_data, many, one

router = APIRouter()
SearchTerm = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class Draft(Input):
    knowledge_id: UUID | None = None
    matter_id: UUID | None = None
    signal_id: UUID | None = None
    category: Literal["missing", "outdated", "intent", "query", "permission"] = "missing"
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=6000)
    product: str = Field(default="", max_length=120)
    topic: str = Field(min_length=1, max_length=60)
    aliases: list[SearchTerm] = Field(default_factory=list, max_length=20)
    keywords: list[SearchTerm] = Field(min_length=1, max_length=20)
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None


@router.get("/knowledge")
async def knowledge(request: Request, operator: Operator):
    async with request.app.state.database.connect() as conn:
        versions = await many(conn, "SELECT * FROM knowledge ORDER BY title,id,version DESC")
        drafts = await many(conn, "SELECT * FROM knowledge_drafts ORDER BY created_at DESC")
        verifications = await many(conn, "SELECT * FROM knowledge_verifications ORDER BY created_at DESC LIMIT 30")
    return {"items": list(map(dict, versions)), "drafts": list(map(dict, drafts)),
            "verifications": list(map(dict, verifications))}


@router.post("/knowledge/drafts")
async def create_draft(data: Draft, request: Request, operator: Writer):
    async def apply(conn):
        for value, table in ((data.matter_id, "matters"), (data.signal_id, "group_signals")):
            if value and not await one(conn, f"SELECT id FROM {table} WHERE id=:id", id=value):
                raise ApiError(404, "NOT_FOUND", "纠错依据不存在。")
        latest = await one(conn, "SELECT max(version) AS version FROM knowledge WHERE id=:id", id=data.knowledge_id)
        if data.knowledge_id and latest["version"] is None:
            raise ApiError(404, "NOT_FOUND", "要修订的知识不存在。")
        config = data.model_dump(mode="json", exclude={"matter_id", "signal_id", "knowledge_id", "category"})
        return dict(await one(conn, """
            INSERT INTO knowledge_drafts (id,knowledge_id,expected_version,matter_id,signal_id,category,config)
            VALUES (:id,:knowledge,:version,:matter,:signal,:category,CAST(:config AS jsonb)) RETURNING *
        """, id=uuid4(), knowledge=data.knowledge_id or uuid4(), version=latest["version"] or 0,
             matter=data.matter_id, signal=data.signal_id, category=data.category, config=json_data(config)))
    return await operator_write(request, operator, "create-draft", data.model_dump(mode="json"), apply)


class Publish(Input):
    expected_version: int = Field(ge=0)


@router.post("/knowledge/drafts/{draft_id}/publish")
async def publish_draft(draft_id: UUID, data: Publish, request: Request, operator: Writer):
    async def apply(conn):
        draft = await one(conn, "SELECT * FROM knowledge_drafts WHERE id=:id FOR UPDATE", id=draft_id)
        if not draft:
            raise ApiError(404, "NOT_FOUND", "知识草稿不存在。")
        if draft["published_version"] is not None:
            raise ApiError(409, "INVALID_STATE", "草稿已发布。")
        item = draft["knowledge_id"]
        await execute(conn, "SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))", key=f"knowledge:{item}")
        latest = await one(conn, "SELECT * FROM knowledge WHERE id=:id ORDER BY version DESC LIMIT 1", id=item)
        actual = latest["version"] if latest else 0
        if actual != data.expected_version or actual != draft["expected_version"]:
            raise ApiError(409, "VERSION_CONFLICT", "已发布其他版本，请基于最新版本重新建草稿。", {"current_version": actual})
        config = Draft.model_validate(draft["config"])
        start = config.valid_from or datetime.now(UTC)
        if (config.valid_until and config.valid_until <= start) or (latest and start < latest["valid_from"]):
            raise ApiError(422, "VALIDATION_ERROR", "请检查新版本生效时间及有效期。")
        if latest and (latest["valid_until"] is None or latest["valid_until"] > start):
            await execute(conn, "UPDATE knowledge SET valid_until=:start WHERE id=:id AND version=:version",
                          id=item, version=actual, start=start)
        row = await one(conn, """
            INSERT INTO knowledge (id,version,title,body,product,topic,aliases,keywords,valid_from,valid_until)
            VALUES (:id,:version,:title,:body,:product,:topic,:aliases,:keywords,:start,:end) RETURNING *
        """, id=item, version=actual + 1, title=config.title, body=config.body, product=config.product,
             topic=config.topic, aliases=config.aliases, keywords=config.keywords, start=start, end=config.valid_until)
        await execute(conn, "UPDATE knowledge_drafts SET published_version=:version WHERE id=:id",
                      id=draft_id, version=row["version"])
        return dict(row)
    return await operator_write(request, operator, f"publish-draft:{draft_id}", data.model_dump(), apply)


class Verification(Input):
    draft_id: UUID
    questions: list[str] = Field(min_length=1, max_length=4)
    expected_text: str = Field(min_length=1, max_length=200)
    intent: Literal["public_knowledge", "public_after_sale_policy"] = "public_knowledge"


@router.post("/knowledge/verify")
async def verify_knowledge(data: Verification, request: Request, operator: Writer):
    from .agent import build_answer_graph, model_for_worker

    if any(not question.strip() or len(question) > 500 for question in data.questions):
        raise ApiError(422, "VALIDATION_ERROR", "每条验证问题应为 1 到 500 字。")
    key = request.headers.get("Idempotency-Key", "")
    if not 8 <= len(key) <= 128:
        raise ApiError(400, "BAD_REQUEST", "请提供有效的 Idempotency-Key。")
    engine = request.app.state.database
    payload = data.model_dump(mode="json")
    async with engine.connect() as conn:
        saved = await one(conn, """
            SELECT response,request=CAST(:body AS jsonb) AS same FROM api_writes
            WHERE operator_id=:operator AND action='verify-knowledge' AND operation_key=:key
        """, operator=operator.operator_id, body=json_data(payload), key=key)
        if saved:
            if not saved["same"]:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "此请求键已用于其他内容。")
            return saved["response"]
        draft = await one(conn, "SELECT * FROM knowledge_drafts WHERE id=:id AND published_version IS NOT NULL", id=data.draft_id)
    if not draft:
        raise ApiError(404, "NOT_FOUND", "请先发布知识草稿。")
    graph = build_answer_graph(engine, model_for_worker(), None)
    cases = []
    for question in data.questions:
        answer = await graph.ainvoke({"question": question, "public_reference": None, "intent": data.intent})
        sources = [{"id": row["id"], "version": row["version"], "title": row["title"]} for row in answer["evidence"]]
        expected_source = any(source["id"] == str(draft["knowledge_id"]) and source["version"] == draft["published_version"]
                              for source in sources)
        cases.append({"question": question, "answer": answer["answer"], "reason": answer["reason"],
                      "sources": sources, "passed": expected_source and data.expected_text in (answer["answer"] or "")})

    async def apply(conn):
        return dict(await one(conn, """
            INSERT INTO knowledge_verifications (id,draft_id,cases) VALUES (:id,:draft,CAST(:cases AS jsonb)) RETURNING *
        """, id=uuid4(), draft=data.draft_id, cases=json_data(cases)))
    return await operator_write(request, operator, "verify-knowledge", payload, apply)
