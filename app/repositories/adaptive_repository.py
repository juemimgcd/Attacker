from collections import Counter
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
from app.schemas.adaptive_agent_schema import (
    CandidateSnapshot,
    CoverageFact,
    DerivedCase,
    HypothesisFact,
    HypothesisStatus,
    InformationGainMetrics,
    ObservationSource,
    UntrustedObservation,
)
from app.schemas.attack_state_schema import CoverageStatus
from app.schemas.graybox_schema import (
    AttackPolicy,
    GrayBoxCase,
    GrayBoxEvaluationResult,
    GrayBoxExecutionResult,
    GrayBoxOutcome,
    LoadedGrayBoxDataset,
    PlannerResult,
    PolicyGateResult,
    TraceAdapterResult,
)
from app.schemas.judge_schema import TargetResponse
from app.services.finding_fingerprint import finding_fingerprint


class AdaptiveRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def create_run(
        self,
        *,
        target_snapshot: dict[str, Any],
        dataset: LoadedGrayBoxDataset,
        policy: AttackPolicy,
        mode: str,
        baseline_run_id: str | None,
        planner_snapshot: dict[str, Any] | None = None,
    ) -> tuple[str, str, str, AttackPolicy]:
        run_id = str(uuid4())
        target_id = str(uuid4())
        thread_id = f"attack-run:{run_id}"
        effective_policy = policy.model_copy(
            update={
                "allowed_target_ids": {target_id},
                "allowed_case_ids": {case.id for case in dataset.cases},
                "allowed_capability_contracts": {
                    case.capability_contract for case in dataset.cases
                },
                "allowed_provider_instance_refs": {
                    case.provider_instance_ref for case in dataset.cases
                },
            }
        )
        async with self.session_factory.begin() as session:
            if baseline_run_id is not None:
                baseline = await session.get(EvaluationRunRecord, baseline_run_id)
                if baseline is None:
                    raise ValueError(f"baseline run {baseline_run_id} not found")

            target = TargetRecord(
                id=target_id,
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
            policy_record = PolicySnapshotRecord(
                id=str(uuid4()),
                config_json=effective_policy.model_dump(mode="json"),
            )
            session.add(target)
            if existing_dataset is None:
                session.add(dataset_record)
            session.add(policy_record)
            await session.flush()
            run = EvaluationRunRecord(
                id=run_id,
                target_id=target_id,
                dataset_id=dataset_record.id,
                policy_id=policy_record.id,
                baseline_run_id=baseline_run_id,
                thread_id=thread_id,
                mode=mode,
                status="running",
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
                        "mode": mode,
                        "thread_id": thread_id,
                        "dataset_sha256": dataset.sha256,
                        "case_order": [case.id for case in dataset.cases],
                        "policy": effective_policy.model_dump(mode="json"),
                        "baseline_run_id": baseline_run_id,
                        "planner": planner_snapshot,
                    },
                )
            )
        return run_id, target_id, thread_id, effective_policy

    async def load_runtime_snapshot(self, run_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            target = await session.get(TargetRecord, run.target_id)
            dataset = await session.get(DatasetSnapshotRecord, run.dataset_id)
            policy = await session.get(PolicySnapshotRecord, run.policy_id)
            started = await session.scalar(
                select(EventRecord).where(EventRecord.operation_id == f"{run_id}:run_started")
            )
            if target is None or dataset is None or policy is None or started is None:
                raise LookupError(f"runtime snapshot for run {run_id} is incomplete")
            return {
                "run_id": run.id,
                "target_id": run.target_id,
                "thread_id": run.thread_id,
                "started_at": run.started_at,
                "target": target.config_json,
                "dataset": dataset.snapshot_json,
                "policy": policy.config_json,
                "planner": started.evidence_json.get("planner"),
            }

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
            existing = await session.scalar(
                select(EventRecord).where(EventRecord.operation_id == operation_id)
            )
            if existing is not None:
                return existing.id
            return await self._append_event(
                session,
                run_id=run_id,
                operation_id=operation_id,
                event_type=event_type,
                evidence=evidence,
                step_id=step_id,
            )

    async def initialize_hypotheses(
        self,
        *,
        run_id: str,
        cases: list[GrayBoxCase],
    ) -> dict[str, HypothesisFact]:
        hypotheses: dict[str, HypothesisFact] = {}
        for case in sorted(cases, key=lambda item: item.id):
            template_id = case.hypothesis_template_id or f"{case.category}.v1"
            event_id = await self.append_event(
                run_id=run_id,
                operation_id=f"{run_id}:hypothesis:{case.id}:initial",
                event_type="hypothesis_created",
                evidence={
                    "action_id": case.id,
                    "template_id": template_id,
                    "status": "pending",
                    "evidence_refs": [],
                },
            )
            hypotheses[case.id] = HypothesisFact(
                hypothesis_ref=event_id,
                template_id=template_id,
                action_id=case.id,
                status=HypothesisStatus.pending,
            )
            await self.append_event(
                run_id=run_id,
                operation_id=f"{run_id}:observation:case_pack:{case.id}",
                event_type="observation_normalized",
                evidence={
                    "source": ObservationSource.case_pack.value,
                    "trust": "untrusted",
                    "summary": (
                        f"case_id={case.id}; hypothesis_template_id={template_id}; "
                        f"coverage_tags={','.join(case.coverage_tags or [case.category])}"
                    )[:2_000],
                },
            )
        return hypotheses

    async def record_candidate_snapshot(self, snapshot: CandidateSnapshot) -> str:
        return await self.append_event(
            run_id=snapshot.run_id,
            operation_id=f"{snapshot.run_id}:candidate_snapshot:{snapshot.snapshot_id}",
            event_type="candidate_snapshot_created",
            evidence={"snapshot": snapshot.model_dump(mode="json")},
        )

    async def load_candidate_snapshot(
        self,
        *,
        run_id: str,
        snapshot_id: str,
    ) -> CandidateSnapshot:
        async with self.session_factory() as session:
            events = (
                await session.scalars(
                    select(EventRecord)
                    .where(
                        EventRecord.run_id == run_id,
                        EventRecord.event_type == "candidate_snapshot_created",
                    )
                    .order_by(EventRecord.sequence.desc())
                )
            ).all()
            for event in events:
                raw_snapshot = event.evidence_json.get("snapshot", {})
                if raw_snapshot.get("snapshot_id") == snapshot_id:
                    return CandidateSnapshot.model_validate(raw_snapshot)
        raise LookupError(f"candidate snapshot {snapshot_id} not found for run {run_id}")

    async def record_observation(
        self,
        *,
        run_id: str,
        operation_id: str,
        source: ObservationSource,
        summary: str,
        step_id: str | None,
    ) -> UntrustedObservation:
        event_id = await self.append_event(
            run_id=run_id,
            operation_id=operation_id,
            event_type="observation_normalized",
            step_id=step_id,
            evidence={
                "source": source.value,
                "trust": "untrusted",
                "summary": summary,
            },
        )
        return UntrustedObservation(
            observation_ref=event_id,
            source=source,
            summary=summary,
        )

    async def record_hypothesis_transition(
        self,
        *,
        run_id: str,
        operation_id: str,
        hypothesis: HypothesisFact,
        step_id: str | None,
    ) -> HypothesisFact:
        event_id = await self.append_event(
            run_id=run_id,
            operation_id=operation_id,
            event_type="hypothesis_updated",
            step_id=step_id,
            evidence={
                "previous_hypothesis_ref": hypothesis.hypothesis_ref,
                "action_id": hypothesis.action_id,
                "template_id": hypothesis.template_id,
                "status": hypothesis.status.value,
                "evidence_refs": list(hypothesis.evidence_refs),
            },
        )
        return hypothesis.model_copy(update={"hypothesis_ref": event_id})

    async def record_coverage_and_gain(
        self,
        *,
        run_id: str,
        operation_id: str,
        coverage_facts: tuple[CoverageFact, ...],
        gain: InformationGainMetrics,
        step_id: str | None,
    ) -> tuple[list[str], str]:
        coverage_refs: list[str] = []
        for fact in coverage_facts:
            coverage_refs.append(
                await self.append_event(
                    run_id=run_id,
                    operation_id=f"{operation_id}:coverage:{fact.tag}",
                    event_type="coverage_updated",
                    step_id=step_id,
                    evidence=fact.model_dump(mode="json"),
                )
            )
        gain_ref = await self.append_event(
            run_id=run_id,
            operation_id=f"{operation_id}:information_gain",
            event_type="information_gain_measured",
            step_id=step_id,
            evidence=gain.model_dump(mode="json"),
        )
        return coverage_refs, gain_ref

    async def record_finish_rejected(
        self,
        *,
        run_id: str,
        operation_id: str,
        reason_code: str,
        detail: str,
    ) -> str:
        return await self.append_event(
            run_id=run_id,
            operation_id=operation_id,
            event_type="planner_finish_rejected",
            evidence={"reason_code": reason_code, "detail": detail},
        )

    async def record_derived_case(
        self,
        *,
        run_id: str,
        derived_case: DerivedCase,
    ) -> str:
        event_type = (
            "derived_case_verified"
            if derived_case.deterministic_verified
            else "derived_case_frozen"
        )
        return await self.append_event(
            run_id=run_id,
            operation_id=(f"{run_id}:derived_case:{derived_case.derived_case_id}:{event_type}"),
            event_type=event_type,
            evidence={"derived_case": derived_case.model_dump(mode="json")},
        )

    async def load_adaptive_facts(self, run_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            events = (
                await session.scalars(
                    select(EventRecord)
                    .where(EventRecord.run_id == run_id)
                    .order_by(EventRecord.sequence)
                )
            ).all()
            findings = (
                await session.scalars(
                    select(FindingRecord)
                    .where(FindingRecord.run_id == run_id)
                    .order_by(FindingRecord.created_at, FindingRecord.id)
                )
            ).all()

        hypotheses: dict[str, HypothesisFact] = {}
        coverage: dict[str, CoverageStatus] = {}
        observations: list[UntrustedObservation] = []
        information_gain_refs: list[str] = []
        for event in events:
            if event.event_type in {"hypothesis_created", "hypothesis_updated"}:
                evidence = event.evidence_json
                action_id = str(evidence["action_id"])
                hypotheses[action_id] = HypothesisFact(
                    hypothesis_ref=event.id,
                    template_id=str(evidence["template_id"]),
                    action_id=action_id,
                    status=HypothesisStatus(str(evidence["status"])),
                    evidence_refs=tuple(evidence.get("evidence_refs", [])),
                )
            elif event.event_type == "coverage_updated":
                coverage[str(event.evidence_json["tag"])] = CoverageStatus(
                    str(event.evidence_json["status"])
                )
            elif event.event_type == "observation_normalized":
                observations.append(
                    UntrustedObservation(
                        observation_ref=event.id,
                        source=ObservationSource(str(event.evidence_json["source"])),
                        summary=str(event.evidence_json["summary"]),
                    )
                )
            elif event.event_type == "information_gain_measured":
                information_gain_refs.append(event.id)

        evidence_gaps = {
            tag for tag, status in coverage.items() if status == CoverageStatus.inconclusive
        }
        return {
            "hypotheses": hypotheses,
            "coverage": coverage,
            "observations": observations[-20:],
            "observation_refs": [item.observation_ref for item in observations],
            "finding_refs": [finding.id for finding in findings],
            "evidence_gaps": evidence_gaps,
            "information_gain_refs": information_gain_refs,
        }

    async def validate_planner_references(
        self,
        *,
        run_id: str,
        evidence_refs: list[str],
        hypothesis_refs: list[str],
    ) -> str | None:
        async with self.session_factory() as session:
            event_rows = (
                await session.scalars(
                    select(EventRecord).where(
                        EventRecord.run_id == run_id,
                        EventRecord.id.in_([*evidence_refs, *hypothesis_refs]),
                    )
                )
            ).all()
            finding_rows = (
                await session.scalars(
                    select(FindingRecord).where(
                        FindingRecord.run_id == run_id,
                        FindingRecord.id.in_(evidence_refs),
                    )
                )
            ).all()
        events_by_id = {event.id: event for event in event_rows}
        persisted_evidence_refs = events_by_id.keys() | {finding.id for finding in finding_rows}
        missing_evidence = sorted(set(evidence_refs) - persisted_evidence_refs)
        if missing_evidence:
            return f"planner referenced non-persisted evidence: {', '.join(missing_evidence)}"
        observation_refs = {
            event.id for event in event_rows if event.event_type == "observation_normalized"
        }
        finding_refs = {finding.id for finding in finding_rows}
        if not set(evidence_refs) & (observation_refs | finding_refs):
            return "planner proposal must cite a persisted observation or finding"
        missing_hypotheses = sorted(set(hypothesis_refs) - events_by_id.keys())
        if missing_hypotheses:
            return f"planner referenced non-persisted hypotheses: {', '.join(missing_hypotheses)}"
        invalid_hypotheses = sorted(
            ref
            for ref in hypothesis_refs
            if events_by_id[ref].event_type not in {"hypothesis_created", "hypothesis_updated"}
        )
        if invalid_hypotheses:
            return f"planner referenced non-hypothesis facts: {', '.join(invalid_hypotheses)}"
        return None

    async def record_planner_result(
        self,
        *,
        run_id: str,
        operation_id: str,
        result: PlannerResult,
        accepted: bool,
        rejection_reason: str | None = None,
    ) -> str:
        async with self.session_factory.begin() as session:
            existing = await session.scalar(
                select(EventRecord).where(EventRecord.operation_id == operation_id)
            )
            if existing is not None:
                return existing.id
            event_id = await self._append_event(
                session,
                run_id=run_id,
                operation_id=operation_id,
                event_type="planner_decided" if accepted else "planner_rejected",
                evidence={
                    "decision": result.decision.model_dump(mode="json"),
                    "backend": result.backend,
                    "usage": result.usage.model_dump(mode="json"),
                    "call_snapshot": result.call_snapshot.model_dump(mode="json"),
                    "rejection_reason": rejection_reason,
                },
            )
            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            run.planner_call_count += 1
            run.planner_token_count += result.usage.input_tokens + result.usage.output_tokens
            return event_id

    async def record_planner_error(
        self,
        *,
        run_id: str,
        operation_id: str,
        error_type: str,
        message: str,
    ) -> str:
        async with self.session_factory.begin() as session:
            existing = await session.scalar(
                select(EventRecord).where(EventRecord.operation_id == operation_id)
            )
            if existing is not None:
                return existing.id
            event_id = await self._append_event(
                session,
                run_id=run_id,
                operation_id=operation_id,
                event_type="planner_error",
                evidence={"error_type": error_type, "message": message},
            )
            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            run.planner_call_count += 1
            return event_id

    async def record_policy_result(
        self,
        *,
        run_id: str,
        case_id: str,
        operation_id: str,
        result: PolicyGateResult,
    ) -> str:
        async with self.session_factory.begin() as session:
            existing = await session.scalar(
                select(EventRecord).where(EventRecord.operation_id == operation_id)
            )
            if existing is not None:
                return existing.id
            event_id = await self._append_event(
                session,
                run_id=run_id,
                operation_id=operation_id,
                event_type=f"policy_{result.decision.value}",
                evidence={
                    "case_id": case_id,
                    "decision": result.decision.value,
                    "reason": result.reason,
                    "approval_id": result.approval_id,
                },
            )
            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            if result.decision.value == "deny":
                run.policy_denied_count += 1
            elif result.decision.value == "approval_required":
                run.approval_required_count += 1
            return event_id

    async def ensure_approval(
        self,
        *,
        run_id: str,
        case: GrayBoxCase,
        operation_id: str,
    ) -> ApprovalRecord:
        async with self.session_factory.begin() as session:
            existing = await session.scalar(
                select(ApprovalRecord).where(
                    ApprovalRecord.run_id == run_id,
                    ApprovalRecord.case_id == case.id,
                )
            )
            if existing is not None:
                return existing
            approval = ApprovalRecord(
                id=str(uuid4()),
                run_id=run_id,
                case_id=case.id,
                operation_id=operation_id,
                risk_summary=f"{case.severity.value}: {case.expected_violation}",
            )
            session.add(approval)
            await session.flush()
            await self._append_event(
                session,
                run_id=run_id,
                operation_id=f"{operation_id}:requested",
                event_type="approval_requested",
                evidence={
                    "approval_id": approval.id,
                    "case_id": case.id,
                    "risk_summary": approval.risk_summary,
                },
            )
            return approval

    async def get_approval(
        self,
        *,
        run_id: str,
        case_id: str | None = None,
        approval_id: str | None = None,
    ) -> ApprovalRecord | None:
        async with self.session_factory() as session:
            statement = select(ApprovalRecord).where(ApprovalRecord.run_id == run_id)
            if case_id is not None:
                statement = statement.where(ApprovalRecord.case_id == case_id)
            if approval_id is not None:
                statement = statement.where(ApprovalRecord.id == approval_id)
            return await session.scalar(statement)

    async def list_approvals(self, run_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            approvals = (
                await session.scalars(
                    select(ApprovalRecord)
                    .where(ApprovalRecord.run_id == run_id)
                    .order_by(ApprovalRecord.requested_at)
                )
            ).all()
            return [self._approval_dict(approval) for approval in approvals]

    async def resolve_approval(
        self,
        *,
        run_id: str,
        approval_id: str,
        approved: bool,
        resolved_by: str,
        reason: str,
    ) -> dict[str, Any]:
        async with self.session_factory.begin() as session:
            approval = await session.scalar(
                select(ApprovalRecord).where(
                    ApprovalRecord.id == approval_id,
                    ApprovalRecord.run_id == run_id,
                )
            )
            if approval is None:
                raise LookupError(f"approval {approval_id} not found")
            desired_status = "approved" if approved else "rejected"
            if approval.status != "pending":
                if approval.status != desired_status:
                    raise ValueError(f"approval already resolved as {approval.status}")
                return self._approval_dict(approval)

            approval.status = desired_status
            approval.resolved_at = datetime.now(UTC)
            approval.resolved_by = resolved_by
            approval.reason = reason
            await self._append_event(
                session,
                run_id=run_id,
                operation_id=f"{approval.operation_id}:resolved",
                event_type="approval_resolved",
                evidence=self._approval_dict(approval),
            )
            return self._approval_dict(approval)

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

    async def get_step(self, operation_id: str) -> RunStepRecord | None:
        async with self.session_factory() as session:
            return await session.scalar(
                select(RunStepRecord).where(RunStepRecord.operation_id == operation_id)
            )

    async def record_target_execution(
        self,
        *,
        run_id: str,
        step_id: str,
        operation_id: str,
        request_body: dict[str, Any],
        response: TargetResponse,
        trace_result: TraceAdapterResult,
    ) -> list[str]:
        target_operation = f"{operation_id}:target"
        async with self.session_factory.begin() as session:
            existing = await session.scalar(
                select(EventRecord).where(EventRecord.operation_id == target_operation)
            )
            if existing is not None:
                return await self._execution_evidence_ids(session, operation_id)

            event_ids = [
                await self._append_event(
                    session,
                    run_id=run_id,
                    step_id=step_id,
                    operation_id=target_operation,
                    event_type="target_called",
                    evidence={
                        "request_body": request_body,
                        "response": response.model_dump(mode="json"),
                        "trace_errors": trace_result.errors,
                        "trace_evidence_complete": trace_result.evidence_complete,
                    },
                )
            ]
            for index, policy_event in enumerate(
                trace_result.trace.policy_events,
                start=1,
            ):
                event_ids.append(
                    await self._append_event(
                        session,
                        run_id=run_id,
                        step_id=step_id,
                        operation_id=f"{operation_id}:target_policy:{index}",
                        event_type="target_policy_observed",
                        evidence=policy_event.model_dump(mode="json"),
                    )
                )
            for index, tool_event in enumerate(
                trace_result.trace.tool_events,
                start=1,
            ):
                event_ids.append(
                    await self._append_event(
                        session,
                        run_id=run_id,
                        step_id=step_id,
                        operation_id=f"{operation_id}:tool_requested:{index}",
                        event_type="tool_requested",
                        evidence={
                            "tool_call_id": tool_event.tool_call_id,
                            "tool_name": tool_event.tool_name,
                            "caller_identity": tool_event.caller_identity,
                            "arguments_summary": tool_event.arguments_summary,
                            "policy_decision": tool_event.policy_decision.value,
                            "sequence": tool_event.sequence,
                        },
                    )
                )
                event_ids.append(
                    await self._append_event(
                        session,
                        run_id=run_id,
                        step_id=step_id,
                        operation_id=f"{operation_id}:tool_completed:{index}",
                        event_type="tool_completed",
                        evidence={
                            "tool_call_id": tool_event.tool_call_id,
                            "executed": tool_event.executed,
                            "result_summary": tool_event.result_summary,
                            "error": tool_event.error,
                            "output_trust": tool_event.output_trust,
                        },
                    )
                )
            for index, approval_event in enumerate(
                trace_result.trace.approval_events,
                start=1,
            ):
                event_ids.append(
                    await self._append_event(
                        session,
                        run_id=run_id,
                        step_id=step_id,
                        operation_id=f"{operation_id}:target_approval:{index}",
                        event_type="target_approval_observed",
                        evidence=approval_event.model_dump(mode="json"),
                    )
                )

            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            run.target_call_count += 1
            run.tool_call_count += len(trace_result.trace.tool_events)
            return event_ids

    async def load_target_execution(
        self,
        operation_id: str,
    ) -> tuple[dict[str, Any], TargetResponse, TraceAdapterResult]:
        async with self.session_factory() as session:
            target_event = await session.scalar(
                select(EventRecord).where(EventRecord.operation_id == f"{operation_id}:target")
            )
            if target_event is None:
                raise LookupError(f"target execution {operation_id} not found")
            evidence = target_event.evidence_json
            trace = await self._load_trace(session, operation_id)
            trace_errors = list(evidence.get("trace_errors", []))
            return (
                dict(evidence["request_body"]),
                TargetResponse.model_validate(evidence["response"]),
                TraceAdapterResult(
                    trace=trace,
                    errors=trace_errors,
                    evidence_complete=bool(evidence.get("trace_evidence_complete", False)),
                ),
            )

    async def record_evaluation(
        self,
        *,
        run_id: str,
        step_id: str,
        operation_id: str,
        case_id: str,
        evaluation: GrayBoxEvaluationResult,
    ) -> str:
        return await self.append_event(
            run_id=run_id,
            step_id=step_id,
            operation_id=f"{operation_id}:evaluation",
            event_type="evaluation_completed",
            evidence={
                "case_id": case_id,
                "evaluation": evaluation.model_dump(mode="json"),
            },
        )

    async def load_evaluation(
        self,
        operation_id: str,
    ) -> GrayBoxEvaluationResult:
        async with self.session_factory() as session:
            event = await session.scalar(
                select(EventRecord).where(EventRecord.operation_id == f"{operation_id}:evaluation")
            )
            if event is None:
                raise LookupError(f"evaluation {operation_id} not found")
            return GrayBoxEvaluationResult.model_validate(event.evidence_json["evaluation"])

    async def complete_case(
        self,
        *,
        run_id: str,
        operation_id: str,
        result: GrayBoxExecutionResult,
        policy_event_ids: list[str],
    ) -> str | None:
        async with self.session_factory.begin() as session:
            step = await session.scalar(
                select(RunStepRecord).where(RunStepRecord.operation_id == operation_id)
            )
            if step is None:
                raise LookupError(f"step {operation_id} not found")
            if step.status == "completed":
                finding = await session.scalar(
                    select(FindingRecord).where(
                        FindingRecord.operation_id == f"{operation_id}:finding"
                    )
                )
                return finding.id if finding else None

            execution_event_ids = await self._execution_evidence_ids(
                session,
                operation_id,
            )
            approval_event_ids = [
                event.id
                for event in (
                    await session.scalars(
                        select(EventRecord)
                        .where(EventRecord.operation_id.startswith(f"{operation_id}:approval:"))
                        .where(
                            EventRecord.event_type.in_(["approval_requested", "approval_resolved"])
                        )
                        .order_by(EventRecord.sequence)
                    )
                ).all()
            ]
            evaluation_event = await session.scalar(
                select(EventRecord).where(EventRecord.operation_id == f"{operation_id}:evaluation")
            )
            if evaluation_event is None:
                raise LookupError(f"evaluation {operation_id} not found")
            evidence_ids = [
                *policy_event_ids,
                *approval_event_ids,
                *execution_event_ids,
                evaluation_event.id,
            ]
            step.status = "completed"
            step.outcome = result.evaluation.outcome.value
            step.completed_at = datetime.now(UTC)
            step.result_json = result.model_dump(mode="json")

            finding_id: str | None = None
            if result.evaluation.violated:
                finding_id = str(uuid4())
                session.add(
                    FindingRecord(
                        id=finding_id,
                        run_id=run_id,
                        step_id=step.id,
                        event_id=evaluation_event.id,
                        operation_id=f"{operation_id}:finding",
                        case_id=result.case.id,
                        category=result.case.category,
                        risk_level=result.evaluation.risk_level.value,
                        outcome=result.evaluation.outcome.value,
                        reason=result.evaluation.reason,
                        fingerprint=finding_fingerprint(
                            stage="graybox",
                            case_id=result.case.id,
                            category=result.case.category,
                            is_control=result.case.kind.value == "control",
                        ),
                        evidence_event_ids=evidence_ids,
                        is_control=result.case.kind.value == "control",
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
                        "case_id": result.case.id,
                        "evidence_event_ids": evidence_ids,
                    },
                )
            return finding_id

    async def complete_skipped_case(
        self,
        *,
        run_id: str,
        case: GrayBoxCase,
        operation_id: str,
        sequence: int,
        outcome: GrayBoxOutcome,
        reason: str,
        policy: PolicyGateResult,
    ) -> None:
        step = await self.ensure_step(
            run_id=run_id,
            case_id=case.id,
            operation_id=operation_id,
            sequence=sequence,
        )
        async with self.session_factory.begin() as session:
            stored = await session.get(RunStepRecord, step.id)
            if stored is None or stored.status == "completed":
                return
            stored.status = "completed"
            stored.outcome = outcome.value
            stored.completed_at = datetime.now(UTC)
            stored.result_json = {
                "case": case.model_dump(mode="json"),
                "outcome": outcome.value,
                "reason": reason,
                "policy": policy.model_dump(mode="json"),
            }
            await self._append_event(
                session,
                run_id=run_id,
                step_id=stored.id,
                operation_id=f"{operation_id}:skipped",
                event_type="case_skipped",
                evidence={
                    "case_id": case.id,
                    "outcome": outcome.value,
                    "reason": reason,
                },
            )

    async def finalize_run(
        self,
        *,
        run_id: str,
        status: str,
        terminal_reason: str,
    ) -> None:
        async with self.session_factory.begin() as session:
            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            if run.status in {"completed", "failed", "aborted"}:
                return
            steps = (
                await session.scalars(select(RunStepRecord).where(RunStepRecord.run_id == run_id))
            ).all()
            outcomes = Counter(step.outcome for step in steps)
            run.status = status
            run.terminal_reason = terminal_reason
            run.completed_at = datetime.now(UTC)
            run.completed_cases = sum(
                count for outcome, count in outcomes.items() if outcome != "pending"
            )
            run.violation_count = outcomes.get("violation", 0)
            run.refused_count = outcomes.get("refused", 0)
            run.safe_count = outcomes.get("safe", 0)
            run.error_count = outcomes.get("error", 0) + outcomes.get("inconclusive", 0)
            run.budget_aborted_count = outcomes.get("budget_aborted", 0) + outcomes.get(
                "loop_aborted", 0
            )
            await self._append_event(
                session,
                run_id=run_id,
                operation_id=f"{run_id}:run_terminal",
                event_type=f"run_{status}",
                evidence={
                    "terminal_reason": terminal_reason,
                    "outcomes": dict(outcomes),
                },
            )

    async def get_run(self, run_id: str) -> EvaluationRunRecord:
        async with self.session_factory() as session:
            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            return run

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
        last_sequence = await session.scalar(
            select(func.max(EventRecord.sequence)).where(EventRecord.run_id == run_id)
        )
        event = EventRecord(
            id=str(uuid4()),
            run_id=run_id,
            step_id=step_id,
            sequence=int(last_sequence or 0) + 1,
            operation_id=operation_id,
            event_type=event_type,
            evidence_json=evidence,
        )
        session.add(event)
        await session.flush()
        return event.id

    async def _execution_evidence_ids(
        self,
        session: AsyncSession,
        operation_id: str,
    ) -> list[str]:
        events = (
            await session.scalars(
                select(EventRecord)
                .where(EventRecord.operation_id.startswith(f"{operation_id}:"))
                .where(
                    EventRecord.event_type.in_(
                        [
                            "target_called",
                            "target_policy_observed",
                            "target_approval_observed",
                            "tool_requested",
                            "tool_completed",
                        ]
                    )
                )
                .order_by(EventRecord.sequence)
            )
        ).all()
        return [event.id for event in events]

    async def _load_trace(
        self,
        session: AsyncSession,
        operation_id: str,
    ):
        from app.schemas.graybox_schema import (
            ApprovalEvent,
            PolicyEvent,
            ToolEvent,
            ToolTraceEnvelope,
        )

        events = (
            await session.scalars(
                select(EventRecord)
                .where(EventRecord.operation_id.startswith(f"{operation_id}:"))
                .order_by(EventRecord.sequence)
            )
        ).all()
        requested = {
            event.evidence_json["tool_call_id"]: event.evidence_json
            for event in events
            if event.event_type == "tool_requested"
        }
        completed = {
            event.evidence_json["tool_call_id"]: event.evidence_json
            for event in events
            if event.event_type == "tool_completed"
        }
        tool_events = []
        for call_id, request in requested.items():
            completion = completed.get(call_id, {})
            tool_events.append(ToolEvent.model_validate({**request, **completion}))
        return ToolTraceEnvelope(
            tool_events=tool_events,
            policy_events=[
                PolicyEvent.model_validate(event.evidence_json)
                for event in events
                if event.event_type == "target_policy_observed"
            ],
            approval_events=[
                ApprovalEvent.model_validate(event.evidence_json)
                for event in events
                if event.event_type == "target_approval_observed"
            ],
        )

    @staticmethod
    def _approval_dict(approval: ApprovalRecord) -> dict[str, Any]:
        return {
            "id": approval.id,
            "run_id": approval.run_id,
            "case_id": approval.case_id,
            "status": approval.status,
            "risk_summary": approval.risk_summary,
            "requested_at": approval.requested_at.isoformat(),
            "resolved_at": (approval.resolved_at.isoformat() if approval.resolved_at else None),
            "resolved_by": approval.resolved_by,
            "reason": approval.reason,
        }
