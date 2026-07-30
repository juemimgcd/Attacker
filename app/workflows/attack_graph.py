import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import httpx
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.repositories.adaptive_repository import AdaptiveRepository
from app.schemas.adaptive_agent_schema import (
    CandidateSnapshot,
    ObservationSource,
    PlannerReasonCode,
)
from app.schemas.attack_sample_schema import CaseKind
from app.schemas.attack_state_schema import CoverageStatus, RunBudgetSnapshot
from app.schemas.graybox_schema import (
    FindingSummary,
    GrayBoxExecutionResult,
    GrayBoxOutcome,
    PlannerContext,
    PlannerDecision,
    PolicyGateResult,
    ToolPolicyDecision,
)
from app.services.candidate_builder import CandidateBuilder
from app.services.finish_gate_service import FinishGateService
from app.services.graybox_connector import GrayBoxConnector
from app.services.graybox_evaluator_service import GrayBoxEvaluatorService
from app.services.hypothesis_service import HypothesisService
from app.services.observation_normalizer import ObservationNormalizer
from app.services.policy_service import PolicyService
from app.services.tool_trace_adapter import ToolTraceAdapter
from app.workflows.attack_state import AttackGraphState


class AttackGraph:
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
        self.connector = GrayBoxConnector()
        self.trace_adapter = ToolTraceAdapter()
        self.evaluator = GrayBoxEvaluatorService()
        self.graph = self._build().compile(checkpointer=checkpointer)

    def _build(self) -> StateGraph:
        builder = StateGraph(AttackGraphState)
        builder.add_node("initialize_run", self.initialize_run)
        builder.add_node("build_candidates", self.build_candidates)
        builder.add_node("plan_next_case", self.plan_next_case)
        builder.add_node("finish_gate", self.finish_gate)
        builder.add_node("policy_gate", self.policy_gate)
        builder.add_node("human_review", self.human_review)
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
            {"review": "human_review", "execute": "execute", "skip": "skip"},
        )
        builder.add_edge("human_review", "policy_gate")
        builder.add_edge("execute", "normalize_observation")
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
            "last_state_fingerprint": state.get("last_state_fingerprint"),
            "consecutive_no_gain_steps": state.get("consecutive_no_gain_steps", 0),
            "repeated_state_count": state.get("repeated_state_count", 0),
            "test_principal_refs": state.get(
                "test_principal_refs",
                ["default-test-principal"],
            ),
            "finding_summaries": state.get("finding_summaries", []),
            "policy_event_ids": [],
            "planner_call_count": state.get("planner_call_count", 0),
            "planner_token_count": state.get("planner_token_count", 0),
            "planner_failures": state.get("planner_failures", 0),
            "decision_history": state.get("decision_history", []),
            "target_call_count": state.get("target_call_count", 0),
            "graph_step_count": state.get("graph_step_count", 0),
            "status": "running",
            "next_action": "plan",
            "recovery_pending": state.get("recovery_pending", False),
        }

    async def build_candidates(self, state: AttackGraphState) -> dict[str, Any]:
        runtime = self.runtime_registry.get(state["run_id"])
        facts = await self.repository.load_adaptive_facts(state["run_id"])
        elapsed = (datetime.now(UTC) - runtime.started_at).total_seconds()
        budget = RunBudgetSnapshot(
            max_steps=runtime.policy.max_steps,
            max_target_calls=runtime.policy.max_target_calls,
            max_provider_calls=0,
            max_duration_seconds=runtime.policy.max_duration_seconds,
            steps_used=min(state["graph_step_count"], runtime.policy.max_steps),
            target_calls_used=min(
                state["target_call_count"],
                runtime.policy.max_target_calls,
            ),
            elapsed_seconds=min(elapsed, runtime.policy.max_duration_seconds),
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
        await self.repository.record_candidate_snapshot(snapshot)
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
        runtime = self.runtime_registry.get(state["run_id"])
        elapsed = (datetime.now(UTC) - runtime.started_at).total_seconds()
        if elapsed >= runtime.policy.max_duration_seconds:
            return {
                "next_action": "finalize",
                "status": "aborted",
                "terminal_reason": "run duration budget exhausted",
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
        persisted_outcome = await self.repository.load_planner_outcome(
            run_id=state["run_id"],
            operation_id=operation_id,
        )
        if persisted_outcome and persisted_outcome["event_type"] == "planner_error":
            failures = state["planner_failures"] + 1
            terminal = failures >= runtime.policy.max_planner_failures
            return {
                "planner_call_count": planner_index,
                "planner_failures": failures,
                "next_action": "finalize" if terminal else "retry",
                "status": "aborted" if terminal else "running",
                "terminal_reason": "planner failure limit exhausted" if terminal else None,
            }
        if persisted_outcome is None:
            try:
                result = await runtime.planner.plan(context)
            except (
                httpx.HTTPError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
                IndexError,
            ) as exc:
                await self.repository.record_planner_error(
                    run_id=state["run_id"],
                    operation_id=operation_id,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                failures = state["planner_failures"] + 1
                terminal = failures >= runtime.policy.max_planner_failures
                return {
                    "planner_call_count": planner_index,
                    "planner_failures": failures,
                    "next_action": "finalize" if terminal else "retry",
                    "status": "aborted" if terminal else "running",
                    "terminal_reason": ("planner failure limit exhausted" if terminal else None),
                }
        else:
            result = persisted_outcome["result"]

        decision = result.decision
        if persisted_outcome and persisted_outcome["event_type"] == "planner_rejected":
            rejection_reason = (
                persisted_outcome.get("rejection_reason") or "persisted planner rejection"
            )
        else:
            rejection_reason = self._validate_decision(snapshot, context, decision)
            if rejection_reason is None and (decision.evidence_refs or decision.hypothesis_refs):
                rejection_reason = await self.repository.validate_planner_references(
                    run_id=state["run_id"],
                    evidence_refs=decision.evidence_refs,
                    hypothesis_refs=decision.hypothesis_refs,
                )
        history = [
            *state["decision_history"],
            decision.candidate_id or decision.action,
        ]
        repeated = Counter(history)[history[-1]]
        if repeated > runtime.policy.max_repeated_decisions:
            rejection_reason = "planner repeated the same decision beyond the configured limit"
        await self.repository.record_planner_result(
            run_id=state["run_id"],
            operation_id=operation_id,
            result=result,
            accepted=rejection_reason is None,
            rejection_reason=rejection_reason,
        )
        planner_tokens = result.usage.input_tokens + result.usage.output_tokens
        if rejection_reason is not None:
            failures = state["planner_failures"] + 1
            terminal = failures >= runtime.policy.max_planner_failures or repeated > (
                runtime.policy.max_repeated_decisions
            )
            return {
                "planner_call_count": planner_index,
                "planner_token_count": state["planner_token_count"] + planner_tokens,
                "planner_failures": failures,
                "decision_history": history,
                "next_action": "finalize" if terminal else "retry",
                "status": "aborted" if terminal else "running",
                "terminal_reason": rejection_reason if terminal else None,
            }
        if decision.action == "finish":
            return {
                "planner_call_count": planner_index,
                "planner_token_count": state["planner_token_count"] + planner_tokens,
                "decision_history": history,
                "next_action": "finish",
                "status": "running",
                "terminal_reason": None,
            }
        candidate = next(
            item for item in context.candidates if item.candidate_id == decision.candidate_id
        )
        case_id = candidate.action_id
        action_attempt = state["action_repeat_counts"].get(case_id, 0) + 1
        return {
            "planner_call_count": planner_index,
            "planner_token_count": state["planner_token_count"] + planner_tokens,
            "decision_history": history,
            "current_case_id": case_id,
            "current_operation_id": f"{state['run_id']}:case:{case_id}:{action_attempt}",
            "current_step_started_at": datetime.now(UTC).isoformat(),
            "current_step_id": None,
            "evaluation_event_id": None,
            "policy_event_ids": [],
            "approval_id": None,
            "approval_status": None,
            "graph_step_count": state["graph_step_count"] + 1,
            "next_action": "policy",
        }

    async def finish_gate(self, state: AttackGraphState) -> dict[str, Any]:
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
            }
        return {
            "next_action": "build",
            "status": "running",
            "terminal_reason": None,
        }

    async def policy_gate(self, state: AttackGraphState) -> dict[str, Any]:
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

    async def human_review(self, state: AttackGraphState) -> dict[str, Any]:
        runtime = self.runtime_registry.get(state["run_id"])
        case = runtime.cases[str(state["current_case_id"])]
        approval = await self.repository.ensure_approval(
            run_id=state["run_id"],
            case=case,
            operation_id=f"{state['current_operation_id']}:approval",
        )
        resume_payload = interrupt(
            {
                "approval_id": approval.id,
                "run_id": state["run_id"],
                "case_id": case.id,
                "risk_summary": approval.risk_summary,
            }
        )
        resolved = await self.repository.get_approval(
            run_id=state["run_id"],
            approval_id=approval.id,
        )
        return {
            "approval_id": approval.id,
            "approval_status": resolved.status if resolved else "pending",
            "next_action": "policy",
            "recovery_pending": bool(
                isinstance(resume_payload, dict) and resume_payload.get("recovered")
            ),
        }

    async def execute(self, state: AttackGraphState) -> dict[str, Any]:
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
            return {"current_step_id": step.id, "next_action": "evaluate"}
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
        _, gain_ref, persisted_gain = await self.repository.record_coverage_and_gain(
            run_id=state["run_id"],
            operation_id=operation_id,
            coverage_facts=coverage_facts,
            gain=gain,
            step_id=state.get("current_step_id"),
        )
        updated_facts = await self.repository.load_adaptive_facts(state["run_id"])
        state_fingerprint = self._state_fingerprint(updated_facts)
        has_information_gain = any(
            (
                persisted_gain.coverage_delta > 0,
                persisted_gain.evidence_completeness_delta > 0,
                persisted_gain.confirmed_finding_delta > 0,
            )
        )
        repeated_state_count = (
            state["repeated_state_count"] + 1
            if state.get("last_state_fingerprint") == state_fingerprint
            else 0
        )
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
            "last_state_fingerprint": state_fingerprint,
            "consecutive_no_gain_steps": (
                0 if has_information_gain else state["consecutive_no_gain_steps"] + 1
            ),
            "repeated_state_count": repeated_state_count,
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
        terminal_reason: str | None = None
        status = "running"
        if state["graph_step_count"] >= runtime.policy.max_steps:
            terminal_reason = "graph step budget exhausted"
            status = "aborted"
        elif (
            datetime.now(UTC) - runtime.started_at
        ).total_seconds() >= runtime.policy.max_duration_seconds:
            terminal_reason = "run duration budget exhausted"
            status = "aborted"
        elif state["target_call_count"] >= runtime.policy.max_target_calls:
            terminal_reason = "target call budget exhausted"
            status = "aborted"
        elif state["consecutive_no_gain_steps"] >= runtime.policy.max_consecutive_no_gain_steps:
            terminal_reason = "consecutive no-information-gain limit reached"
            status = "aborted"
        elif state["repeated_state_count"] >= runtime.policy.max_repeated_states:
            terminal_reason = "repeated normalized state limit reached"
            status = "aborted"
        elif runtime.policy.stop_on_critical and any(
            summary["risk_level"] == "critical" for summary in state["finding_summaries"]
        ):
            terminal_reason = "critical finding stop policy triggered"
            status = "completed"
        return {
            "completed_case_ids": completed,
            "action_repeat_counts": repeat_counts,
            "recent_similarity_keys": recent_similarity_keys,
            "next_action": "finalize" if terminal_reason else "build",
            "status": status,
            "terminal_reason": terminal_reason,
        }

    async def finalize(self, state: AttackGraphState) -> dict[str, Any]:
        status = state.get("status", "completed")
        terminal_reason = state.get("terminal_reason") or "planner finished the run"
        await self.repository.finalize_run(
            run_id=state["run_id"],
            status=status,
            terminal_reason=terminal_reason,
        )
        return {"status": status, "terminal_reason": terminal_reason, "next_action": "done"}

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
    def _state_fingerprint(facts: dict[str, Any]) -> str:
        latest_observation = facts["observations"][-1] if facts["observations"] else None
        latest_gain = facts["information_gains"][-1] if facts["information_gains"] else None
        payload = {
            "coverage": {tag: status.value for tag, status in sorted(facts["coverage"].items())},
            "hypotheses": {
                action_id: hypothesis.status.value
                for action_id, hypothesis in sorted(facts["hypotheses"].items())
            },
            "evidence_gaps": sorted(facts["evidence_gaps"]),
            "latest_observation": (
                {
                    "source": latest_observation.source.value,
                    "summary": latest_observation.summary,
                }
                if latest_observation
                else None
            ),
            "latest_information_gain": (
                {
                    "coverage_delta": latest_gain.coverage_delta,
                    "evidence_completeness_delta": (latest_gain.evidence_completeness_delta),
                    "confirmed_finding_delta": latest_gain.confirmed_finding_delta,
                }
                if latest_gain
                else None
            ),
            "finding_fingerprints": facts["finding_fingerprints"],
        }
        canonical = json.dumps(
            payload,
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
    def _after_decide(state: AttackGraphState) -> str:
        return state["next_action"]
