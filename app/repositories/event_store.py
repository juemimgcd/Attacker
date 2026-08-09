"""Run 事件的并发安全、幂等追加入口。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import EvaluationRunRecord, EventRecord


class EventStore:
    """用数据库原子计数器分配 Run 内序号，避免 ``MAX(sequence) + 1`` 竞争。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def append(
        self,
        *,
        run_id: str,
        operation_id: str,
        event_type: str,
        evidence: dict[str, Any],
        step_id: str | None = None,
        event_id: str | None = None,
    ) -> str:
        async with self.session_factory.begin() as session:
            return await self.append_in_session(
                session,
                run_id=run_id,
                operation_id=operation_id,
                event_type=event_type,
                evidence=evidence,
                step_id=step_id,
                event_id=event_id,
            )

    async def append_in_session(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        operation_id: str,
        event_type: str,
        evidence: dict[str, Any],
        step_id: str | None = None,
        event_id: str | None = None,
    ) -> str:
        existing = await session.scalar(
            select(EventRecord).where(EventRecord.operation_id == operation_id)
        )
        if existing is not None:
            if existing.run_id != run_id:
                raise ValueError("event operation_id is already bound to another run")
            return existing.id

        sequence = await session.scalar(
            update(EvaluationRunRecord)
            .where(EvaluationRunRecord.id == run_id)
            .values(event_sequence=EvaluationRunRecord.event_sequence + 1)
            .returning(EvaluationRunRecord.event_sequence)
        )
        if sequence is None:
            raise LookupError(f"run {run_id} not found")

        event = EventRecord(
            id=event_id or str(uuid4()),
            run_id=run_id,
            step_id=step_id,
            sequence=int(sequence),
            operation_id=operation_id,
            event_type=event_type,
            evidence_json=evidence,
        )
        session.add(event)
        await session.flush()
        return event.id
