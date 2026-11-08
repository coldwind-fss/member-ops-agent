import asyncio
import hashlib
import os
import secrets
import time
from collections import deque
from hmac import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pwdlib import PasswordHash
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .http import ApiError
from .store import execute, one

router = APIRouter(prefix="/auth")
COOKIE = "operator_session"
SESSION_SECONDS = 8 * 60 * 60
hasher = PasswordHash.recommended()


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=1, max_length=1024)


class Session(BaseModel):
    operator_id: UUID
    display_name: str
    csrf_token: str


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def verify_origin(request, *, required=False):
    origin = request.headers.get("Origin")
    if origin is None and required:
        raise ApiError(400, "BAD_REQUEST", "缺少 Origin 请求头。")
    allowed = os.getenv("OPERATOR_ORIGIN", "http://127.0.0.1:5173")
    if origin is not None and origin != allowed:
        raise ApiError(403, "FORBIDDEN", "请求来源不受信任。")


async def current_session(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        async with request.app.state.database.connect() as conn:
            session = await one(conn, """
                SELECT s.operator_id, o.display_name, s.csrf_token FROM operator_sessions s
                JOIN operators o ON o.id=s.operator_id
                WHERE s.token_digest=:digest AND s.expires_at > now()
            """, digest=digest(token))
        if session:
            return Session.model_validate(dict(session))
    raise ApiError(401, "UNAUTHENTICATED", "请先登录运营后台。")


Operator = Annotated[Session, Depends(current_session)]


async def write_session(request: Request, session: Operator):
    verify_origin(request)
    token = request.headers.get("X-CSRF-Token")
    if token is None:
        raise ApiError(400, "BAD_REQUEST", "缺少 X-CSRF-Token。")
    if not compare_digest(token.encode(), session.csrf_token.encode()):
        raise ApiError(403, "FORBIDDEN", "会话校验失败，请刷新后重试。")
    return session


Writer = Annotated[Session, Depends(write_session)]


@router.post("/login", response_model=Session)
async def login(data: Login, request: Request, response: Response):
    verify_origin(request, required=True)
    attempts = request.app.state.login_attempts
    now = time.monotonic()
    while attempts and now - attempts[0] >= 60:
        attempts.popleft()
    if len(attempts) >= 10:
        raise ApiError(429, "RATE_LIMITED", "登录尝试过于频繁，请稍后再试。")
    attempts.append(now)
    engine = request.app.state.database
    async with engine.connect() as conn:
        operator = await one(conn, "SELECT * FROM operators WHERE username=:username", username=data.username)
    if not operator or not await asyncio.to_thread(hasher.verify, data.password.get_secret_value(), operator["password_hash"]):
        raise ApiError(401, "UNAUTHENTICATED", "账号或密码不正确。")

    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    async with engine.begin() as conn:
        current = await one(conn, "SELECT password_hash FROM operators WHERE id=:id FOR UPDATE", id=operator["id"])
        if not current or current["password_hash"] != operator["password_hash"]:
            raise ApiError(401, "UNAUTHENTICATED", "运营凭据已变化，请重新登录。")
        await execute(conn, "DELETE FROM operator_sessions WHERE expires_at <= now() OR token_digest=:old",
                      old=digest(request.cookies.get(COOKIE, "")))
        await execute(conn, """
            INSERT INTO operator_sessions VALUES (:digest, :id, :csrf, now() + interval '8 hours')
        """, digest=digest(token), id=operator["id"], csrf=csrf)
    attempts.clear()
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax",
                        secure=os.getenv("OPERATOR_ORIGIN", "").startswith("https://"), max_age=SESSION_SECONDS)
    return Session(operator_id=operator["id"], display_name=operator["display_name"], csrf_token=csrf)


@router.get("/session", response_model=Session)
async def get_session(session: Operator):
    return session


@router.post("/logout", status_code=204)
async def logout(request: Request, session: Writer):
    async with request.app.state.database.begin() as conn:
        await execute(conn, "DELETE FROM operator_sessions WHERE token_digest=:digest AND operator_id=:id",
                      digest=digest(request.cookies[COOKIE]), id=session.operator_id)
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, httponly=True, samesite="lax")
    return response
