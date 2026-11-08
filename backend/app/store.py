import json

from sqlalchemy import text


async def execute(conn, sql, **params):
    return await conn.execute(text(sql), params)


async def one(conn, sql, **params):
    return (await execute(conn, sql, **params)).mappings().first()


async def many(conn, sql, **params):
    return (await execute(conn, sql, **params)).mappings().all()


def json_data(data):
    return json.dumps(data, ensure_ascii=False, default=str)
