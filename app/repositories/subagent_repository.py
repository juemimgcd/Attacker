"""保存不含凭据的主任务和预分配子 Run 关系。"""

from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SubagentRunRecord


class SubagentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def create(self, manifest: dict[str, Any]) -> str:
        coordinator_id = str(uuid4())
        async with self.session_factory() as session, session.begin():
            session.add(SubagentRunRecord(id=coordinator_id, manifest_json=manifest))
        return coordinator_id

    async def finish(
        self,
        coordinator_id: str,
        errors: dict[str, str],
        model_summary: dict[str, Any] | None = None,
    ) -> None:
        async with self.session_factory() as session, session.begin():
            row = await session.get(SubagentRunRecord, coordinator_id)
            if row is None:
                raise LookupError(f"subagent coordinator {coordinator_id} not found")
            if model_summary is not None:
                row.manifest_json = {**row.manifest_json, "model_summary": model_summary}
            row.dispatch_finished = True
            row.errors_json = errors

    async def get(self, coordinator_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            row = await session.get(SubagentRunRecord, coordinator_id)
            if row is None:
                raise LookupError(f"subagent coordinator {coordinator_id} not found")
            return {
                "coordinator_id": row.id,
                "manifest": row.manifest_json,
                "dispatch_finished": row.dispatch_finished,
                "errors": row.errors_json,
                "created_at": row.created_at.isoformat(),
            }

    async def list_recent(self, limit: int) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            rows = await session.scalars(
                select(SubagentRunRecord)
                .order_by(SubagentRunRecord.created_at.desc(), SubagentRunRecord.id)
                .limit(limit)
            )
            return [
                {
                    "coordinator_id": row.id,
                    "created_at": row.created_at.isoformat(),
                    "dispatch_finished": row.dispatch_finished,
                    "mode": row.manifest_json["mode"],
                    "has_model_summary": "model_summary" in row.manifest_json,
                    "subagents": row.manifest_json["subagents"],
                }
                for row in rows
            ]
