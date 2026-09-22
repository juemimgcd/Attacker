"""一次 Adaptive Run 的手写运行时；SQL 保存业务事实与 Session。"""

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.agent.context import build_context
from app.agent.session import save_session
from app.agent.state import RunState
from app.agent.tools import decision_to_tool_call, validate_decision
from app.infrastructure.model_adapter import PlannerAdapterError, PlannerModelAdapter
from app.repositories.adaptive_repository import AdaptiveRepository
from app.schemas.adaptive_agent_schema import CandidateSnapshot, InformationGain
from app.schemas.attack_sample_schema import CaseKind
from app.schemas.attack_state_schema import (
    AttackState,
    CoverageStatus,
    RunBudgetSnapshot,
    RunStatus,
)
from app.schemas.graybox_schema import (
    AttackPolicy,
    FindingSummary,
    GrayBoxCase,
    GrayBoxOutcome,
    PlannerContext,
    PlannerResult,
    PlannerUsage,
    PolicyGateResult,
    ToolPolicyDecision,
)
from app.schemas.run_control_schema import (
    FallbackAction,
    PlannerUnavailableContext,
    StepProgress,
    StopAction,
    StopContext,
    StopLimits,
)
from app.schemas.target_schema import TargetConfig
from app.services.candidate_builder import CandidateBuilder
from app.services.finish_gate_service import FinishGateService
from app.services.graybox_case_pipeline import GrayBoxCasePipeline
from app.services.graybox_connector import GrayBoxConnector
from app.services.hypothesis_service import HypothesisService
from app.services.policy_service import PolicyService
from app.services.run_control import RunControlService


@dataclass
class RunResources:
    target: TargetConfig
    cases: dict[str, GrayBoxCase]
    policy: AttackPolicy
    planner: PlannerModelAdapter
    started_at: datetime
    secret_values: set[str]
    target_runtime: Callable[[], AbstractAsyncContextManager[TargetConfig]]


class AgentRuntime:
    def __init__(
        self,
        repository: AdaptiveRepository,
        resources: RunResources,
        state: RunState,
        *,
        connector: GrayBoxConnector | None = None,
        finish_gate: FinishGateService | None = None,
    ) -> None:
        self.repository = repository
        self.resources = resources
        self.state = state
        self.policy_service = PolicyService()
        self.candidate_builder = CandidateBuilder()
        self.finish_gate_service = finish_gate or FinishGateService()
        self.hypothesis_service = HypothesisService()
        self.run_control = RunControlService()
        self.case_pipeline = GrayBoxCasePipeline(repository, connector=connector)
        self.snapshot: CandidateSnapshot | None = None

    async def prepare(self) -> PlannerContext:
        self.state.update(**await self.build_candidates())
        if self.snapshot is None:
            raise RuntimeError("candidate snapshot was not prepared")
        facts = await self.repository.load_adaptive_facts(self.state["run_id"])
        return build_context(
            self.snapshot, facts, self.resources.policy.max_steps - self.state["step_count"]
        )

    async def finish(self) -> None:
        if self.state["status"] not in {"waiting_approval", "paused"}:
            self.state.update(**await self.finalize())
        await save_session(self.repository, self.state)

    async def request(self, context: PlannerContext) -> dict[str, Any]:
        """让 Planner 从当前候选快照中选择 action，不接受自由生成的执行参数。"""

        state = self.state
        external_stop = await self._external_stop()
        if external_stop is not None:
            return external_stop
        runtime = self.resources
        elapsed = (datetime.now(UTC) - runtime.started_at).total_seconds()
        if elapsed >= runtime.policy.max_duration_seconds:
            return {
                "next_action": "finalize",
                "status": "aborted",
                "terminal_reason": "run duration budget exhausted",
                "stop_reason": "budget_exhausted",
            }
        if (
            runtime.policy.max_cost is not None
            and Decimal(str(state["planner_estimated_cost"])) >= runtime.policy.max_cost
        ):
            return {
                "next_action": "finalize",
                "status": "aborted",
                "terminal_reason": "planner cost budget exhausted",
                "stop_reason": "budget_exhausted",
            }
        snapshot = self.snapshot
        if snapshot is None:
            raise RuntimeError("prepare must run before request")
        planner_index = state["planner_call_count"] + 1
        operation_id = f"{state['run_id']}:planner:{planner_index}"
        persisted = await self.repository.load_planner_outcome(operation_id)
        recovered_outcome = persisted is not None
        if persisted is not None and persisted["event_type"] == "planner_error":
            evidence = persisted["evidence"]
            try:
                usage = PlannerUsage.model_validate(evidence.get("usage", {}))
                error_category = str(evidence.get("error_type", "planner_error"))
            except (ValueError, TypeError):
                usage = PlannerUsage()
                error_category = "persisted_planner_outcome_invalid"
            return await self._handle_planner_unavailable(
                state=state,
                snapshot=snapshot,
                planner_index=planner_index,
                usage=usage,
                error_category=error_category,
                planner_event_id=str(persisted["event_id"]),
            )
        if persisted is not None:
            evidence = persisted["evidence"]
            try:
                result = PlannerResult.model_validate(
                    {
                        "decision": evidence["decision"],
                        "usage": evidence["usage"],
                        "backend": evidence["backend"],
                        "call_snapshot": evidence["call_snapshot"],
                        "tool_call": evidence.get("tool_call"),
                    }
                )
            except (ValueError, KeyError, TypeError):
                return await self._handle_planner_unavailable(
                    state=state,
                    snapshot=snapshot,
                    planner_index=planner_index,
                    usage=PlannerUsage(),
                    error_category="persisted_planner_outcome_invalid",
                    planner_event_id=str(persisted["event_id"]),
                )
            planner_event_id = str(persisted["event_id"])
            rejection_reason = (
                str(evidence.get("rejection_reason") or "planner_rejected")
                if persisted["event_type"] == "planner_rejected"
                else None
            )
        else:
            remaining_provider_calls = max(
                runtime.policy.max_provider_calls - state["provider_call_count"],
                0,
            )
            if runtime.planner.requires_provider and remaining_provider_calls == 0:
                usage = PlannerUsage()
                planner_error_event_id = await self.repository.record_planner_error(
                    run_id=state["run_id"],
                    operation_id=operation_id,
                    error_type="provider_budget_exhausted",
                    provider_id=runtime.planner.provider_id,
                    model_id=runtime.planner.model_id,
                    usage=usage,
                    call_snapshot=None,
                )
                return await self._handle_planner_unavailable(
                    state=state,
                    snapshot=snapshot,
                    planner_index=planner_index,
                    usage=usage,
                    error_category="provider_budget_exhausted",
                    planner_event_id=planner_error_event_id,
                )
            try:
                result = await runtime.planner.plan(
                    context,
                    operation_id=operation_id,
                    max_physical_attempts=max(remaining_provider_calls, 1),
                )
            except PlannerAdapterError as exc:
                planner_error_event_id = await self.repository.record_planner_error(
                    run_id=state["run_id"],
                    operation_id=operation_id,
                    error_type=exc.error_category,
                    provider_id=exc.provider_id,
                    model_id=exc.model_id,
                    usage=exc.usage,
                    call_snapshot=exc.call_snapshot,
                )
                return await self._handle_planner_unavailable(
                    state=state,
                    snapshot=snapshot,
                    planner_index=planner_index,
                    usage=exc.usage,
                    error_category=exc.error_category,
                    planner_event_id=planner_error_event_id,
                )
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
                usage = PlannerUsage()
                planner_error_event_id = await self.repository.record_planner_error(
                    run_id=state["run_id"],
                    operation_id=operation_id,
                    error_type=type(exc).__name__,
                    provider_id=runtime.planner.provider_id,
                    model_id=runtime.planner.model_id,
                    usage=usage,
                    call_snapshot=None,
                )
                return await self._handle_planner_unavailable(
                    state=state,
                    snapshot=snapshot,
                    planner_index=planner_index,
                    usage=usage,
                    error_category=type(exc).__name__,
                    planner_event_id=planner_error_event_id,
                )

            decision = result.decision
            rejection_reason = validate_decision(context, decision)
            if rejection_reason is None:
                rejection_reason = await self.repository.validate_planner_references(
                    run_id=state["run_id"],
                    evidence_refs=decision.evidence_refs,
                    hypothesis_refs=decision.hypothesis_refs,
                )

        decision = result.decision
        history = [
            *state["decision_history"],
            decision.candidate_id or decision.action,
        ]
        repeated = Counter(history)[history[-1]]
        if not recovered_outcome and repeated > runtime.policy.max_repeated_decisions:
            rejection_reason = "planner repeated the same decision beyond the configured limit"
        if not recovered_outcome:
            if result.tool_call is None:
                result.tool_call = decision_to_tool_call(result.decision, f"call_{planner_index}")
            planner_event_id = await self.repository.record_planner_result(
                run_id=state["run_id"],
                operation_id=operation_id,
                result=result,
                accepted=rejection_reason is None,
                rejection_reason=rejection_reason,
            )
        planner_tokens = result.usage.input_tokens + result.usage.output_tokens
        provider_calls = state["provider_call_count"] + result.usage.physical_attempts
        planner_estimated_cost = state["planner_estimated_cost"] + result.usage.estimated_cost
        common = {
            "planner_call_count": planner_index,
            "provider_call_count": provider_calls,
            "planner_token_count": state["planner_token_count"] + planner_tokens,
            "planner_latency_ms": state["planner_latency_ms"] + result.usage.latency_ms,
            "planner_estimated_cost": planner_estimated_cost,
            "planner_failures": 0,
            "decision_history": history,
        }
        if rejection_reason is not None:
            return await self._handle_planner_unavailable(
                state=state,
                snapshot=snapshot,
                planner_index=planner_index,
                usage=result.usage,
                error_category="planner_rejected",
                planner_event_id=planner_event_id,
                decision_history=history,
            )
        if (
            runtime.policy.max_cost is not None
            and Decimal(str(planner_estimated_cost)) >= runtime.policy.max_cost
        ):
            return {
                **common,
                "next_action": "finalize",
                "status": "aborted",
                "terminal_reason": "planner cost budget exhausted",
                "stop_reason": "budget_exhausted",
            }
        if decision.action == "finish":
            return {
                **common,
                "next_action": "finish",
                "status": "running",
                "terminal_reason": None,
                "stop_reason": None,
            }
        candidate = next(
            item for item in context.candidates if item.candidate_id == decision.candidate_id
        )
        case_id = candidate.action_id
        action_attempt = state["action_repeat_counts"].get(case_id, 0) + 1
        case_operation_id = f"{state['run_id']}:case:{case_id}:{action_attempt}"
        await self.repository.append_event(
            run_id=state["run_id"],
            operation_id=f"{case_operation_id}:decision",
            event_type="decision_bound",
            evidence={
                "planner_event_id": planner_event_id,
                "candidate_snapshot_id": snapshot.snapshot_id,
                "candidate_id": candidate.candidate_id,
                "case_id": case_id,
            },
        )
        return {
            **common,
            "current_case_id": case_id,
            "current_operation_id": case_operation_id,
            "current_step_started_at": datetime.now(UTC).isoformat(),
            "current_step_id": None,
            "evaluation_event_id": None,
            "policy_event_ids": [],
            "approval_id": None,
            "approval_status": None,
            "step_count": state["step_count"] + 1,
            "expected_information_gain": (
                decision.expected_information_gain.value
                if decision.expected_information_gain is not None
                else None
            ),
            "last_coverage_delta": 0,
            "last_evidence_delta": 0,
            "last_finding_delta": 0,
            "last_target_transport_failed": False,
            "next_action": "policy",
        }

    async def _handle_planner_unavailable(
        self,
        *,
        state: RunState,
        snapshot: CandidateSnapshot,
        planner_index: int,
        usage: PlannerUsage,
        error_category: str,
        planner_event_id: str,
        decision_history: list[str] | None = None,
    ) -> dict[str, Any]:
        runtime = self.resources
        provider_calls = state["provider_call_count"] + usage.physical_attempts
        planner_tokens = usage.input_tokens + usage.output_tokens
        elapsed = max(
            (datetime.now(UTC) - runtime.started_at).total_seconds(),
            0,
        )
        control_state = AttackState(
            run_id=state["run_id"],
            goal_id=state["goal_id"],
            test_principal_refs=state["test_principal_refs"],
            candidate_snapshot_id=snapshot.snapshot_id,
            candidate_action_ids=[candidate.action_id for candidate in snapshot.candidates],
            completed_action_ids=state["completed_case_ids"],
            denied_action_ids=state["denied_action_ids"],
            coverage={tag: CoverageStatus(status) for tag, status in state["coverage"].items()},
            hypothesis_refs=state["hypothesis_refs"],
            observation_refs=state["observation_refs"],
            finding_refs=state["finding_refs"],
            budget=RunBudgetSnapshot(
                max_steps=runtime.policy.max_steps,
                max_target_calls=runtime.policy.max_target_calls,
                max_provider_calls=runtime.policy.max_provider_calls,
                max_duration_seconds=runtime.policy.max_duration_seconds,
                max_cost=runtime.policy.max_cost,
                steps_used=min(state["step_count"], runtime.policy.max_steps),
                target_calls_used=min(
                    state["target_call_count"],
                    runtime.policy.max_target_calls,
                ),
                provider_calls_used=min(
                    provider_calls,
                    runtime.policy.max_provider_calls,
                ),
                elapsed_seconds=min(elapsed, runtime.policy.max_duration_seconds),
                cost_used=Decimal(str(state["planner_estimated_cost"] + usage.estimated_cost)),
            ),
            status=RunStatus.running,
            checkpoint_ref=state.get("checkpoint_ref"),
            planner_fallback_mode=runtime.policy.planner_fallback_mode,
            last_state_fingerprint=state.get("last_state_fingerprint"),
            consecutive_no_gain_steps=state["consecutive_no_gain_steps"],
            repeated_state_count=state["repeated_state_count"],
            planner_failure_count=state["planner_failures"] + 1,
            target_transport_failure_count=state["target_transport_failure_count"],
        )
        fallback_state, fallback = self.run_control.handle_planner_unavailable(
            control_state,
            PlannerUnavailableContext(
                reason=error_category,
                actual_model_id=runtime.planner.model_id,
                actual_provider_id=runtime.planner.provider_id,
                checkpoint_ref=control_state.checkpoint_ref,
            ),
        )
        await self.repository.append_event(
            run_id=state["run_id"],
            operation_id=f"{state['run_id']}:planner:{planner_index}:fallback",
            event_type=fallback.event.event_type,
            evidence={
                "mode": fallback.event.mode.value,
                "reason": fallback.event.reason,
                "actual_model_id": fallback.event.actual_model_id,
                "actual_provider_id": fallback.event.actual_provider_id,
                "candidate_snapshot_id": fallback.event.candidate_snapshot_id,
                "candidate_id": fallback.event.candidate_id,
                "planner_event_id": planner_event_id,
                "physical_attempts": usage.physical_attempts,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "latency_ms": usage.latency_ms,
                "estimated_cost": usage.estimated_cost,
                "attempt_errors": usage.attempt_errors,
            },
        )
        common = {
            "planner_call_count": planner_index,
            "provider_call_count": provider_calls,
            "planner_token_count": state["planner_token_count"] + planner_tokens,
            "planner_latency_ms": state["planner_latency_ms"] + usage.latency_ms,
            "planner_estimated_cost": (state["planner_estimated_cost"] + usage.estimated_cost),
            "planner_failures": fallback_state.planner_failure_count,
            "planner_fallback_snapshot": (
                fallback_state.planner_fallback_snapshot.model_dump(mode="json")
                if fallback_state.planner_fallback_snapshot is not None
                else None
            ),
            "decision_history": decision_history or state["decision_history"],
        }
        if fallback.action == FallbackAction.pause:
            terminal_reason = f"planner paused: {error_category}"
            return {
                **common,
                "next_action": "pause",
                "status": "paused",
                "terminal_reason": terminal_reason,
                "stop_reason": None,
            }
        if fallback.action == FallbackAction.terminate:
            stop_reason = fallback.stop_reason.value if fallback.stop_reason is not None else None
            return {
                **common,
                "next_action": "finalize",
                "status": ("failed" if fallback_state.status == RunStatus.failed else "aborted"),
                "terminal_reason": f"planner unavailable: {error_category}",
                "stop_reason": stop_reason,
            }
        if fallback_state.planner_failure_count >= runtime.policy.max_planner_failures:
            return {
                **common,
                "next_action": "finalize",
                "status": "failed",
                "terminal_reason": "planner failure limit reached",
                "stop_reason": "planner_failed",
            }

        candidate = next(
            candidate
            for candidate in snapshot.candidates
            if candidate.action_id == fallback.candidate_id
        )
        case_id = candidate.action_id
        case_operation_id = (
            f"{state['run_id']}:case:{case_id}:{state['action_repeat_counts'].get(case_id, 0) + 1}"
        )
        await self.repository.append_event(
            run_id=state["run_id"],
            operation_id=f"{case_operation_id}:decision",
            event_type="decision_bound",
            evidence={
                "decision_source": "deterministic_fallback",
                "planner_event_id": planner_event_id,
                "candidate_snapshot_id": snapshot.snapshot_id,
                "candidate_id": candidate.candidate_id,
                "case_id": case_id,
            },
        )
        return {
            **common,
            "decision_history": [
                *(decision_history or state["decision_history"]),
                candidate.candidate_id,
            ],
            "current_case_id": case_id,
            "current_operation_id": case_operation_id,
            "current_step_started_at": datetime.now(UTC).isoformat(),
            "current_step_id": None,
            "evaluation_event_id": None,
            "policy_event_ids": [],
            "approval_id": None,
            "approval_status": None,
            "step_count": state["step_count"] + 1,
            "expected_information_gain": candidate.expected_information_gain.value,
            "last_coverage_delta": 0,
            "last_evidence_delta": 0,
            "last_finding_delta": 0,
            "last_target_transport_failed": False,
            "next_action": "policy",
            "status": "running",
            "terminal_reason": None,
            "stop_reason": None,
        }

    async def execute_case(self) -> dict[str, Any]:
        """一次完成执行、归一化、评估和落库，直接复用共享 Pipeline。"""
        state, runtime = self.state, self.resources
        external_stop = await self._external_stop()
        if external_stop is not None:
            return external_stop
        case = runtime.cases[str(state["current_case_id"])]
        async with runtime.target_runtime() as target:
            secrets = set(runtime.secret_values) | {
                value for value in target.headers.values() if value
            }
            if target.auth.token:
                secrets.add(target.auth.token)
            result = await self.case_pipeline.run_case(
                run_id=state["run_id"],
                case=case,
                target=target,
                operation_id=str(state["current_operation_id"]),
                sequence=state["step_count"],
                approval_id=state.get("approval_id"),
                secret_values=secrets,
                policy=PolicyGateResult(
                    decision=ToolPolicyDecision(str(state["policy_decision"])),
                    reason=str(state["policy_reason"]),
                    approval_id=state.get("approval_id"),
                ),
                policy_event_ids=state["policy_event_ids"],
            )
        findings = list(state["finding_summaries"])
        if result.evaluation.violated:
            findings.append(
                FindingSummary(
                    finding_ref=result.finding_id,
                    case_id=case.id,
                    category=case.category,
                    risk_level=result.evaluation.risk_level,
                    reason=result.evaluation.reason,
                ).model_dump(mode="json")
            )
        return {
            "current_step_id": result.step_id,
            "evaluation_event_id": result.evaluation_event_id,
            "target_call_count": state["target_call_count"] + int(result.target_called),
            "observation_refs": list(
                dict.fromkeys([*state["observation_refs"], result.observation.observation_ref])
            ),
            "finding_refs": list(
                dict.fromkeys(
                    [*state["finding_refs"], *([result.finding_id] if result.finding_id else [])]
                )
            ),
            "finding_summaries": findings,
        }

    async def build_candidates(self) -> dict[str, Any]:
        state = self.state
        runtime = self.resources
        facts = await self.repository.load_adaptive_facts(state["run_id"])
        elapsed = (datetime.now(UTC) - runtime.started_at).total_seconds()
        budget = RunBudgetSnapshot(
            max_steps=runtime.policy.max_steps,
            max_target_calls=runtime.policy.max_target_calls,
            max_provider_calls=runtime.policy.max_provider_calls,
            max_duration_seconds=runtime.policy.max_duration_seconds,
            max_cost=runtime.policy.max_cost,
            steps_used=min(state["step_count"], runtime.policy.max_steps),
            target_calls_used=min(
                state["target_call_count"],
                runtime.policy.max_target_calls,
            ),
            provider_calls_used=min(
                state["provider_call_count"],
                runtime.policy.max_provider_calls,
            ),
            elapsed_seconds=min(elapsed, runtime.policy.max_duration_seconds),
            cost_used=Decimal(str(state["planner_estimated_cost"])),
        )
        actions = self.candidate_builder.actions_from_cases(
            cases=runtime.cases.values(),
            target_id=state["target_id"],
            test_principal_ref=state["test_principal_refs"][0],
        )
        snapshot = self.candidate_builder.build(
            run_id=state["run_id"],
            actions=actions,
            policy=runtime.policy,
            budget=budget,
            completed_action_ids=set(state["completed_case_ids"]),
            denied_action_ids=set(state["denied_action_ids"]),
            action_repeat_counts=state["action_repeat_counts"],
            coverage=facts["coverage"],
            hypotheses=facts["hypotheses"],
            evidence_gaps=facts["evidence_gaps"],
            valid_test_principal_refs=set(state["test_principal_refs"]),
            recent_similarity_keys=tuple(state["recent_similarity_keys"][-3:]),
        )
        self.snapshot = snapshot
        await self.repository.record_candidate_snapshot(
            snapshot,
            previous_snapshot_id=state.get("candidate_snapshot_id"),
        )
        return {
            "candidate_snapshot_id": snapshot.snapshot_id,
            "candidate_action_ids": [candidate.action_id for candidate in snapshot.candidates],
            "coverage": {tag: status.value for tag, status in facts["coverage"].items()},
            "hypothesis_refs": [fact.hypothesis_ref for fact in facts["hypotheses"].values()],
            "observation_refs": facts["observation_refs"],
            "finding_refs": facts["finding_refs"],
            "evidence_gaps": sorted(facts["evidence_gaps"]),
            "information_gain_refs": facts["information_gain_refs"],
            "next_action": "plan",
        }

    async def finish_gate(self) -> dict[str, Any]:
        state = self.state
        external_stop = await self._external_stop()
        if external_stop is not None:
            return external_stop
        runtime = self.resources
        snapshot = await self.repository.load_candidate_snapshot(
            run_id=state["run_id"],
            snapshot_id=str(state["candidate_snapshot_id"]),
        )
        facts = await self.repository.load_adaptive_facts(state["run_id"])
        approvals = await self.repository.list_approvals(state["run_id"])
        allowed_cases = [
            case
            for case in runtime.cases.values()
            if case.enabled
            and case.compatible
            and case.id in runtime.policy.allowed_case_ids
            and case.capability_contract in runtime.policy.allowed_capability_contracts
            and case.provider_instance_ref in runtime.policy.allowed_provider_instance_refs
        ]
        result = self.finish_gate_service.evaluate(
            required_coverage_tags={
                tag for case in allowed_cases for tag in (case.coverage_tags or [case.category])
            },
            required_control_action_ids={
                case.id for case in allowed_cases if case.kind == CaseKind.control
            },
            coverage=facts["coverage"],
            completed_action_ids=set(state["completed_case_ids"]),
            evidence_gaps=facts["evidence_gaps"],
            pending_approval_ids={
                str(approval["id"]) for approval in approvals if approval["status"] == "pending"
            },
            allow_early_finish=runtime.policy.allow_early_finish,
            has_candidates=bool(snapshot.candidates),
        )
        if result.allowed:
            return {
                "next_action": "finalize",
                "status": "completed",
                "terminal_reason": result.detail,
                "stop_reason": "completed",
            }
        await self.repository.record_finish_rejected(
            run_id=state["run_id"],
            operation_id=(f"{state['run_id']}:finish_rejected:{state['planner_call_count']}"),
            reason_code=result.reason_code.value,
            detail=result.detail,
        )
        if not snapshot.candidates:
            return {
                "next_action": "finalize",
                "status": "completed",
                "terminal_reason": result.detail,
                "stop_reason": "no_information_gain",
            }
        return {
            "next_action": "build",
            "status": "running",
            "terminal_reason": None,
        }

    async def policy_gate(self) -> dict[str, Any]:
        """在任何 Target 副作用前应用确定性策略、预算与审批事实。"""
        state = self.state

        external_stop = await self._external_stop()
        if external_stop is not None:
            return external_stop
        runtime = self.resources
        case = runtime.cases[str(state["current_case_id"])]
        approval = await self.repository.get_approval(
            run_id=state["run_id"],
            case_id=case.id,
        )
        approval_id = approval.id if approval else None
        approval_status = approval.status if approval else None
        elapsed = (datetime.now(UTC) - runtime.started_at).total_seconds()
        result = self.policy_service.evaluate(
            policy=runtime.policy,
            target_id=state["target_id"],
            case=case,
            remaining_steps=runtime.policy.max_steps - state["step_count"] + 1,
            target_call_count=state["target_call_count"],
            elapsed_seconds=elapsed,
            approval_status=approval_status,
            approval_id=approval_id,
        )
        policy_index = len(state["policy_event_ids"]) + 1
        event_id = await self.repository.record_policy_result(
            run_id=state["run_id"],
            case_id=case.id,
            operation_id=f"{state['current_operation_id']}:policy:{policy_index}",
            result=result,
        )
        policy_event_ids = [*state["policy_event_ids"], event_id]
        if state.get("recovery_pending", False):
            recovery_event_id = await self.repository.append_event(
                run_id=state["run_id"],
                operation_id=f"{state['current_operation_id']}:recovery:policy",
                event_type="recovery_policy_revalidated",
                evidence={
                    "case_id": case.id,
                    "decision": result.decision.value,
                    "reason": result.reason,
                    "thread_id": state["thread_id"],
                },
            )
            policy_event_ids.append(recovery_event_id)
        next_action = {
            ToolPolicyDecision.allow: "execute",
            ToolPolicyDecision.deny: "skip",
            ToolPolicyDecision.approval_required: "review",
        }[result.decision]
        return {
            "policy_decision": result.decision.value,
            "policy_reason": result.reason,
            "policy_event_ids": policy_event_ids,
            "approval_id": approval_id,
            "approval_status": approval_status,
            "next_action": next_action,
            "recovery_pending": False,
        }

    async def wait_for_approval(self) -> dict[str, Any]:
        state = self.state
        runtime = self.resources
        case = runtime.cases[str(state["current_case_id"])]
        approval = await self.repository.ensure_approval(
            run_id=state["run_id"],
            case=case,
            operation_id=f"{state['current_operation_id']}:approval",
        )
        return {
            "approval_id": approval.id,
            "approval_status": approval.status,
            "status": "waiting_approval",
            "terminal_reason": None,
            "stop_reason": None,
        }

    async def update_facts(self) -> dict[str, Any]:
        state = self.state
        runtime = self.resources
        case = runtime.cases[str(state["current_case_id"])]
        operation_id = str(state["current_operation_id"])
        evaluation = await self.repository.load_evaluation(operation_id)
        _, response, _ = await self.repository.load_target_execution(operation_id)
        facts = await self.repository.load_adaptive_facts(state["run_id"])
        previous_hypothesis = facts["hypotheses"][case.id]
        evaluation_ref = str(state["evaluation_event_id"])
        transitioned = self.hypothesis_service.transition(
            previous=previous_hypothesis,
            hypothesis_ref=previous_hypothesis.hypothesis_ref,
            evaluation=evaluation,
            evidence_refs=(evaluation_ref,),
        )
        await self.repository.record_hypothesis_transition(
            run_id=state["run_id"],
            operation_id=f"{operation_id}:hypothesis",
            hypothesis=transitioned,
            step_id=state.get("current_step_id"),
        )
        coverage_facts = self.hypothesis_service.coverage_facts(
            tags=tuple(case.coverage_tags or [case.category]),
            evaluation=evaluation,
            evidence_refs=(evaluation_ref,),
        )
        started_at = datetime.fromisoformat(str(state["current_step_started_at"]))
        gain = self.hypothesis_service.actual_gain(
            previous_coverage=facts["coverage"],
            previous_hypothesis=previous_hypothesis,
            current_coverage=coverage_facts,
            evaluation=evaluation,
            target_call_cost=1,
            planner_cost=1,
            duration_delta=max(
                (datetime.now(UTC) - started_at).total_seconds(),
                0,
            ),
        )
        _, gain_ref, _ = await self.repository.record_coverage_and_gain(
            run_id=state["run_id"],
            operation_id=operation_id,
            coverage_facts=coverage_facts,
            gain=gain,
            predicted_information_gain=(
                None
                if state.get("expected_information_gain") is None
                else InformationGain(str(state["expected_information_gain"]))
            ),
            step_id=state.get("current_step_id"),
        )
        updated_facts = await self.repository.load_adaptive_facts(state["run_id"])
        return {
            "coverage": {tag: status.value for tag, status in updated_facts["coverage"].items()},
            "hypothesis_refs": [
                fact.hypothesis_ref for fact in updated_facts["hypotheses"].values()
            ],
            "evidence_gaps": sorted(updated_facts["evidence_gaps"]),
            "information_gain_refs": list(
                dict.fromkeys(
                    [
                        *state["information_gain_refs"],
                        gain_ref,
                    ]
                )
            ),
            "last_coverage_delta": gain.coverage_delta,
            "last_evidence_delta": gain.evidence_completeness_delta,
            "last_finding_delta": gain.confirmed_finding_delta,
            "last_target_transport_failed": response.error_type is not None,
            "next_action": "decide",
        }

    async def skip(self) -> dict[str, Any]:
        state = self.state
        runtime = self.resources
        case = runtime.cases[str(state["current_case_id"])]
        policy = PolicyGateResult(
            decision=ToolPolicyDecision(str(state["policy_decision"])),
            reason=str(state["policy_reason"]),
            approval_id=state.get("approval_id"),
        )
        outcome = (
            GrayBoxOutcome.approval_rejected
            if state.get("approval_status") == "rejected"
            else GrayBoxOutcome.policy_denied
        )
        await self.repository.complete_skipped_case(
            run_id=state["run_id"],
            case=case,
            operation_id=str(state["current_operation_id"]),
            sequence=state["step_count"],
            outcome=outcome,
            reason=policy.reason,
            policy=policy,
        )
        return {
            "denied_action_ids": list(dict.fromkeys([*state["denied_action_ids"], case.id])),
            "next_action": "decide",
        }

    async def decide_next(self) -> dict[str, Any]:
        state = self.state
        runtime = self.resources
        case_id = str(state["current_case_id"])
        completed = state["completed_case_ids"]
        if case_id not in state["denied_action_ids"]:
            completed = list(dict.fromkeys([*completed, case_id]))
        repeat_counts = dict(state["action_repeat_counts"])
        repeat_counts[case_id] = repeat_counts.get(case_id, 0) + 1
        recent_similarity_keys = [
            *state["recent_similarity_keys"],
            runtime.cases[case_id].category,
        ][-10:]
        elapsed = max(
            (datetime.now(UTC) - runtime.started_at).total_seconds(),
            0,
        )
        state_fingerprint = self._state_fingerprint(
            {
                "completed_case_ids": completed,
                "denied_action_ids": state["denied_action_ids"],
                "coverage": state["coverage"],
                "finding_refs": state["finding_refs"],
            }
        )
        control_state = AttackState(
            run_id=state["run_id"],
            goal_id=state["goal_id"],
            test_principal_refs=state["test_principal_refs"],
            candidate_snapshot_id=state.get("candidate_snapshot_id"),
            candidate_action_ids=state["candidate_action_ids"],
            completed_action_ids=completed,
            denied_action_ids=state["denied_action_ids"],
            coverage={tag: CoverageStatus(status) for tag, status in state["coverage"].items()},
            hypothesis_refs=state["hypothesis_refs"],
            observation_refs=state["observation_refs"],
            finding_refs=state["finding_refs"],
            budget=RunBudgetSnapshot(
                max_steps=runtime.policy.max_steps,
                max_target_calls=runtime.policy.max_target_calls,
                max_provider_calls=runtime.policy.max_provider_calls,
                max_duration_seconds=runtime.policy.max_duration_seconds,
                max_cost=runtime.policy.max_cost,
                steps_used=min(state["step_count"], runtime.policy.max_steps),
                target_calls_used=min(
                    state["target_call_count"],
                    runtime.policy.max_target_calls,
                ),
                provider_calls_used=min(
                    state["provider_call_count"],
                    runtime.policy.max_provider_calls,
                ),
                elapsed_seconds=min(elapsed, runtime.policy.max_duration_seconds),
                cost_used=Decimal(str(state["planner_estimated_cost"])),
            ),
            last_state_fingerprint=state.get("last_state_fingerprint"),
            consecutive_no_gain_steps=state["consecutive_no_gain_steps"],
            repeated_state_count=state["repeated_state_count"],
            planner_failure_count=state["planner_failures"],
            target_transport_failure_count=state["target_transport_failure_count"],
        )
        control_state = self.run_control.record_step_progress(
            control_state,
            StepProgress(
                state_fingerprint=state_fingerprint,
                coverage_delta=state["last_coverage_delta"],
                evidence_delta=state["last_evidence_delta"],
                finding_delta=state["last_finding_delta"],
                target_transport_failed=state["last_target_transport_failed"],
            ),
        )
        control_flags = await self.repository.load_control_flags(state["run_id"])
        stop_decision = self.run_control.evaluate_stop(
            control_state,
            StopContext(
                cancelled="run_cancel_requested" in control_flags,
                target_authorization_revoked=("target_authorization_revoked" in control_flags),
                policy_termination_requested=("policy_termination_requested" in control_flags),
                has_executable_candidates=True,
            ),
            StopLimits(
                max_consecutive_no_gain_steps=(runtime.policy.max_no_information_gain_steps),
                max_repeated_state_count=runtime.policy.max_repeated_states,
                max_planner_failures=runtime.policy.max_planner_failures,
                max_target_transport_failures=(runtime.policy.max_target_transport_failures),
            ),
        )
        terminal_reason: str | None = None
        stop_reason: str | None = None
        status = "running"
        if stop_decision.action == StopAction.stop:
            stop_reason = stop_decision.stop_reason.value if stop_decision.stop_reason else None
            terminal_reason = stop_decision.reason_code
            if stop_reason == "completed":
                status = "completed"
            elif stop_reason == "cancelled":
                status = "cancelled"
            else:
                status = "aborted"
        elif runtime.policy.stop_on_critical and any(
            summary["risk_level"] == "critical" for summary in state["finding_summaries"]
        ):
            terminal_reason = "critical finding stop policy triggered"
            stop_reason = "policy_terminated"
            status = "completed"
        await self.repository.record_run_control(
            run_id=state["run_id"],
            operation_id=str(state["current_operation_id"]),
            state_fingerprint=state_fingerprint,
            repeated_state_count=control_state.repeated_state_count,
            consecutive_no_gain_steps=control_state.consecutive_no_gain_steps,
            target_transport_failure_count=(control_state.target_transport_failure_count),
            action=stop_decision.action.value,
            stop_reason=stop_reason,
            reason_code=stop_decision.reason_code,
            step_id=state.get("current_step_id"),
        )
        return {
            "completed_case_ids": completed,
            "action_repeat_counts": repeat_counts,
            "recent_similarity_keys": recent_similarity_keys,
            "last_state_fingerprint": control_state.last_state_fingerprint,
            "repeated_state_count": control_state.repeated_state_count,
            "consecutive_no_gain_steps": control_state.consecutive_no_gain_steps,
            "target_transport_failure_count": (control_state.target_transport_failure_count),
            "next_action": "finalize" if terminal_reason else "build",
            "status": status,
            "terminal_reason": terminal_reason,
            "stop_reason": stop_reason,
        }

    async def finalize(self) -> dict[str, Any]:
        state = self.state
        status = state.get("status", "completed")
        terminal_reason = state.get("terminal_reason") or "planner finished the run"
        await self.repository.finalize_run(
            run_id=state["run_id"],
            status=status,
            terminal_reason=terminal_reason,
            stop_reason=state.get("stop_reason"),
        )
        return {"status": status, "terminal_reason": terminal_reason, "next_action": "done"}

    async def _external_stop(
        self,
    ) -> dict[str, Any] | None:
        state = self.state
        flags = await self.repository.load_control_flags(state["run_id"])
        if "run_cancel_requested" in flags:
            evidence = flags["run_cancel_requested"]
            return {
                "next_action": "finalize",
                "status": "cancelled",
                "terminal_reason": str(evidence.get("reason", "run cancelled")),
                "stop_reason": "cancelled",
            }
        for event_type in (
            "target_authorization_revoked",
            "policy_termination_requested",
        ):
            if event_type in flags:
                evidence = flags[event_type]
                return {
                    "next_action": "finalize",
                    "status": "aborted",
                    "terminal_reason": str(evidence.get("reason", event_type)),
                    "stop_reason": "policy_terminated",
                }
        return None

    @staticmethod
    def _state_fingerprint(value: dict[str, Any]) -> str:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
