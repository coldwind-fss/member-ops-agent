from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from .http import ApiError
from .store import execute, one

BINDING_FIELDS = """
id,connection_id,sender_external_id,
CASE WHEN member_id IS NULL THEN 'unbound' ELSE 'verified' END AS status,
(private_external_id IS NOT NULL) AS private_available,
version,created_at,updated_at
"""


async def change_binding(engine, connection_id, sender, member_id, private_id):
    try:
        async with engine.begin() as conn:
            binding = await one(conn, f"""
                INSERT INTO member_bindings (id,connection_id,sender_external_id,member_id,private_external_id)
                VALUES (:id,:connection,:sender,:member,:private)
                ON CONFLICT (connection_id,sender_external_id) DO UPDATE
                SET member_id=EXCLUDED.member_id,private_external_id=EXCLUDED.private_external_id,
                    version=member_bindings.version+1,updated_at=now()
                WHERE (member_bindings.member_id,member_bindings.private_external_id)
                    IS DISTINCT FROM (EXCLUDED.member_id,EXCLUDED.private_external_id)
                RETURNING {BINDING_FIELDS}
            """, id=uuid4(), connection=connection_id, sender=sender, member=member_id, private=private_id)
            if binding is None:
                binding = await one(conn, f"""
                    SELECT {BINDING_FIELDS} FROM member_bindings
                    WHERE connection_id=:connection AND sender_external_id=:sender
                """, connection=connection_id, sender=sender)
            if member_id:
                await execute(conn, """
                    INSERT INTO member_profiles (id,merchant_member) VALUES (:id,:member)
                    ON CONFLICT (merchant_member) DO NOTHING
                """, id=uuid4(), member=member_id)
            return dict(binding)
    except IntegrityError as error:
        if getattr(error.orig.diag, "constraint_name", None) == "member_private_conversation":
            raise ApiError(409, "INVALID_STATE", "此私密会话已属于其他会员。") from None
        raise


async def get_binding(engine, connection_id, sender):
    async with engine.connect() as conn:
        return await one(conn, """
            SELECT id,member_id,private_external_id,version FROM member_bindings
            WHERE connection_id=:connection AND sender_external_id=:sender AND member_id IS NOT NULL
        """, connection=connection_id, sender=sender)


async def binding_current(conn, binding_id, version):
    return bool(await one(conn, """
        SELECT id FROM member_bindings WHERE id=:id AND version=:version AND member_id IS NOT NULL
        FOR SHARE
    """, id=binding_id, version=version))
