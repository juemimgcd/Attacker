"""LangGraph checkpoint 后端选择；本地用 SQLite，生产用 PostgreSQL。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from conf.settings import CheckpointSettings


def _postgres_connection_string(value: str) -> str:
    return value.replace("postgresql+asyncpg://", "postgresql://", 1).replace(
        "postgres+asyncpg://",
        "postgresql://",
        1,
    )


@asynccontextmanager
async def open_checkpointer(
    config: CheckpointSettings,
    *,
    setup_schema: bool = True,
) -> AsyncIterator[Any]:
    """打开并初始化控制流存储；checkpoint 不承载业务 Evidence。"""

    connection_string = config.connection_string
    if connection_string.lower().startswith(
        ("postgres://", "postgresql://", "postgresql+asyncpg://", "postgres+asyncpg://")
    ):
        async with AsyncPostgresSaver.from_conn_string(
            _postgres_connection_string(connection_string)
        ) as checkpointer:
            if setup_schema:
                await checkpointer.setup()
            yield checkpointer
        return

    sqlite_path = connection_string.removeprefix("sqlite:///")
    path = Path(sqlite_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as checkpointer:
        if setup_schema:
            await checkpointer.setup()
        yield checkpointer
