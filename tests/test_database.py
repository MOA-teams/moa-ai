import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.skipif(not os.getenv("MOA_TEST_DATABASE_URL"), reason="PostgreSQL integration opt-in")
async def test_migrated_tables_and_ai_version_history():
    engine = create_async_engine(os.environ["MOA_TEST_DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            )
            tables = {row[0] for row in result}
            assert {"ai_pipeline_runs", "ai_rag_sources", "ai_alembic_version"} <= tables
            version = await connection.execute(text("SELECT version_num FROM ai_alembic_version"))
            assert version.scalar_one() == "0001"
    finally:
        await engine.dispose()
