import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.models import Base


def run_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=Base.metadata,
        version_table="ai_alembic_version",
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_sync(connection) -> None:
    context.configure(
        connection=connection, target_metadata=Base.metadata, version_table="ai_alembic_version"
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(get_settings().database_url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
