"""受约束自适应攻击状态图；模型只选择候选，Core 掌握授权、执行和持久化。"""

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.infrastructure.model_adapter import PlannerAdapterError
from app.repositories.adaptive_repository import AdaptiveRepository
from app.schemas.adaptive_agent_schema import (
    CandidateSnapshot,
    InformationGain,
    ObservationSource,
    PlannerReasonCode,
)
from app.schemas.attack_sample_schema import CaseKind
from app.schemas.attack_state_schema import (
    AttackState,
    CoverageStatus,
    RunBudgetSnapshot,
    RunStatus,
)
from app.schemas.graybox_schema import (
    FindingSummary,
    GrayBoxExecutionResult,
    GrayBoxOutcome,
    PlannerContext,
    PlannerDecision,
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
from app.services.candidate_builder import CandidateBuilder
from app.services.finish_gate_service import FinishGateService
from app.services.graybox_connector import GrayBoxConnector
from app.services.graybox_evaluator_service import GrayBoxEvaluatorService
from app.services.hypothesis_service import HypothesisService
from app.services.observation_normalizer import ObservationNormalizer
from app.services.policy_service import PolicyService
from app.services.run_control import RunControlService
from app.services.tool_trace_adapter import ToolTraceAdapter
from app.workflows.attack_state import AttackGraphState


class AttackGraph:
    """编排 Plan→Gate→Act→Observe→Evaluate，并把业务事实写入 SQL。"""

    def __init__(
        self,
        *,
        repository: AdaptiveRepository,
        runtime_registry: Any,
        checkpointer: Any,
    ) -> None:
        self.repository = repository
        self.runtime_registry = runtime_registry
        self.policy_service = PolicyService()
        self.candidate_builder = CandidateBuilder()
        self.finish_gate_service = FinishGateService()
        self.hypothesis_service = HypothesisService()
        self.observation_normalizer = ObservationNormalizer()
        self.run_control = RunControlService()
        self.connector = GrayBoxConnector()
        self.trace_adapter = ToolTraceAdapter()
        self.evaluator = GrayBoxEvaluatorService()
        self.graph = self._build().compile(checkpointer=checkpointer)

    def _build(self) -> StateGraph:
        """声明节点和条件边；checkpoint 只负责恢复控制流位置。"""

        builder = StateGraph(AttackGraphState)
        builder.add_node("initialize_run", self.initialize_run)
        builder.add_node("build_candidates", self.build_candidates)
        builder.add_node("plan_next_case", self.plan_next_case)
        builder.add_node("finish_gate", self.finish_gate)
        builder.add_node("policy_gate", self.policy_gate)
        builder.add_node("prepare_human_review", self.prepare_human_review)
        builder.add_node("human_review", self.human_review)
        builder.add_node("planner_pause", self.planner_pause)
        builder.add_node("execute", self.execute)
        builder.add_node("normalize_observation", self.normalize_observation)
        builder.add_node("skip", self.skip)
        builder.add_node("evaluate", self.evaluate)
        builder.add_node("persist", self.persist)
        builder.add_node("update_facts", self.update_facts)
        builder.add_node("decide_next", self.decide_next)
        builder.add_node("finalize", self.finalize)
        builder.add_edge(START, "initialize_run")
        builder.add_edge("initialize_run", "build_candidates")
        builder.add_edge("build_candidates", "plan_next_case")
        builder.add_conditional_edges(
            "plan_next_case",
            self._after_plan,
            {
                "policy": "policy_gate",
                "retry": "plan_next_case",
                "finish": "finish_gate",
                "finalize": "finalize",
                "pause": "planner_pause",
            },
        )
        builder.add_conditional_edges(
            "finish_gate",
            self._after_finish,
            {"build": "build_candidates", "finalize": "finalize"},
        )
        builder.add_conditional_edges(
            "policy_gate",
            self._after_policy,
            {
                "review": "prepare_human_review",
                "execute": "execute",
                "skip": "skip",
                "finalize": "finalize",
            },
        )
        builder.add_edge("prepare_human_review", "human_review")
        builder.add_edge("human_review", "policy_gate")
        builder.add_edge("planner_pause", "build_candidates")
        builder.add_conditional_edges(
            "execute",
            self._after_execute,
            {
                "normalize": "normalize_observation",
                "finalize": "finalize",
            },
        )
        builder.add_edge("normalize_observation", "evaluate")
        builder.add_edge("evaluate", "persist")
        builder.add_edge("persist", "update_facts")
        builder.add_edge("update_facts", "decide_next")
        builder.add_edge("skip", "decide_next")
        builder.add_conditional_edges(
            "decide_next",
            self._after_decide,
            {"build": "build_candidates", "finalize": "finalize"},
        )
        builder.add_edge("finalize", END)
        return builder

    async def initialize_run(self, state: AttackGraphState) -> dict[str, Any]:
        runtime = self.runtime_registry.get(state["run_id"])
        facts = await self.repository.load_adaptive_facts(state["run_id"])
        coverage = {tag: status.value for tag, status in facts["coverage"].items()}
        for case in runtime.cases.values():
            for tag in case.coverage_tags or [case.category]:
                coverage.setdefault(tag, CoverageStatus.not_started.value)
        return {
            "goal_id": state.get(
                "goal_id",
                f"adaptive_graybox:{state['run_id']}",
            ),
            "checkpoint_ref": (state.get("checkpoint_ref") or f"langgraph:{state['thread_id']}"),
            "completed_case_ids": state.get("completed_case_ids", []),
            "denied_action_ids": state.get("denied_action_ids", []),
            "candidate_snapshot_id": state.get("candidate_snapshot_id"),
            "candidate_action_ids": state.get("candidate_action_ids", []),
            "coverage": coverage,
            "hypothesis_refs": [fact.hypothesis_ref for fact in facts["hypotheses"].values()],
            "observation_refs": facts["observation_refs"],
            "finding_refs": facts["finding_refs"],
            "evidence_gaps": sorted(facts["evidence_gaps"]),
            "action_repeat_counts": state.get("action_repeat_counts", {}),
            "recent_similarity_keys": state.get("recent_similarity_keys", []),
            "information_gain_refs": facts["information_gain_refs"],
            "expected_information_gain": state.get("expected_information_gain"),
            "last_coverage_delta": state.get("last_coverage_delta", 0),
            "last_evidence_delta": state.get("last_evidence_delta", 0),
            "last_finding_delta": state.get("last_finding_delta", 0),
            "last_target_transport_failed": state.get(
                "last_target_transport_failed",
                False,
            ),
            "test_principal_refs": state.get(
                "test_principal_refs",
                ["default-test-principal"],
            ),
            "finding_summaries": state.get("finding_summaries", []),
            "policy_event_ids": [],
            "planner_call_count": state.get("planner_call_count", 0),
            "provider_call_count": state.get("provider_call_count", 0),
            "planner_token_count": state.get("planner_token_count", 0),
            "planner_latency_ms": state.get("planner_latency_ms", 0),
            "planner_estimated_cost": state.get("planner_estimated_cost", 0),
            "planner_failures": state.get("planner_failures", 0),
            "planner_fallback_snapshot": state.get("planner_fallback_snapshot"),
            "decision_history": state.get("decision_history", []),
            "target_call_count": state.get("target_call_count", 0),
            "target_transport_failure_count": state.get(
                "target_transport_failure_count",
                0,
            ),
            "graph_step_count": state.get("graph_step_count", 0),
            "last_state_fingerprint": state.get("last_state_fingerprint"),
            "repeated_state_count": state.get("repeated_state_count", 0),
            "consecutive_no_gain_steps": state.get("consecutive_no_gain_steps", 0),
            "status": "running",
            "next_action": "plan",
            "stop_reason": state.get("stop_reason"),
            "recovery_pending": state.get("recovery_pending", False),
        }

    async def build_candidates(self, state: AttackGraphState) -> dict[str, Any]:
        runtime = self.runtime_registry.get(state["run_id"])
        facts = await self.repository.load_adaptive_facts(state["run_id"])
        elapsed = (datetime.now(UTC) - runtime.started_at).total_seconds()
        budget = RunBudgetSnapshot(
            max_steps=runtime.policy.max_steps,
            max_target_calls=runtime.policy.max_target_calls,
            max_provider_calls=runtime.policy.max_provider_calls,
            max_duration_seconds=runtime.policy.max_duration_seconds,
            max_cost=runtime.policy.max_cost,
            steps_used=min(state["graph_step_count"], runtime.policy.max_steps),
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

    async def plan_next_case(self, state: AttackGraphState) -> dict[str, Any]:
        """让 Planner 从当前候选快照中选择 action，不接受自由生成的执行参数。"""

        external_stop = await self._external_stop(state)
        if external_stop is not None:
            return external_stop
        runtime = self.runtime_registry.get(state["run_id"])
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
        remaining_steps = runtime.policy.max_steps - state["graph_step_count"]
        snapshot = await self.repository.load_candidate_snapshot(
            run_id=state["run_id"],
            snapshot_id=str(state["candidate_snapshot_id"]),
        )
        facts = await self.repository.load_adaptive_facts(state["run_id"])
        candidates = list(snapshot.candidates[:100])
        observations = facts["observations"][-20:]
        finding_refs = facts["finding_refs"][-100:]
        hypothesis_refs = list(
            dict.fromkeys(
                hypothesis_ref
                for candidate in candidates
                for hypothesis_ref in candidate.hypothesis_refs
            )
        )[:100]
        coverage_tags = sorted(
            {tag for candidate in candidates for tag in candidate.coverage_tags}
        )[:100]
        context = PlannerContext(
            candidate_snapshot_id=snapshot.snapshot_id,
            candidates=candidates,
            observations=observations,
            evidence_refs=[
                *(observation.observation_ref for observation in observations),
                *finding_refs,
            ],
            finding_refs=finding_refs,
            hypothesis_refs=hypothesis_refs,
            coverage={
                tag: facts["coverage"][tag] for tag in coverage_tags if tag in facts["coverage"]
            },
            coverage_refs=[
                facts["coverage_ref_by_tag"][tag]
                for tag in coverage_tags
                if tag in facts["coverage_ref_by_tag"]
            ],
            information_gain_refs=facts["information_gain_refs"][-100:],
            hypotheses=[
                facts["hypotheses"][candidate.action_id]
                for candidate in candidates
                if candidate.action_id in facts["hypotheses"]
            ],
            information_gains=facts["information_gains"][-100:],
            remaining_steps=max(remaining_steps, 0),
        )
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
            rejection_reason = self._validate_decision(snapshot, context, decision)
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
                "planner_call_count": planner_index,
                "provider_call_count": provider_calls,
                "planner_token_count": state["planner_token_count"] + planner_tokens,
                "planner_latency_ms": (state["planner_latency_ms"] + result.usage.latency_ms),
                "planner_estimated_cost": planner_estimated_cost,
                "planner_failures": 0,
                "decision_history": history,
                "next_action": "finalize",
                "status": "aborted",
                "terminal_reason": "planner cost budget exhausted",
                "stop_reason": "budget_exhausted",
            }
        if decision.action == "finish":
            return {
                "planner_call_count": planner_index,
                "provider_call_count": provider_calls,
                "planner_token_count": state["planner_token_count"] + planner_tokens,
                "planner_latency_ms": (state["planner_latency_ms"] + result.usage.latency_ms),
                "planner_estimated_cost": (planner_estimated_cost),
                "planner_failures": 0,
                "decision_history": history,
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
            "planner_call_count": planner_index,
            "provider_call_count": provider_calls,
            "planner_token_count": state["planner_token_count"] + planner_tokens,
            "planner_latency_ms": state["planner_latency_ms"] + result.usage.latency_ms,
            "planner_estimated_cost": (planner_estimated_cost),
            "planner_failures": 0,
            "decision_history": history,
            "current_case_id": case_id,
            "current_operation_id": case_operation_id,
            "current_step_started_at": datetime.now(UTC).isoformat(),
            "current_step_id": None,
            "evaluation_event_id": None,
            "policy_event_ids": [],
            "approval_id": None,
            "approval_status": None,
            "graph_step_count": state["graph_step_count"] + 1,
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

    async def finish_gate(self, state: AttackGraphState) -> dict[str, Any]:
        external_stop = await self._external_stop(state)
        if external_stop is not None:
            return external_stop
        runtime = self.runtime_registry.get(state["run_id"])
        snapshot = await self.repository.load_candidate_snapshot(
            run_id=state["run_id"],
            snapshot_id=str(state["candidate_snapshot_id"]),
        )
        facts = await self.repository.load_adaptive_facts(state["run_id"])
        approvals = await self.repository.list_approvals(state["run_id"])
        result = self.finish_gate_service.evaluate(
            required_coverage_tags={
                tag
                for case in runtime.cases.values()
                for tag in (case.coverage_tags or [case.category])
            },
            required_control_action_ids={
                case.id for case in runtime.cases.values() if case.kind == CaseKind.control
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

    async def policy_gate(self, state: AttackGraphState) -> dict[str, Any]:
        """在任何 Target 副作用前应用确定性策略、预算与审批事实。"""

        external_stop = await self._external_stop(state)
        if external_stop is not None:
            return external_stop
        runtime = self.runtime_registry.get(state["run_id"])
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
            remaining_steps=runtime.policy.max_steps - state["graph_step_count"] + 1,
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

    async def prepare_human_review(self, state: AttackGraphState) -> dict[str, Any]:
        runtime = self.runtime_registry.get(state["run_id"])
        case = runtime.cases[str(state["current_case_id"])]
        approval = await self.repository.ensure_approval(
            run_id=state["run_id"],
            case=case,
            operation_id=f"{state['current_operation_id']}:approval",
        )
        await self.repository.mark_waiting_approval(
            run_id=state["run_id"],
            approval_id=approval.id,
            checkpoint_ref=str(state["checkpoint_ref"]),
        )
        return {
            "approval_id": approval.id,
            "approval_status": approval.status,
            "status": "waiting_approval",
            "terminal_reason": None,
            "stop_reason": None,
        }

    async def human_review(self, state: AttackGraphState) -> dict[str, Any]:
        approval_id = str(state["approval_id"])
        approval = await self.repository.get_approval(
            run_id=state["run_id"],
            approval_id=approval_id,
        )
        if approval is None:
            raise LookupError(f"approval {approval_id} not found")
        resume_payload = interrupt(
            {
                "approval_id": approval_id,
                "run_id": state["run_id"],
                "case_id": state["current_case_id"],
                "risk_summary": approval.risk_summary,
            }
        )
        resolved = await self.repository.get_approval(
            run_id=state["run_id"],
            approval_id=approval_id,
        )
        return {
            "approval_id": approval_id,
            "approval_status": resolved.status if resolved else "pending",
            "next_action": "policy",
            "status": "running",
            "terminal_reason": None,
            "stop_reason": None,
            "recovery_pending": bool(
                isinstance(resume_payload, dict) and resume_payload.get("recovered")
            ),
        }

    async def planner_pause(self, state: AttackGraphState) -> dict[str, Any]:
        resume_payload = interrupt(
            {
                "run_id": state["run_id"],
                "thread_id": state["thread_id"],
                "fallback": state.get("planner_fallback_snapshot"),
            }
        )
        await self.repository.mark_run_running(
            run_id=state["run_id"],
            operation_id=(f"{state['run_id']}:planner:{state['planner_call_count']}:resumed"),
            event_type="planner_resumed",
        )
        return {
            "status": "running",
            "terminal_reason": None,
            "stop_reason": None,
            "next_action": "build",
            "recovery_pending": bool(
                isinstance(resume_payload, dict) and resume_payload.get("recovered")
            ),
        }

    async def execute(self, state: AttackGraphState) -> dict[str, Any]:
        """使用稳定 operation_id 执行；已持久化结果在恢复时直接复用。"""

        external_stop = await self._external_stop(state)
        if external_stop is not None:
            return external_stop
        runtime = self.runtime_registry.get(state["run_id"])
        case = runtime.cases[str(state["current_case_id"])]
        operation_id = str(state["current_operation_id"])
        step = await self.repository.ensure_step(
            run_id=state["run_id"],
            case_id=case.id,
            operation_id=operation_id,
            sequence=state["graph_step_count"],
        )
        try:
            await self.repository.load_target_execution(operation_id)
            return {"current_step_id": step.id, "next_action": "normalize"}
        except LookupError:
            pass

        request_body, response = await self.connector.execute(
            target=runtime.target,
            case=case,
            operation_id=operation_id,
            approval_id=state.get("approval_id"),
        )
        redacted_fields = set(case.redact_fields)
        secret_values = runtime.secret_values
        sanitized_request = self.trace_adapter.sanitize(
            request_body,
            redacted_fields=redacted_fields,
            secret_values=secret_values,
        )
        sanitized_response = response.model_copy(
            update={
                "body": self.trace_adapter.sanitize(
                    response.body,
                    redacted_fields=redacted_fields,
                    secret_values=secret_values,
                ),
                "text": self.trace_adapter.sanitize(
                    response.text,
                    redacted_fields=redacted_fields,
                    secret_values=secret_values,
                ),
            }
        )
        trace = self.trace_adapter.parse(
            sanitized_response,
            redacted_fields=redacted_fields,
            secret_values=secret_values,
        )
        await self.repository.record_target_execution(
            run_id=state["run_id"],
            step_id=step.id,
            operation_id=operation_id,
            request_body=sanitized_request,
            response=sanitized_response,
            trace_result=trace,
        )
        return {
            "current_step_id": step.id,
            "target_call_count": state["target_call_count"] + 1,
            "next_action": "normalize",
        }

    async def normalize_observation(self, state: AttackGraphState) -> dict[str, Any]:
        operation_id = str(state["current_operation_id"])
        _, response, trace = await self.repository.load_target_execution(operation_id)
        normalized = self.observation_normalizer.normalize_target(
            observation_ref=f"{operation_id}:observation",
            response=response,
            trace=trace,
        )
        observation = await self.repository.record_observation(
            run_id=state["run_id"],
            operation_id=f"{operation_id}:observation",
            source=ObservationSource.target,
            summary=normalized.summary,
            step_id=state.get("current_step_id"),
        )
        return {
            "observation_refs": list(
                dict.fromkeys([*state["observation_refs"], observation.observation_ref])
            ),
            "next_action": "evaluate",
        }

    async def evaluate(self, state: AttackGraphState) -> dict[str, Any]:
        runtime = self.runtime_registry.get(state["run_id"])
        case = runtime.cases[str(state["current_case_id"])]
        operation_id = str(state["current_operation_id"])
        _, response, trace = await self.repository.load_target_execution(operation_id)
        evaluation = self.evaluator.evaluate(
            case=case,
            response=response,
            trace_result=trace,
        )
        event_id = await self.repository.record_evaluation(
            run_id=state["run_id"],
            step_id=str(state["current_step_id"]),
            operation_id=operation_id,
            case_id=case.id,
            evaluation=evaluation,
        )
        return {"evaluation_event_id": event_id, "next_action": "persist"}

    async def persist(self, state: AttackGraphState) -> dict[str, Any]:
        """把 Evaluation、Trace 与 Finding 写入 SQL，checkpoint 不作为事实来源。"""

        runtime = self.runtime_registry.get(state["run_id"])
        case = runtime.cases[str(state["current_case_id"])]
        operation_id = str(state["current_operation_id"])
        request_body, response, trace = await self.repository.load_target_execution(operation_id)
        evaluation = await self.repository.load_evaluation(operation_id)
        policy = PolicyGateResult(
            decision=ToolPolicyDecision(str(state["policy_decision"])),
            reason=str(state["policy_reason"]),
            approval_id=state.get("approval_id"),
        )
        result = GrayBoxExecutionResult(
            case=case,
            request_body=request_body,
            response=response,
            trace=trace.trace,
            evaluation=evaluation,
            policy=policy,
        )
        finding_id = await self.repository.complete_case(
            run_id=state["run_id"],
            operation_id=operation_id,
            result=result,
            policy_event_ids=state["policy_event_ids"],
        )
        findings = state["finding_summaries"]
        if evaluation.violated:
            findings = [
                *findings,
                FindingSummary(
                    finding_ref=finding_id,
                    case_id=case.id,
                    category=case.category,
                    risk_level=evaluation.risk_level,
                    reason=evaluation.reason,
                ).model_dump(mode="json"),
            ]
        finding_refs = state["finding_refs"]
        if finding_id is not None:
            finding_refs = list(dict.fromkeys([*finding_refs, finding_id]))
        return {
            "finding_summaries": findings,
            "finding_refs": finding_refs,
            "next_action": "update_facts",
        }

    async def update_facts(self, state: AttackGraphState) -> dict[str, Any]:
        runtime = self.runtime_registry.get(state["run_id"])
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

    async def skip(self, state: AttackGraphState) -> dict[str, Any]:
        runtime = self.runtime_registry.get(state["run_id"])
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
            sequence=state["graph_step_count"],
            outcome=outcome,
            reason=policy.reason,
            policy=policy,
        )
        return {
            "denied_action_ids": list(dict.fromkeys([*state["denied_action_ids"], case.id])),
            "next_action": "decide",
        }

    async def decide_next(self, state: AttackGraphState) -> dict[str, Any]:
        runtime = self.runtime_registry.get(state["run_id"])
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
                steps_used=min(state["graph_step_count"], runtime.policy.max_steps),
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

    async def finalize(self, state: AttackGraphState) -> dict[str, Any]:
        status = state.get("status", "completed")
        terminal_reason = state.get("terminal_reason") or "planner finished the run"
        await self.repository.finalize_run(
            run_id=state["run_id"],
            status=status,
            terminal_reason=terminal_reason,
            stop_reason=state.get("stop_reason"),
        )
        return {"status": status, "terminal_reason": terminal_reason, "next_action": "done"}

    async def _handle_planner_unavailable(
        self,
        *,
        state: AttackGraphState,
        snapshot: CandidateSnapshot,
        planner_index: int,
        usage: PlannerUsage,
        error_category: str,
        planner_event_id: str,
        decision_history: list[str] | None = None,
    ) -> dict[str, Any]:
        runtime = self.runtime_registry.get(state["run_id"])
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
                steps_used=min(state["graph_step_count"], runtime.policy.max_steps),
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
            await self.repository.pause_run(
                run_id=state["run_id"],
                reason=terminal_reason,
                checkpoint_ref=str(state["checkpoint_ref"]),
                operation_id=f"{state['run_id']}:planner:{planner_index}:paused",
            )
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
            f"{state['run_id']}:case:{case_id}:{len(state['completed_case_ids']) + 1}"
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
            "graph_step_count": state["graph_step_count"] + 1,
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

    async def _external_stop(
        self,
        state: AttackGraphState,
    ) -> dict[str, Any] | None:
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
    def _validate_decision(
        snapshot: CandidateSnapshot,
        context: PlannerContext,
        decision: PlannerDecision,
    ) -> str | None:
        if decision.candidate_snapshot_id != snapshot.snapshot_id:
            return "planner referenced an expired or unknown candidate snapshot"
        empty_run_finish = (
            decision.action == "finish"
            and decision.reason_code == PlannerReasonCode.no_candidates
            and not context.candidates
        )
        if not decision.evidence_refs and not empty_run_finish:
            return "planner decision must cite persisted evidence"
        current_evidence_refs = {
            *context.evidence_refs,
            *context.coverage_refs,
            *context.information_gain_refs,
        }
        missing_evidence = sorted(set(decision.evidence_refs) - current_evidence_refs)
        if missing_evidence:
            return "planner cited evidence outside the current context"
        missing_hypotheses = sorted(set(decision.hypothesis_refs) - set(context.hypothesis_refs))
        if missing_hypotheses:
            return "planner cited hypotheses outside the current context"
        if decision.action == "finish":
            return None
        candidate_ids = [
            candidate.candidate_id
            for candidate in context.candidates
            if candidate.candidate_id == decision.candidate_id
        ]
        if len(candidate_ids) != 1:
            return "planner selected a candidate outside the current snapshot"
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

    @staticmethod
    def _after_plan(state: AttackGraphState) -> str:
        return state["next_action"]

    @staticmethod
    def _after_finish(state: AttackGraphState) -> str:
        return state["next_action"]

    @staticmethod
    def _after_policy(state: AttackGraphState) -> str:
        return state["next_action"]

    @staticmethod
    def _after_execute(state: AttackGraphState) -> str:
        return state["next_action"]

    @staticmethod
    def _after_decide(state: AttackGraphState) -> str:
        return state["next_action"]
