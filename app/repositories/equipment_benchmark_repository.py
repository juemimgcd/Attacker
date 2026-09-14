"""Persist Equipment benchmark attempts in the existing Run/Step/Event tables."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.models import EvaluationRunRecord, RunStepRecord
from app.repositories.run_repository import RunRepository


class EquipmentBenchmarkRepository:
    def __init__(self, runs: RunRepository) -> None:
        self.runs = runs

    async def begin_case(self, run_id: str, sequence: int, task_id: str, repetition: int) -> str:
        step_id = str(uuid4())
        async with self.runs.session_factory.begin() as session:
            session.add(
                RunStepRecord(
                    id=step_id,
                    run_id=run_id,
                    case_id=task_id,
                    sequence=sequence,
                    operation_id=f"{run_id}:benchmark:{sequence}",
                    status="running",
                    outcome="inconclusive",
                    result_json={"task_id": task_id, "repetition": repetition},
                )
            )
        return step_id

    async def complete_case(self, step_id: str, result: dict[str, Any]) -> None:
        async with self.runs.session_factory.begin() as session:
            step = await session.get(RunStepRecord, step_id)
            if step is None:
                raise LookupError(step_id)
            step.status = "completed"
            step.outcome = result["outcome"]
            step.completed_at = datetime.now(UTC)
            step.result_json = {**step.result_json, **result}
            await self.runs.events.append_in_session(
                session,
                run_id=step.run_id,
                step_id=step.id,
                operation_id=f"{step.operation_id}:evaluation",
                event_type="equipment_benchmark_evaluated",
                evidence=step.result_json,
            )

    async def finish(self, run_id: str, status: str, cleanup: list[dict[str, Any]]) -> None:
        async with self.runs.session_factory.begin() as session:
            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(run_id)
            steps = list(
                await session.scalars(select(RunStepRecord).where(RunStepRecord.run_id == run_id))
            )
            run.status = status
            run.completed_cases = sum(step.status == "completed" for step in steps)
            run.error_count = sum(step.outcome in {"error", "timeout", "denied"} for step in steps)
            run.completed_at = datetime.now(UTC)
            run.terminal_reason = f"equipment_benchmark_{status}"
            await self.runs.events.append_in_session(
                session,
                run_id=run_id,
                operation_id=f"{run_id}:benchmark:finished",
                event_type="equipment_benchmark_finished",
                evidence={"status": status, "cleanup": cleanup},
            )
