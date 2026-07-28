from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    ApprovalRecord,
    DatasetSnapshotRecord,
    EvaluationRunRecord,
    EventRecord,
    FindingRecord,
    PolicySnapshotRecord,
    RunStepRecord,
    TargetRecord,
)
from app.schemas.run_schema import CaseRunResult, EvaluationOutcome, LoadedDataset, RunBudget


class RunRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def create_run(
        self,
        *,
        target_snapshot: dict[str, Any],
        dataset: LoadedDataset,
        budget: RunBudget,
    ) -> str:
        run_id = str(uuid4())
        async with self.session_factory.begin() as session:
            target = TargetRecord(
                id=str(uuid4()),
                name=str(target_snapshot["name"]),
                endpoint=str(target_snapshot["endpoint"]),
                config_json=target_snapshot,
            )
            existing_dataset = await session.scalar(
                select(DatasetSnapshotRecord).where(DatasetSnapshotRecord.sha256 == dataset.sha256)
            )
            dataset_record = existing_dataset or DatasetSnapshotRecord(
                id=str(uuid4()),
                name=dataset.name,
                version=dataset.version,
                sha256=dataset.sha256,
                snapshot_json=dataset.snapshot,
            )
            policy = PolicySnapshotRecord(
                id=str(uuid4()),
                config_json=budget.model_dump(mode="json"),
            )
            session.add(target)
            if existing_dataset is None:
                session.add(dataset_record)
            session.add(policy)
            await session.flush()
            run = EvaluationRunRecord(
                id=run_id,
                target_id=target.id,
                dataset_id=dataset_record.id,
                policy_id=policy.id,
                total_cases=len(dataset.cases),
            )
            session.add(run)
            await session.flush()
            session.add(
                EventRecord(
                    id=str(uuid4()),
                    run_id=run_id,
                    sequence=1,
                    operation_id=f"{run_id}:run_started",
                    event_type="run_started",
                    evidence_json={
                        "dataset_name": dataset.name,
                        "dataset_sha256": dataset.sha256,
                        "case_order": [case.id for case in dataset.cases],
                        "budget": budget.model_dump(mode="json"),
                    },
                )
            )
        return run_id

    async def get_case_result(self, operation_id: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            step = await session.scalar(
                select(RunStepRecord).where(RunStepRecord.operation_id == operation_id)
            )
            return None if step is None else step.result_json

    async def record_case_result(
        self,
        *,
        run_id: str,
        operation_id: str,
        step_sequence: int,
        result: CaseRunResult,
    ) -> dict[str, Any]:
        existing = await self.get_case_result(operation_id)
        if existing is not None:
            return existing

        result_json = result.model_dump(mode="json")
        async with self.session_factory.begin() as session:
            last_sequence = await session.scalar(
                select(func.max(EventRecord.sequence)).where(EventRecord.run_id == run_id)
            )
            event_sequence = int(last_sequence or 0)
            step = RunStepRecord(
                id=str(uuid4()),
                run_id=run_id,
                case_id=result.case.id,
                operation_id=operation_id,
                sequence=step_sequence,
                status="completed",
                outcome=result.outcome.value,
                result_json=result_json,
            )
            session.add(step)
            await session.flush()

            evidence_event_ids: list[str] = []
            for call_index, call in enumerate(result.calls, start=1):
                event_sequence += 1
                event_id = str(uuid4())
                session.add(
                    EventRecord(
                        id=event_id,
                        run_id=run_id,
                        step_id=step.id,
                        sequence=event_sequence,
                        operation_id=f"{operation_id}:target:{call_index}",
                        event_type="target_called",
                        evidence_json=call.model_dump(mode="json"),
                    )
                )
                evidence_event_ids.append(event_id)

            event_sequence += 1
            evaluation_event_id = str(uuid4())
            session.add(
                EventRecord(
                    id=evaluation_event_id,
                    run_id=run_id,
                    step_id=step.id,
                    sequence=event_sequence,
                    operation_id=f"{operation_id}:evaluation",
                    event_type="evaluation_completed",
                    evidence_json={
                        "case_id": result.case.id,
                        "required_evidence": result.case.required_evidence,
                        "evaluation": result.evaluation.model_dump(mode="json"),
                        "budget": result.budget.model_dump(mode="json"),
                    },
                )
            )
            evidence_event_ids.append(evaluation_event_id)

            if result.outcome == EvaluationOutcome.budget_aborted:
                event_sequence += 1
                session.add(
                    EventRecord(
                        id=str(uuid4()),
                        run_id=run_id,
                        step_id=step.id,
                        sequence=event_sequence,
                        operation_id=f"{operation_id}:budget",
                        event_type="budget_exhausted",
                        evidence_json={"reason": result.evaluation.reason},
                    )
                )

            if result.evaluation.violated:
                await session.flush()
                session.add(
                    FindingRecord(
                        id=str(uuid4()),
                        run_id=run_id,
                        step_id=step.id,
                        event_id=evaluation_event_id,
                        operation_id=f"{operation_id}:finding",
                        case_id=result.case.id,
                        category=result.case.category,
                        risk_level=result.evaluation.risk_level.value,
                        outcome=result.outcome.value,
                        reason=result.evaluation.reason,
                        evidence_event_ids=evidence_event_ids,
                        is_control=result.case.kind.value == "control",
                    )
                )
                event_sequence += 1
                session.add(
                    EventRecord(
                        id=str(uuid4()),
                        run_id=run_id,
                        step_id=step.id,
                        sequence=event_sequence,
                        operation_id=f"{operation_id}:finding_event",
                        event_type="finding_created",
                        evidence_json={
                            "case_id": result.case.id,
                            "evidence_event_ids": evidence_event_ids,
                        },
                    )
                )

        return result_json

    async def finalize_run(
        self,
        run_id: str,
        *,
        counts: dict[str, int],
        target_call_count: int,
    ) -> None:
        async with self.session_factory.begin() as session:
            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            run.status = "completed"
            run.completed_cases = sum(
                counts.get(outcome, 0)
                for outcome in ("violation", "refused", "safe", "error", "budget_aborted")
            )
            run.target_call_count = target_call_count
            run.violation_count = counts.get("violation", 0)
            run.refused_count = counts.get("refused", 0)
            run.safe_count = counts.get("safe", 0)
            run.error_count = counts.get("error", 0)
            run.budget_aborted_count = counts.get("budget_aborted", 0)
            run.false_positive_count = counts.get("false_positive", 0)
            run.defense_overblock_count = counts.get("defense_overblock", 0)
            run.completed_at = datetime.now(UTC)

            last_sequence = await session.scalar(
                select(func.max(EventRecord.sequence)).where(EventRecord.run_id == run_id)
            )
            session.add(
                EventRecord(
                    id=str(uuid4()),
                    run_id=run_id,
                    sequence=int(last_sequence or 0) + 1,
                    operation_id=f"{run_id}:run_completed",
                    event_type="run_completed",
                    evidence_json={
                        "counts": counts,
                        "target_call_count": target_call_count,
                    },
                )
            )

    async def get_report_rows(self, run_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            target = await session.get(TargetRecord, run.target_id)
            dataset = await session.get(DatasetSnapshotRecord, run.dataset_id)
            policy = await session.get(PolicySnapshotRecord, run.policy_id)
            steps = list(
                (
                    await session.scalars(
                        select(RunStepRecord)
                        .where(RunStepRecord.run_id == run_id)
                        .order_by(RunStepRecord.sequence)
                    )
                ).all()
            )
            events = list(
                (
                    await session.scalars(
                        select(EventRecord)
                        .where(EventRecord.run_id == run_id)
                        .order_by(EventRecord.sequence)
                    )
                ).all()
            )
            findings = list(
                (
                    await session.scalars(
                        select(FindingRecord)
                        .where(FindingRecord.run_id == run_id)
                        .order_by(FindingRecord.created_at)
                    )
                ).all()
            )
            approvals = list(
                (
                    await session.scalars(
                        select(ApprovalRecord)
                        .where(ApprovalRecord.run_id == run_id)
                        .order_by(ApprovalRecord.requested_at)
                    )
                ).all()
            )
            return {
                "run": self._run_dict(run),
                "target": {
                    "id": target.id,
                    "name": target.name,
                    "endpoint": target.endpoint,
                    "config": target.config_json,
                }
                if target
                else None,
                "dataset": {
                    "id": dataset.id,
                    "name": dataset.name,
                    "version": dataset.version,
                    "sha256": dataset.sha256,
                }
                if dataset
                else None,
                "policy": policy.config_json if policy else None,
                "steps": [self._step_dict(step) for step in steps],
                "events": [self._event_dict(event) for event in events],
                "findings": [self._finding_dict(finding) for finding in findings],
                "approvals": [
                    {
                        "id": approval.id,
                        "case_id": approval.case_id,
                        "status": approval.status,
                        "risk_summary": approval.risk_summary,
                        "requested_at": approval.requested_at.isoformat(),
                        "resolved_at": (
                            approval.resolved_at.isoformat() if approval.resolved_at else None
                        ),
                        "resolved_by": approval.resolved_by,
                        "reason": approval.reason,
                    }
                    for approval in approvals
                ],
            }

    @staticmethod
    def _run_dict(run: EvaluationRunRecord) -> dict[str, Any]:
        return {
            column.name: (value.isoformat() if isinstance(value, datetime) else value)
            for column in EvaluationRunRecord.__table__.columns
            if (value := getattr(run, column.name)) is not None
        }

    @staticmethod
    def _step_dict(step: RunStepRecord) -> dict[str, Any]:
        return {
            "id": step.id,
            "case_id": step.case_id,
            "operation_id": step.operation_id,
            "sequence": step.sequence,
            "status": step.status,
            "outcome": step.outcome,
            "result": step.result_json,
        }

    @staticmethod
    def _event_dict(event: EventRecord) -> dict[str, Any]:
        return {
            "id": event.id,
            "step_id": event.step_id,
            "sequence": event.sequence,
            "operation_id": event.operation_id,
            "event_type": event.event_type,
            "evidence": event.evidence_json,
            "created_at": event.created_at.isoformat(),
        }

    @staticmethod
    def _finding_dict(finding: FindingRecord) -> dict[str, Any]:
        return {
            "id": finding.id,
            "case_id": finding.case_id,
            "category": finding.category,
            "risk_level": finding.risk_level,
            "outcome": finding.outcome,
            "reason": finding.reason,
            "evidence_event_ids": finding.evidence_event_ids,
            "is_control": finding.is_control,
        }
