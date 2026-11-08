from alembic import context
from sqlalchemy import MetaData

from app.database import create_database_engine, run_with_database_loop

metadata = MetaData()


def apply_migrations(connection):
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


async def migrate() -> None:
    engine = create_database_engine()
    try:
        if context.is_offline_mode():
            context.configure(dialect_name=engine.dialect.name, target_metadata=metadata)
            with context.begin_transaction():
                context.run_migrations()
            return
        async with engine.connect() as connection:
            await connection.run_sync(apply_migrations)
    finally:
        await engine.dispose()


run_with_database_loop(migrate())
