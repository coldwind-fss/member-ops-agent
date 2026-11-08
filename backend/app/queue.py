from uuid import uuid4

from .store import execute, json_data, one


async def enqueue(conn, kind, key, lane, payload, *, run_after=None):
    await execute(conn, """
        INSERT INTO jobs (id, operation_key, kind, lane, payload, run_after)
        VALUES (:id, :key, :kind, :lane, CAST(:payload AS jsonb), COALESCE(:at, now()))
        ON CONFLICT (operation_key) DO NOTHING
    """, id=uuid4(), key=key, kind=kind, lane=lane, payload=json_data(payload), at=run_after)


async def claim(engine):
    async with engine.begin() as conn:
        return await one(conn, """
            WITH candidate AS (
                SELECT j.id FROM jobs j
                WHERE ((j.state='pending' AND j.run_after <= now())
                    OR (j.state='running' AND j.lease_until <= now()))
                  AND NOT EXISTS (
                    SELECT 1 FROM jobs earlier
                    WHERE earlier.lane=j.lane AND earlier.state IN ('pending','running')
                      AND (earlier.created_at,earlier.id)<(j.created_at,j.id)
                  )
                ORDER BY j.run_after,j.created_at,j.id
                LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            UPDATE jobs j SET state='running', generation=j.generation+1,
                lease_until=now()+interval '5 minutes'
            FROM candidate WHERE j.id=candidate.id RETURNING j.*
        """)


async def fence(conn, job):
    return await one(conn, """
        SELECT id FROM jobs WHERE id=:id AND state='running' AND generation=:generation
        FOR UPDATE
    """, id=job["id"], generation=job["generation"])


async def finish(conn, job, state="done", error=None, *, retry_at=None):
    await execute(conn, """
        UPDATE jobs SET state=:state, lease_until=NULL, last_error=:error,
            run_after=CASE WHEN :state='pending' THEN COALESCE(:retry_at,now()+interval '2 seconds') ELSE run_after END
        WHERE id=:id AND generation=:generation
    """, id=job["id"], generation=job["generation"], state=state, error=error, retry_at=retry_at)
