from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.database import Database


@pytest.fixture
async def session_factory(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "attacker-test.sqlite3"
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    await database.initialize()
    try:
        yield database.session_factory
    finally:
        await database.dispose()
