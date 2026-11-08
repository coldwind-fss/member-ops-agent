from fastapi.encoders import jsonable_encoder

from .http import ApiError
from .store import execute, json_data, one


def check_version(row, expected):
    if row["version"] != expected:
        raise ApiError(409, "VERSION_CONFLICT", "记录已更新，请刷新。", {"current_version": row["version"]})


async def operator_write(request, operator, action, data, apply):
    key = request.headers.get("Idempotency-Key", "")
    if not 8 <= len(key) <= 128:
        raise ApiError(400, "BAD_REQUEST", "请提供有效的 Idempotency-Key。")
    params = dict(operator=operator.operator_id, action=action, key=key, body=json_data(data))
    async with request.app.state.database.begin() as conn:
        created = await one(conn, """
            INSERT INTO api_writes (operator_id,action,operation_key,request)
            VALUES (:operator,:action,:key,CAST(:body AS jsonb))
            ON CONFLICT DO NOTHING RETURNING operation_key
        """, **params)
        if not created:
            saved = await one(conn, """
                SELECT response,request=CAST(:body AS jsonb) AS same FROM api_writes
                WHERE operator_id=:operator AND action=:action AND operation_key=:key
            """, **params)
            if not saved["same"]:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "此请求键已用于其他内容。")
            return saved["response"]
        response = jsonable_encoder(await apply(conn))
        await execute(conn, """
            UPDATE api_writes SET response=CAST(:response AS jsonb)
            WHERE operator_id=:operator AND action=:action AND operation_key=:key
        """, response=json_data(response), **params)
        return response
