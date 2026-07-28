from collections import Counter
from datetime import UTC, datetime
from typing import Any

import httpx
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.repositories.adaptive_repository import AdaptiveRepository
from app.schemas.graybox_schema import (
    CaseSummary,
    FindingSummary,
    GrayBoxExecutionResult,
    GrayBoxOutcome,
    PlannerContext,
    PolicyGateResult,
    ToolPolicyDecision,
)
from app.services.graybox_connector import GrayBoxConnector
from app.services.graybox_evaluator_service import GrayBoxEvaluatorService
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
        self.connector = GrayBoxConnector()
        self.trace_adapter = ToolTraceAdapter()
        self.evaluator = GrayBoxEvaluatorService()
        self.graph = self._build().compile(checkpointer=checkpointer)

    def _build(self) -> StateGraph:
        builder = StateGraph(AttackGraphState)
        builder.add_node("initialize_run", self.initialize_run)
        builder.add_node("plan_next_case", self.plan_next_case)
        builder.add_node("policy_gate", self.policy_gate)
        builder.add_node("human_review", self.human_review)
        builder.add_node("execute", self.execute)
        builder.add_node("skip", self.skip)
        builder.add_node("evaluate", self.evaluate)
        builder.add_node("persist", self.persist)
        builder.add_node("decide_next", self.decide_next)
        builder.add_node("finalize", self.finalize)
        builder.add_edge(START, "initialize_run")
        builder.add_edge("initialize_run", "plan_next_case")
        builder.add_conditional_edges(
            "plan_next_case",
            self._after_plan,
            {"policy": "policy_gate", "retry": "plan_next_case", "finalize": "finalize"},
        )
        builder.add_conditional_edges(
            "policy_gate",
            self._after_policy,
            {"review": "human_review", "execute": "execute", "skip": "skip"},
        )
        builder.add_edge("human_review", "policy_gate")
        builder.add_edge("execute", "evaluate")
        builder.add_edge("evaluate", "persist")
        builder.add_edge("persist", "decide_next")
        builder.add_edge("skip", "decide_next")
        builder.add_conditional_edges(
            "decide_next",
            self._after_decide,
            {"plan": "plan_next_case", "finalize": "finalize"},
        )
        builder.add_edge("finalize", END)
        return builder

    async def initialize_run(self, state: AttackGraphState) -> dict[str, Any]:
        return {
            "completed_case_ids": state.get("completed_case_ids", []),
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
        context = PlannerContext(
            allowed_cases=[
                CaseSummary(
                    id=case.id,
                    name=case.name,
                    category=case.category,
                    severity=case.severity,
                    requires_approval=case.requires_approval,
                )
                for case in runtime.cases.values()
                if case.id in state["allowed_case_ids"]
            ],
            completed_case_ids=state["completed_case_ids"],
            finding_summaries=[
                FindingSummary.model_validate(summary) for summary in state["finding_summaries"]
            ],
            remaining_steps=max(remaining_steps, 0),
        )
        planner_index = state["planner_call_count"] + 1
        operation_id = f"{state['run_id']}:planner:{planner_index}"
        try:
            result = await runtime.planner.plan(context)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
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
                "terminal_reason": "planner failure limit exhausted" if terminal else None,
            }

        decision = result.decision
        rejection_reason = self._validate_decision(state, decision.case_id, decision.action)
        history = [*state["decision_history"], decision.case_id or decision.action]
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
        if decision.action == "finish_run":
            return {
                "planner_call_count": planner_index,
                "planner_token_count": state["planner_token_count"] + planner_tokens,
                "decision_history": history,
                "next_action": "finalize",
                "status": "completed",
                "terminal_reason": decision.reason,
            }
        case_id = str(decision.case_id)
        return {
            "planner_call_count": planner_index,
            "planner_token_count": state["planner_token_count"] + planner_tokens,
            "decision_history": history,
            "current_case_id": case_id,
            "current_operation_id": (
                f"{state['run_id']}:case:{case_id}:{len(state['completed_case_ids']) + 1}"
            ),
            "current_step_id": None,
            "evaluation_event_id": None,
            "policy_event_ids": [],
            "approval_id": None,
            "approval_status": None,
            "graph_step_count": state["graph_step_count"] + 1,
            "next_action": "policy",
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
        next_action = {
            ToolPolicyDecision.allow: "execute",
            ToolPolicyDecision.deny: "skip",
            ToolPolicyDecision.approval_required: "review",
        }[result.decision]
        return {
            "policy_decision": result.decision.value,
            "policy_reason": result.reason,
            "policy_event_ids": [*state["policy_event_ids"], event_id],
            "approval_id": approval_id,
            "approval_status": approval_status,
            "next_action": next_action,
        }

    async def human_review(self, state: AttackGraphState) -> dict[str, Any]:
        runtime = self.runtime_registry.get(state["run_id"])
        case = runtime.cases[str(state["current_case_id"])]
        approval = await self.repository.ensure_approval(
            run_id=state["run_id"],
            case=case,
            operation_id=f"{state['current_operation_id']}:approval",
        )
        interrupt(
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
        }

    async def execute(self, state: AttackGraphState) -> dict[str, Any]:
        runtime = self.runtime_registry.get(state["run_id"])
        case = runtime.cases[str(state["current_case_id"])]
        operation_id = str(state["current_operation_id"])
        step = await self.repository.ensure_step(
            run_id=state["run_id"],
            case_id=case.id,
            operation_id=operation_id,
            sequence=len(state["completed_case_ids"]) + 1,
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
        await self.repository.complete_case(
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
                    case_id=case.id,
                    category=case.category,
                    risk_level=evaluation.risk_level,
                    reason=evaluation.reason,
                ).model_dump(mode="json"),
            ]
        return {"finding_summaries": findings, "next_action": "decide"}

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
            sequence=len(state["completed_case_ids"]) + 1,
            outcome=outcome,
            reason=policy.reason,
            policy=policy,
        )
        return {"next_action": "decide"}

    async def decide_next(self, state: AttackGraphState) -> dict[str, Any]:
        runtime = self.runtime_registry.get(state["run_id"])
        case_id = str(state["current_case_id"])
        completed = list(dict.fromkeys([*state["completed_case_ids"], case_id]))
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
        elif len(completed) >= len(state["allowed_case_ids"]):
            terminal_reason = "all allowed cases completed"
            status = "completed"
        elif runtime.policy.stop_on_critical and any(
            summary["risk_level"] == "critical" for summary in state["finding_summaries"]
        ):
            terminal_reason = "critical finding stop policy triggered"
            status = "completed"
        return {
            "completed_case_ids": completed,
            "next_action": "finalize" if terminal_reason else "plan",
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
        state: AttackGraphState,
        case_id: str | None,
        action: str,
    ) -> str | None:
        if action == "finish_run":
            return None
        if case_id is None:
            return "planner omitted case_id"
        if case_id not in state["allowed_case_ids"]:
            return "planner selected a case outside the allowlist"
        if case_id in state["completed_case_ids"]:
            return "planner selected an already completed case"
        return None

    @staticmethod
    def _after_plan(state: AttackGraphState) -> str:
        return state["next_action"]

    @staticmethod
    def _after_policy(state: AttackGraphState) -> str:
        return state["next_action"]

    @staticmethod
    def _after_decide(state: AttackGraphState) -> str:
        return state["next_action"]
