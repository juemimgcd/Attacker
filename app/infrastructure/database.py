import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models import Base
from conf.settings import DatabaseSettings


class Database:
    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        auto_create_schema: bool = True,
        pool_size: int = 10,
        max_overflow: int = 20,
        pool_timeout_seconds: float = 30,
        pool_recycle_seconds: int = 1_800,
        pool_pre_ping: bool = True,
        connect_timeout_seconds: float = 10,
    ) -> None:
        self.url = url
        self.auto_create_schema = auto_create_schema
        engine_options: dict[str, Any] = {"echo": echo}
        if not url.startswith("sqlite+"):
            engine_options.update(
                {
                    "pool_size": pool_size,
                    "max_overflow": max_overflow,
                    "pool_timeout": pool_timeout_seconds,
                    "pool_recycle": pool_recycle_seconds,
                    "pool_pre_ping": pool_pre_ping,
                    "connect_args": {"timeout": connect_timeout_seconds},
                }
            )
        self.engine: AsyncEngine = create_async_engine(url, **engine_options)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        if url.startswith("sqlite+"):
            event.listen(self.engine.sync_engine, "connect", self._enable_sqlite_foreign_keys)

    @classmethod
    def from_settings(cls, config: DatabaseSettings) -> "Database":
        return cls(
            config.url,
            echo=config.echo,
            auto_create_schema=config.auto_create_schema,
            pool_size=config.pool_size,
            max_overflow=config.max_overflow,
            pool_timeout_seconds=config.pool_timeout_seconds,
            pool_recycle_seconds=config.pool_recycle_seconds,
            pool_pre_ping=config.pool_pre_ping,
            connect_timeout_seconds=config.connect_timeout_seconds,
        )

    async def initialize(self) -> None:
        self._ensure_sqlite_parent()
        if self.auto_create_schema:
            async with self.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        else:
            await self.ping()

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def readiness(self) -> dict[str, str]:
        try:
            await self.ping()
        except SQLAlchemyError as exc:
            return {
                "status": "unavailable",
                "dialect": self.engine.dialect.name,
                "error": type(exc).__name__,
            }
        return {"status": "ready", "dialect": self.engine.dialect.name}

    @asynccontextmanager
    async def advisory_lock(
        self,
        key: str,
        *,
        timeout_seconds: float = 60,
    ) -> AsyncIterator[None]:
        if self.engine.dialect.name != "postgresql":
            yield
            return
        started = monotonic()
        async with self.engine.connect() as connection:
            acquired = False
            try:
                while not acquired:
                    acquired = bool(
                        await connection.scalar(
                            text("SELECT pg_try_advisory_lock(hashtext(:key))"),
                            {"key": key},
                        )
                    )
                    if acquired:
                        break
                    if monotonic() - started >= timeout_seconds:
                        raise TimeoutError(f"timed out acquiring database advisory lock {key}")
                    await asyncio.sleep(0.25)
                yield
            finally:
                if acquired:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(hashtext(:key))"),
                        {"key": key},
                    )

    async def dispose(self) -> None:
        await self.engine.dispose()

    def _ensure_sqlite_parent(self) -> None:
        prefix = "sqlite+aiosqlite:///"
        if not self.url.startswith(prefix):
            return
        database_path = self.url.removeprefix(prefix)
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
