"""Atomic shared concurrency slots for SQLite and PostgreSQL deployments."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ConcurrencyLeaseRecord, ConcurrencyQuotaRecord


class ConcurrencyRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _insert(session: AsyncSession, model: type[Any]) -> Any:
        dialect = session.bind.dialect.name if session.bind is not None else ""
        if dialect == "postgresql":
            return postgres_insert(model)
        if dialect == "sqlite":
            return sqlite_insert(model)
        raise ValueError("shared concurrency requires PostgreSQL or SQLite")

    @staticmethod
    async def _database_now(session: AsyncSession) -> datetime:
        value = await session.scalar(select(func.current_timestamp()))
        if not isinstance(value, datetime):
            raise TypeError("database did not return its current time")
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    async def try_claim(
        self, resource_key: str, capacity: int, owner_token: str, lease_seconds: int
    ) -> bool:
        async with self.session_factory() as session, session.begin():
            quota_insert = self._insert(session, ConcurrencyQuotaRecord).values(
                resource_key=resource_key, capacity=capacity
            )
            await session.execute(
                quota_insert.on_conflict_do_nothing(index_elements=["resource_key"])
            )
            quota = await session.get(ConcurrencyQuotaRecord, resource_key)
            if quota is None or quota.capacity != capacity:
                raise ValueError("shared concurrency capacity differs between processes")
            expires_at = await self._database_now(session) + timedelta(seconds=lease_seconds)
            for slot in range(capacity):
                claim = self._insert(session, ConcurrencyLeaseRecord).values(
                    resource_key=resource_key,
                    slot=slot,
                    owner_token=owner_token,
                    expires_at=expires_at,
                )
                claimed = await session.scalar(
                    claim.on_conflict_do_update(
                        index_elements=["resource_key", "slot"],
                        set_={"owner_token": owner_token, "expires_at": expires_at},
                        where=ConcurrencyLeaseRecord.expires_at <= func.current_timestamp(),
                    ).returning(ConcurrencyLeaseRecord.slot)
                )
                if claimed is not None:
                    return True
            return False

    async def renew(self, owner_token: str, expected_slots: int, lease_seconds: int) -> bool:
        async with self.session_factory() as session, session.begin():
            expires_at = await self._database_now(session) + timedelta(seconds=lease_seconds)
            result = await session.execute(
                update(ConcurrencyLeaseRecord)
                .where(
                    ConcurrencyLeaseRecord.owner_token == owner_token,
                    ConcurrencyLeaseRecord.expires_at > func.current_timestamp(),
                )
                .values(expires_at=expires_at)
            )
            return cast(CursorResult[Any], result).rowcount == expected_slots

    async def release(self, owner_token: str) -> None:
        async with self.session_factory() as session, session.begin():
            await session.execute(
                delete(ConcurrencyLeaseRecord).where(
                    ConcurrencyLeaseRecord.owner_token == owner_token
                )
            )
