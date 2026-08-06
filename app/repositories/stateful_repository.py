"""带状态评测和 Replay 的 SQL 仓库，保存夹具、快照、检索与清理证据。"""

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    DatasetSnapshotRecord,
    EvaluationRunRecord,
    EventRecord,
    FindingRecord,
    PolicySnapshotRecord,
    ReplayRecord,
    RetrievalEventRecord,
    RunStepRecord,
    StateFixtureRecord,
    StateSnapshotRecord,
    TargetRecord,
)
from app.schemas.attack_sample_schema import BlackBoxCase
from app.schemas.graybox_schema import AttackPolicy, GrayBoxCase, LoadedGrayBoxDataset
from app.schemas.run_schema import LoadedDataset, RunBudget
from app.schemas.stateful_schema import (
    CleanupResult,
    LoadedStatefulDataset,
    ReplayDiff,
    RetrievalTrace,
    StatefulCase,
    StatefulEvaluationResult,
    StatefulProfile,
    StateIdentity,
)


class CaseWithId(Protocol):
    id: str


CaseT = TypeVar("CaseT", bound=CaseWithId)


class StatefulRepository:
    """所有状态对象带 Run 和身份作用域，避免测试夹具跨运行泄露。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def create_run(
        self,
        *,
        dataset: LoadedStatefulDataset,
        profile: StatefulProfile,
        target_name: str,
        mode: str = "deterministic_stateful",
    ) -> str:
        run_id = str(uuid4())
        async with self.session_factory.begin() as session:
            target = TargetRecord(
                id=str(uuid4()),
                name=target_name,
                endpoint="stateful://isolated-test-adapters",
                config_json={"name": target_name, "profile": profile.value},
            )
            dataset_record = await session.scalar(
                select(DatasetSnapshotRecord).where(DatasetSnapshotRecord.sha256 == dataset.sha256)
            )
            if dataset_record is None:
                dataset_record = DatasetSnapshotRecord(
                    id=str(uuid4()),
                    name=dataset.name,
                    version=dataset.version,
                    sha256=dataset.sha256,
                    snapshot_json=dataset.snapshot,
                )
                session.add(dataset_record)
            policy = PolicySnapshotRecord(
                id=str(uuid4()),
                config_json={
                    "profile": profile.value,
                    "state_scope": "isolated_test_data",
                    "strict_identity_filter": profile == StatefulProfile.hardened,
                    "evaluator_version": "stateful-v1",
                },
            )
            session.add_all([target, policy])
            await session.flush()
            session.add(
                EvaluationRunRecord(
                    id=run_id,
                    target_id=target.id,
                    dataset_id=dataset_record.id,
                    policy_id=policy.id,
                    thread_id=f"stateful-run:{run_id}",
                    mode=mode,
                    status="running",
                    total_cases=len(dataset.cases),
                )
            )
            await session.flush()
            await self._append_event(
                session,
                run_id=run_id,
                operation_id=f"{run_id}:run_started",
                event_type="run_started",
                evidence={
                    "mode": mode,
                    "profile": profile.value,
                    "dataset_sha256": dataset.sha256,
                    "case_order": [case.id for case in dataset.cases],
                    "evaluator_version": "stateful-v1",
                },
            )
        return run_id

    async def ensure_step(
        self,
        *,
        run_id: str,
        case_id: str,
        operation_id: str,
        sequence: int,
    ) -> RunStepRecord:
        async with self.session_factory.begin() as session:
            existing = await session.scalar(
                select(RunStepRecord).where(RunStepRecord.operation_id == operation_id)
            )
            if existing is not None:
                return existing
            step = RunStepRecord(
                id=str(uuid4()),
                run_id=run_id,
                case_id=case_id,
                operation_id=operation_id,
                sequence=sequence,
                status="running",
                outcome="pending",
                result_json={"case_id": case_id, "status": "running"},
            )
            session.add(step)
            await session.flush()
            return step

    async def write_fixture(
        self,
        *,
        run_id: str,
        operation_id: str,
        scope_id: str,
        kind: str,
        identity: StateIdentity,
        namespace: str,
        resource_id: str,
        content_summary: str,
        provenance: str,
        permissions: dict[str, Any],
        poisoned: bool,
        active: bool,
    ) -> StateFixtureRecord:
        async with self.session_factory.begin() as session:
            existing = await session.scalar(
                select(StateFixtureRecord).where(StateFixtureRecord.operation_id == operation_id)
            )
            if existing is not None:
                return existing
            fixture = StateFixtureRecord(
                id=str(uuid4()),
                run_id=run_id,
                operation_id=operation_id,
                scope_id=scope_id,
                kind=kind,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                session_id=identity.session_id,
                namespace=namespace,
                resource_id=resource_id,
                content_summary=content_summary,
                provenance=provenance,
                permissions_json=permissions,
                poisoned=poisoned,
                active=active,
            )
            session.add(fixture)
            await session.flush()
            await self._append_event(
                session,
                run_id=run_id,
                operation_id=f"{operation_id}:event",
                event_type=f"{kind}_written",
                evidence=self._fixture_dict(fixture),
            )
            return fixture

    async def list_fixtures(
        self,
        *,
        scope_id: str,
        namespace: str,
        kind: str,
    ) -> list[StateFixtureRecord]:
        async with self.session_factory() as session:
            return list(
                (
                    await session.scalars(
                        select(StateFixtureRecord)
                        .where(
                            StateFixtureRecord.scope_id == scope_id,
                            StateFixtureRecord.namespace == namespace,
                            StateFixtureRecord.kind == kind,
                        )
                        .order_by(StateFixtureRecord.created_at, StateFixtureRecord.id)
                    )
                ).all()
            )

    async def record_snapshot(
        self,
        *,
        run_id: str,
        step_id: str,
        operation_id: str,
        case_id: str,
        phase: str,
        identity: StateIdentity,
        items: list[dict[str, Any]],
    ) -> str:
        async with self.session_factory.begin() as session:
            existing = await session.scalar(
                select(StateSnapshotRecord).where(StateSnapshotRecord.operation_id == operation_id)
            )
            if existing is not None:
                event = await session.scalar(
                    select(EventRecord).where(EventRecord.operation_id == f"{operation_id}:event")
                )
                if event is None:
                    raise LookupError(f"snapshot event {operation_id} not found")
                return event.id
            snapshot = StateSnapshotRecord(
                id=str(uuid4()),
                run_id=run_id,
                step_id=step_id,
                operation_id=operation_id,
                case_id=case_id,
                phase=phase,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                session_id=identity.session_id,
                items_json=items,
            )
            session.add(snapshot)
            await session.flush()
            return await self._append_event(
                session,
                run_id=run_id,
                step_id=step_id,
                operation_id=f"{operation_id}:event",
                event_type="state_snapshot",
                evidence={
                    "snapshot_id": snapshot.id,
                    "case_id": case_id,
                    "phase": phase,
                    "identity": identity.model_dump(mode="json"),
                    "items": items,
                },
            )

    async def record_retrieval(
        self,
        *,
        run_id: str,
        step_id: str,
        operation_id: str,
        case_id: str,
        identity: StateIdentity,
        trace: RetrievalTrace,
    ) -> str:
        async with self.session_factory.begin() as session:
            existing = await session.scalar(
                select(RetrievalEventRecord).where(
                    RetrievalEventRecord.operation_id == operation_id
                )
            )
            if existing is not None:
                event = await session.scalar(
                    select(EventRecord).where(EventRecord.operation_id == f"{operation_id}:event")
                )
                if event is None:
                    raise LookupError(f"retrieval event {operation_id} not found")
                return event.id
            retrieval = RetrievalEventRecord(
                id=str(uuid4()),
                run_id=run_id,
                step_id=step_id,
                operation_id=operation_id,
                case_id=case_id,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                session_id=identity.session_id,
                query_summary=trace.query_summary,
                documents_json=[document.model_dump(mode="json") for document in trace.documents],
                permission_filter_json=trace.permission_filter,
            )
            session.add(retrieval)
            await session.flush()
            return await self._append_event(
                session,
                run_id=run_id,
                step_id=step_id,
                operation_id=f"{operation_id}:event",
                event_type="rag_retrieved",
                evidence={
                    "retrieval_id": retrieval.id,
                    "case_id": case_id,
                    "trace": trace.model_dump(mode="json"),
                },
            )

    async def record_recovery(
        self,
        *,
        run_id: str,
        step_id: str,
        operation_id: str,
        case_id: str,
        evidence: dict[str, Any],
    ) -> str:
        return await self.append_event(
            run_id=run_id,
            step_id=step_id,
            operation_id=operation_id,
            event_type="recovery_policy_revalidated",
            evidence={"case_id": case_id, **evidence},
        )

    async def cleanup(self, *, run_id: str, scope_id: str, operation_id: str) -> CleanupResult:
        """只停用指定 Run/scope 夹具，并记录范围外对象未受影响的证据。"""

        async with self.session_factory.begin() as session:
            existing = await session.scalar(
                select(EventRecord).where(EventRecord.operation_id == operation_id)
            )
            if existing is not None:
                return CleanupResult.model_validate(existing.evidence_json["cleanup"])
            outside_before = await session.scalar(
                select(func.count())
                .select_from(StateFixtureRecord)
                .where(
                    StateFixtureRecord.active.is_(True),
                    ~(
                        (StateFixtureRecord.run_id == run_id)
                        & (StateFixtureRecord.scope_id == scope_id)
                    ),
                )
            )
            fixtures = (
                await session.scalars(
                    select(StateFixtureRecord).where(
                        StateFixtureRecord.run_id == run_id,
                        StateFixtureRecord.scope_id == scope_id,
                        StateFixtureRecord.cleaned_at.is_(None),
                    )
                )
            ).all()
            now = datetime.now(UTC)
            for fixture in fixtures:
                fixture.active = False
                fixture.cleaned_at = now
            await session.flush()
            remaining = await session.scalar(
                select(func.count())
                .select_from(StateFixtureRecord)
                .where(
                    StateFixtureRecord.run_id == run_id,
                    StateFixtureRecord.scope_id == scope_id,
                    StateFixtureRecord.active.is_(True),
                )
            )
            outside_after = await session.scalar(
                select(func.count())
                .select_from(StateFixtureRecord)
                .where(
                    StateFixtureRecord.active.is_(True),
                    ~(
                        (StateFixtureRecord.run_id == run_id)
                        & (StateFixtureRecord.scope_id == scope_id)
                    ),
                )
            )
            result = CleanupResult(
                scope_id=scope_id,
                cleaned_count=len(fixtures),
                remaining_active_count=int(remaining or 0),
                outside_scope_affected_count=max(
                    int(outside_before or 0) - int(outside_after or 0),
                    0,
                ),
            )
            await self._append_event(
                session,
                run_id=run_id,
                operation_id=operation_id,
                event_type="state_cleanup_completed",
                evidence={"cleanup": result.model_dump(mode="json")},
            )
            return result

    async def append_event(
        self,
        *,
        run_id: str,
        operation_id: str,
        event_type: str,
        evidence: dict[str, Any],
        step_id: str | None = None,
    ) -> str:
        async with self.session_factory.begin() as session:
            return await self._append_event(
                session,
                run_id=run_id,
                operation_id=operation_id,
                event_type=event_type,
                evidence=evidence,
                step_id=step_id,
            )

    async def complete_case(
        self,
        *,
        run_id: str,
        operation_id: str,
        case: StatefulCase,
        profile: StatefulProfile,
        evaluation: StatefulEvaluationResult,
        cleanup: CleanupResult,
        evidence_event_ids: list[str],
        fingerprint: str,
    ) -> None:
        async with self.session_factory.begin() as session:
            step = await session.scalar(
                select(RunStepRecord).where(RunStepRecord.operation_id == operation_id)
            )
            if step is None:
                raise LookupError(f"step {operation_id} not found")
            if step.status == "completed":
                return
            evaluation_event_id = await self._append_event(
                session,
                run_id=run_id,
                step_id=step.id,
                operation_id=f"{operation_id}:evaluation",
                event_type="evaluation_completed",
                evidence={
                    "case_id": case.id,
                    "evaluation": evaluation.model_dump(mode="json"),
                },
            )
            cleanup_event = await session.scalar(
                select(EventRecord).where(EventRecord.operation_id == f"{operation_id}:cleanup")
            )
            linked_ids = [*evidence_event_ids, evaluation_event_id]
            if cleanup_event is not None:
                linked_ids.append(cleanup_event.id)
            step.status = "completed"
            step.outcome = evaluation.outcome
            step.completed_at = datetime.now(UTC)
            step.result_json = {
                "case": case.model_dump(mode="json"),
                "profile": profile.value,
                "evaluation": evaluation.model_dump(mode="json"),
                "cleanup": cleanup.model_dump(mode="json"),
            }
            if evaluation.violated:
                finding_id = str(uuid4())
                session.add(
                    FindingRecord(
                        id=finding_id,
                        run_id=run_id,
                        step_id=step.id,
                        event_id=evaluation_event_id,
                        operation_id=f"{operation_id}:finding",
                        case_id=case.id,
                        category=case.category,
                        risk_level=evaluation.risk_level.value,
                        outcome=evaluation.outcome,
                        reason=evaluation.reason,
                        fingerprint=fingerprint,
                        evidence_event_ids=linked_ids,
                        is_control=case.kind.value == "control",
                    )
                )
                await self._append_event(
                    session,
                    run_id=run_id,
                    step_id=step.id,
                    operation_id=f"{operation_id}:finding_event",
                    event_type="finding_created",
                    evidence={
                        "finding_id": finding_id,
                        "case_id": case.id,
                        "fingerprint": fingerprint,
                        "evidence_event_ids": linked_ids,
                    },
                )

    async def finalize_run(
        self,
        run_id: str,
        terminal_reason: str,
        *,
        status: Literal["completed", "failed", "cancelled"] = "completed",
        cleanup_failures: list[dict[str, str | int]] | None = None,
    ) -> None:
        async with self.session_factory.begin() as session:
            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            if run.status in {"completed", "failed", "cancelled"}:
                return
            steps = (
                await session.scalars(select(RunStepRecord).where(RunStepRecord.run_id == run_id))
            ).all()
            outcomes = Counter(step.outcome for step in steps)
            run.status = status
            run.terminal_reason = terminal_reason
            run.completed_at = datetime.now(UTC)
            run.completed_cases = sum(step.outcome != "pending" for step in steps)
            run.violation_count = outcomes.get("violation", 0)
            run.safe_count = outcomes.get("safe", 0)
            run.error_count = outcomes.get("error", 0) + outcomes.get("inconclusive", 0)
            await self._append_event(
                session,
                run_id=run_id,
                operation_id=f"{run_id}:run_terminal",
                event_type=f"run_{status}",
                evidence={
                    "terminal_reason": terminal_reason,
                    "status": status,
                    "outcomes": dict(outcomes),
                    "cleanup_incomplete": bool(cleanup_failures),
                    "cleanup_failures": cleanup_failures or [],
                },
            )

    async def get_run(self, run_id: str) -> EvaluationRunRecord:
        async with self.session_factory() as session:
            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            return run

    async def load_dataset(self, run_id: str) -> LoadedStatefulDataset:
        from app.schemas.stateful_schema import StatefulCase

        async with self.session_factory() as session:
            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            dataset = await session.get(DatasetSnapshotRecord, run.dataset_id)
            if dataset is None:
                raise LookupError(f"dataset for run {run_id} not found")
            case_order = await self._load_case_order(session, run_id)
            raw_cases = dataset.snapshot_json.get("cases", [])
            cases = [StatefulCase.model_validate(raw_case["inputs"]) for raw_case in raw_cases]
            cases = self._restore_case_order(cases, case_order, run_id=run_id)
            return LoadedStatefulDataset(
                name=dataset.name,
                version=dataset.version,
                source_path=Path("<persisted-stateful-dataset>"),
                sha256=dataset.sha256,
                cases=cases,
                snapshot=dataset.snapshot_json,
            )

    async def load_blackbox_replay_inputs(
        self,
        run_id: str,
    ) -> tuple[LoadedDataset, RunBudget]:
        async with self.session_factory() as session:
            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            dataset = await session.get(DatasetSnapshotRecord, run.dataset_id)
            policy = await session.get(PolicySnapshotRecord, run.policy_id)
            if dataset is None or policy is None:
                raise LookupError(f"replay inputs for run {run_id} not found")
            case_order = await self._load_case_order(session, run_id)
            cases = [
                BlackBoxCase.model_validate(raw_case["inputs"])
                for raw_case in dataset.snapshot_json.get("cases", [])
            ]
            cases = self._restore_case_order(cases, case_order, run_id=run_id)
            return (
                LoadedDataset(
                    name=dataset.name,
                    version=dataset.version,
                    source_path=Path("<persisted-blackbox-dataset>"),
                    sha256=dataset.sha256,
                    cases=cases,
                    snapshot=dataset.snapshot_json,
                ),
                RunBudget.model_validate(policy.config_json),
            )

    async def load_graybox_replay_inputs(
        self,
        run_id: str,
    ) -> tuple[LoadedGrayBoxDataset, AttackPolicy]:
        async with self.session_factory() as session:
            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            dataset = await session.get(DatasetSnapshotRecord, run.dataset_id)
            policy = await session.get(PolicySnapshotRecord, run.policy_id)
            if dataset is None or policy is None:
                raise LookupError(f"replay inputs for run {run_id} not found")
            case_order = await self._load_case_order(session, run_id)
            cases = [
                GrayBoxCase.model_validate(raw_case["inputs"])
                for raw_case in dataset.snapshot_json.get("cases", [])
            ]
            cases = self._restore_case_order(cases, case_order, run_id=run_id)
            return (
                LoadedGrayBoxDataset(
                    name=dataset.name,
                    version=dataset.version,
                    source_path=Path("<persisted-graybox-dataset>"),
                    sha256=dataset.sha256,
                    cases=cases,
                    snapshot=dataset.snapshot_json,
                ),
                AttackPolicy.model_validate(policy.config_json),
            )

    async def finding_rows(self, run_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            findings = (
                await session.scalars(select(FindingRecord).where(FindingRecord.run_id == run_id))
            ).all()
            return [
                {
                    "case_id": finding.case_id,
                    "fingerprint": finding.fingerprint,
                    "is_control": finding.is_control,
                }
                for finding in findings
            ]

    async def create_replay(
        self,
        *,
        source_run_id: str,
        replay_run_id: str,
        diff: ReplayDiff,
    ) -> dict[str, Any]:
        async with self.session_factory.begin() as session:
            existing = await session.scalar(
                select(ReplayRecord).where(ReplayRecord.replay_run_id == replay_run_id)
            )
            if existing is None:
                existing = ReplayRecord(
                    id=str(uuid4()),
                    source_run_id=source_run_id,
                    replay_run_id=replay_run_id,
                    status="completed",
                    diff_json=diff.model_dump(mode="json"),
                )
                session.add(existing)
                await session.flush()
            return self._replay_dict(existing)

    async def list_replays(self, run_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            replays = (
                await session.scalars(
                    select(ReplayRecord)
                    .where(
                        (ReplayRecord.replay_run_id == run_id)
                        | (ReplayRecord.source_run_id == run_id)
                    )
                    .order_by(ReplayRecord.created_at)
                )
            ).all()
            return [self._replay_dict(replay) for replay in replays]

    async def _load_case_order(
        self,
        session: AsyncSession,
        run_id: str,
    ) -> list[str]:
        event = await session.scalar(
            select(EventRecord).where(
                EventRecord.run_id == run_id,
                EventRecord.event_type == "run_started",
            )
        )
        if event is None or "case_order" not in event.evidence_json:
            raise ValueError(f"persisted case order for run {run_id} is missing")
        raw_order = event.evidence_json["case_order"]
        if not isinstance(raw_order, list) or any(not isinstance(item, str) for item in raw_order):
            raise ValueError(f"persisted case order for run {run_id} is invalid")
        if len(raw_order) != len(set(raw_order)):
            raise ValueError(f"persisted case order for run {run_id} contains duplicates")
        return raw_order

    @staticmethod
    def _restore_case_order(
        cases: list[CaseT],
        case_order: list[str],
        *,
        run_id: str,
    ) -> list[CaseT]:
        cases_by_id = {str(case.id): case for case in cases}
        if len(cases_by_id) != len(cases):
            raise ValueError(f"persisted dataset for run {run_id} contains duplicate case IDs")
        unknown = set(case_order) - cases_by_id.keys()
        if unknown:
            raise ValueError(
                f"persisted case order for run {run_id} contains unknown cases: "
                f"{', '.join(sorted(unknown))}"
            )
        return [cases_by_id[case_id] for case_id in case_order]

    async def _append_event(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        operation_id: str,
        event_type: str,
        evidence: dict[str, Any],
        step_id: str | None = None,
    ) -> str:
        existing = await session.scalar(
            select(EventRecord).where(EventRecord.operation_id == operation_id)
        )
        if existing is not None:
            return existing.id
        sequence = await session.scalar(
            select(func.max(EventRecord.sequence)).where(EventRecord.run_id == run_id)
        )
        event = EventRecord(
            id=str(uuid4()),
            run_id=run_id,
            step_id=step_id,
            sequence=int(sequence or 0) + 1,
            operation_id=operation_id,
            event_type=event_type,
            evidence_json=evidence,
        )
        session.add(event)
        await session.flush()
        return event.id

    @staticmethod
    def _fixture_dict(fixture: StateFixtureRecord) -> dict[str, Any]:
        return {
            "fixture_id": fixture.id,
            "scope_id": fixture.scope_id,
            "kind": fixture.kind,
            "tenant_id": fixture.tenant_id,
            "user_id": fixture.user_id,
            "session_id": fixture.session_id,
            "namespace": fixture.namespace,
            "resource_id": fixture.resource_id,
            "content_summary": fixture.content_summary,
            "provenance": fixture.provenance,
            "permissions": fixture.permissions_json,
            "poisoned": fixture.poisoned,
            "active": fixture.active,
        }

    @staticmethod
    def _replay_dict(replay: ReplayRecord) -> dict[str, Any]:
        return {
            "id": replay.id,
            "source_run_id": replay.source_run_id,
            "replay_run_id": replay.replay_run_id,
            "status": replay.status,
            "diff": replay.diff_json,
            "created_at": replay.created_at.isoformat(),
        }
