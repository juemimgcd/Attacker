import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langgraph.types import Command

from app.infrastructure.model_adapter import PlannerModelAdapter, create_planner_adapter
from app.repositories.adaptive_repository import AdaptiveRepository
from app.schemas.graybox_schema import (
    AttackPolicy,
    GrayBoxCase,
    GrayBoxExecutionResult,
    GrayBoxOutcome,
    GrayBoxRunRequest,
    LoadedGrayBoxDataset,
    PlannerConfig,
    PolicyGateResult,
    ToolPolicyDecision,
)
from app.schemas.target_schema import TargetConfig
from app.services.graybox_connector import GrayBoxConnector
from app.services.graybox_evaluator_service import GrayBoxEvaluatorService
from app.services.policy_service import PolicyService
from app.services.sample_loader import GrayBoxDatasetLoader
from app.services.tool_trace_adapter import ToolTraceAdapter
from app.workflows.attack_graph import AttackGraph
from app.workflows.attack_state import AttackGraphState


@dataclass
class AdaptiveRuntime:
    target: TargetConfig
    cases: dict[str, Any]
    policy: Any
    planner: PlannerModelAdapter
    started_at: datetime
    secret_values: set[str]


class AdaptiveRuntimeRegistry:
    def __init__(self) -> None:
        self._runtimes: dict[str, AdaptiveRuntime] = {}

    def add(self, run_id: str, runtime: AdaptiveRuntime) -> None:
        self._runtimes[run_id] = runtime

    def get(self, run_id: str) -> AdaptiveRuntime:
        try:
            return self._runtimes[run_id]
        except KeyError as exc:
            raise LookupError(f"runtime for run {run_id} is not loaded") from exc

    def contains(self, run_id: str) -> bool:
        return run_id in self._runtimes


class AdaptiveRunService:
    def __init__(
        self,
        *,
        repository: AdaptiveRepository,
        checkpointer: Any,
    ) -> None:
        self.repository = repository
        self.loader = GrayBoxDatasetLoader()
        self.registry = AdaptiveRuntimeRegistry()
        self.graph = AttackGraph(
            repository=repository,
            runtime_registry=self.registry,
            checkpointer=checkpointer,
        )

    async def start(self, request: GrayBoxRunRequest) -> dict[str, Any]:
        self._validate_target(request.target)
        dataset = await self.loader.load(request.dataset_path, request.case_ids)
        snapshot = self._redacted_target_snapshot(request.target)
        run_id, target_id, thread_id, policy = await self.repository.create_run(
            target_snapshot=snapshot,
            dataset=dataset,
            policy=request.policy,
            mode="adaptive_graybox",
            baseline_run_id=request.baseline_run_id,
            planner_snapshot=self._planner_snapshot(request.planner),
        )
        hypotheses = await self.repository.initialize_hypotheses(
            run_id=run_id,
            cases=dataset.cases,
        )
        self.registry.add(
            run_id,
            AdaptiveRuntime(
                target=request.target,
                cases={case.id: case for case in dataset.cases},
                policy=policy,
                planner=create_planner_adapter(request.planner),
                started_at=datetime.now(UTC),
                secret_values=self._secret_values(request.target),
            ),
        )
        state: AttackGraphState = {
            "run_id": run_id,
            "target_id": target_id,
            "thread_id": thread_id,
            "allowed_case_ids": [case.id for case in dataset.cases],
            "completed_case_ids": [],
            "denied_action_ids": [],
            "candidate_snapshot_id": None,
            "candidate_action_ids": [],
            "coverage": {
                tag: "not_started"
                for case in dataset.cases
                for tag in (case.coverage_tags or [case.category])
            },
            "hypothesis_refs": [fact.hypothesis_ref for fact in hypotheses.values()],
            "observation_refs": [],
            "finding_refs": [],
            "evidence_gaps": [],
            "action_repeat_counts": {},
            "recent_similarity_keys": [],
            "information_gain_refs": [],
            "test_principal_refs": request.test_principal_refs,
            "finding_summaries": [],
            "current_case_id": None,
            "current_operation_id": None,
            "current_step_started_at": None,
            "current_step_id": None,
            "evaluation_event_id": None,
            "policy_decision": None,
            "policy_reason": None,
            "policy_event_ids": [],
            "approval_id": None,
            "approval_status": None,
            "planner_call_count": 0,
            "planner_token_count": 0,
            "planner_failures": 0,
            "decision_history": [],
            "target_call_count": 0,
            "graph_step_count": 0,
            "next_action": "initialize",
            "status": "running",
            "terminal_reason": None,
            "recovery_pending": False,
        }
        result = await self.graph.graph.ainvoke(
            state,
            config={
                "configurable": {"thread_id": thread_id},
                "recursion_limit": policy.max_steps * 10 + 20,
            },
        )
        return await self._status(run_id, result)

    async def resume(
        self,
        *,
        run_id: str,
        approval_id: str,
        approved: bool,
        resolved_by: str,
        reason: str,
        target: TargetConfig | None = None,
        planner: PlannerConfig | None = None,
    ) -> dict[str, Any]:
        run = await self.repository.get_run(run_id)
        if run.thread_id is None:
            raise ValueError("run does not have a resumable thread")
        recovered = False
        if not self.registry.contains(run_id):
            await self._rehydrate_runtime(
                run_id=run_id,
                target_override=target,
                planner_override=planner,
            )
            recovered = True
        approval = await self.repository.resolve_approval(
            run_id=run_id,
            approval_id=approval_id,
            approved=approved,
            resolved_by=resolved_by,
            reason=reason,
        )
        result = await self.graph.graph.ainvoke(
            Command(
                resume={
                    "approval_id": approval_id,
                    "status": approval["status"],
                    "recovered": recovered,
                }
            ),
            config={
                "configurable": {"thread_id": run.thread_id},
                "recursion_limit": 1_000,
            },
        )
        return await self._status(run_id, result)

    async def _rehydrate_runtime(
        self,
        *,
        run_id: str,
        target_override: TargetConfig | None,
        planner_override: PlannerConfig | None,
    ) -> None:
        snapshot = await self.repository.load_runtime_snapshot(run_id)
        stored_target = dict(snapshot["target"])
        if target_override is None:
            if self._contains_redaction(stored_target):
                raise ValueError(
                    "target credentials were redacted; resupply target configuration to resume"
                )
            target = TargetConfig.model_validate(stored_target)
        else:
            if target_override.name != stored_target["name"] or str(
                target_override.endpoint
            ) != str(stored_target["endpoint"]):
                raise ValueError("resupplied target must match the original name and endpoint")
            self._validate_target(target_override)
            target = target_override

        stored_planner = snapshot.get("planner") or {
            "backend": "deterministic",
            "model": "planner",
            "timeout_seconds": 30.0,
        }
        if planner_override is None:
            if stored_planner.get("api_key_required"):
                raise ValueError(
                    "planner credentials were redacted; resupply planner configuration to resume"
                )
            planner_config = PlannerConfig.model_validate(stored_planner)
        else:
            for field in ("backend", "model", "endpoint"):
                stored_value = stored_planner.get(field)
                override_value = planner_override.model_dump(mode="json").get(field)
                if stored_value is not None and stored_value != override_value:
                    raise ValueError(f"resupplied planner {field} must match the original run")
            planner_config = planner_override

        raw_cases = snapshot["dataset"].get("cases", [])
        cases = [GrayBoxCase.model_validate(raw_case["inputs"]) for raw_case in raw_cases]
        started_at = snapshot["started_at"]
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        self.registry.add(
            run_id,
            AdaptiveRuntime(
                target=target,
                cases={case.id: case for case in cases},
                policy=AttackPolicy.model_validate(snapshot["policy"]),
                planner=create_planner_adapter(planner_config),
                started_at=started_at,
                secret_values=self._secret_values(target),
            ),
        )
        await self.repository.append_event(
            run_id=run_id,
            operation_id=f"{run_id}:runtime_rehydrated",
            event_type="runtime_rehydrated",
            evidence={
                "thread_id": snapshot["thread_id"],
                "target_id": snapshot["target_id"],
                "credentials_persisted": False,
            },
        )

    async def _status(self, run_id: str, graph_result: dict[str, Any]) -> dict[str, Any]:
        run = await self.repository.get_run(run_id)
        approvals = await self.repository.list_approvals(run_id)
        return {
            "run_id": run_id,
            "thread_id": run.thread_id,
            "status": run.status,
            "terminal_reason": run.terminal_reason,
            "pending_approvals": [
                approval for approval in approvals if approval["status"] == "pending"
            ],
            "interrupted": bool(graph_result.get("__interrupt__")),
        }

    @staticmethod
    def _secret_values(target: TargetConfig) -> set[str]:
        values = {value for value in target.headers.values() if value}
        if target.auth.token:
            values.add(target.auth.token)
        return values

    @staticmethod
    def _redacted_target_snapshot(target: TargetConfig) -> dict[str, Any]:
        snapshot = target.model_dump(mode="json")
        snapshot["headers"] = {key: "[REDACTED]" for key in target.headers}
        if snapshot["auth"].get("token"):
            snapshot["auth"]["token"] = "[REDACTED]"
        return snapshot

    @staticmethod
    def _planner_snapshot(planner: PlannerConfig) -> dict[str, Any]:
        return {
            "backend": planner.backend,
            "endpoint": str(planner.endpoint) if planner.endpoint else None,
            "model": planner.model,
            "timeout_seconds": planner.timeout_seconds,
            "temperature": planner.temperature,
            "prompt_template_version": planner.prompt_template_version,
            "api_key_required": planner.api_key is not None,
        }

    @classmethod
    def _contains_redaction(cls, value: Any) -> bool:
        if isinstance(value, dict):
            return any(cls._contains_redaction(item) for item in value.values())
        if isinstance(value, list):
            return any(cls._contains_redaction(item) for item in value)
        return value == "[REDACTED]"

    @staticmethod
    def _validate_target(target: TargetConfig) -> None:
        if target.allow_public_target:
            return
        host = target.endpoint.host
        if host is None:
            raise ValueError("target endpoint must include a host")
        if host == "localhost":
            return
        try:
            address = ipaddress.ip_address(host)
            if address.is_private or address.is_loopback:
                return
        except ValueError:
            pass
        raise ValueError(
            "public or unresolved targets require allow_public_target=true and explicit authorization"
        )


class DeterministicGrayBoxRunService:
    def __init__(self, repository: AdaptiveRepository) -> None:
        self.repository = repository
        self.loader = GrayBoxDatasetLoader()
        self.policy_service = PolicyService()
        self.connector = GrayBoxConnector()
        self.trace_adapter = ToolTraceAdapter()
        self.evaluator = GrayBoxEvaluatorService()

    async def run(self, request: GrayBoxRunRequest) -> dict[str, Any]:
        dataset = await self.loader.load(request.dataset_path, request.case_ids)
        return await self.run_dataset(
            target=request.target,
            dataset=dataset,
            policy=request.policy,
            mode="deterministic_graybox",
            baseline_run_id=request.baseline_run_id,
        )

    async def run_dataset(
        self,
        *,
        target: TargetConfig,
        dataset: LoadedGrayBoxDataset,
        policy: AttackPolicy,
        mode: str,
        baseline_run_id: str | None = None,
    ) -> dict[str, Any]:
        AdaptiveRunService._validate_target(target)
        run_id, target_id, _, policy = await self.repository.create_run(
            target_snapshot=AdaptiveRunService._redacted_target_snapshot(target),
            dataset=dataset,
            policy=policy,
            mode=mode,
            baseline_run_id=baseline_run_id,
        )
        started_at = datetime.now(UTC)
        secret_values = AdaptiveRunService._secret_values(target)
        target_calls = 0
        for sequence, case in enumerate(dataset.cases, start=1):
            operation_id = f"{run_id}:case:{case.id}:{sequence}"
            approval_id: str | None = None
            approval_status: str | None = None
            if case.requires_approval or case.severity in policy.approval_required_severities:
                approval = await self.repository.ensure_approval(
                    run_id=run_id,
                    case=case,
                    operation_id=f"{operation_id}:approval",
                )
                approval_id = approval.id
                resolved = await self.repository.resolve_approval(
                    run_id=run_id,
                    approval_id=approval.id,
                    approved=True,
                    resolved_by="deterministic-baseline",
                    reason="pre-authorized isolated sandbox baseline",
                )
                approval_status = str(resolved["status"])
            gate = self.policy_service.evaluate(
                policy=policy,
                target_id=target_id,
                case=case,
                remaining_steps=policy.max_steps - sequence + 1,
                target_call_count=target_calls,
                elapsed_seconds=(datetime.now(UTC) - started_at).total_seconds(),
                approval_status=approval_status,
                approval_id=approval_id,
            )
            policy_event_id = await self.repository.record_policy_result(
                run_id=run_id,
                case_id=case.id,
                operation_id=f"{operation_id}:policy:1",
                result=gate,
            )
            if gate.decision != ToolPolicyDecision.allow:
                await self.repository.complete_skipped_case(
                    run_id=run_id,
                    case=case,
                    operation_id=operation_id,
                    sequence=sequence,
                    outcome=GrayBoxOutcome.policy_denied,
                    reason=gate.reason,
                    policy=gate,
                )
                continue
            step = await self.repository.ensure_step(
                run_id=run_id,
                case_id=case.id,
                operation_id=operation_id,
                sequence=sequence,
            )
            request_body, response = await self.connector.execute(
                target=target,
                case=case,
                operation_id=operation_id,
                approval_id=approval_id,
            )
            fields = set(case.redact_fields)
            sanitized_request = self.trace_adapter.sanitize(
                request_body,
                redacted_fields=fields,
                secret_values=secret_values,
            )
            sanitized_response = response.model_copy(
                update={
                    "body": self.trace_adapter.sanitize(
                        response.body,
                        redacted_fields=fields,
                        secret_values=secret_values,
                    ),
                    "text": self.trace_adapter.sanitize(
                        response.text,
                        redacted_fields=fields,
                        secret_values=secret_values,
                    ),
                }
            )
            trace = self.trace_adapter.parse(
                sanitized_response,
                redacted_fields=fields,
                secret_values=secret_values,
            )
            await self.repository.record_target_execution(
                run_id=run_id,
                step_id=step.id,
                operation_id=operation_id,
                request_body=sanitized_request,
                response=sanitized_response,
                trace_result=trace,
            )
            target_calls += 1
            evaluation = self.evaluator.evaluate(
                case=case,
                response=sanitized_response,
                trace_result=trace,
            )
            await self.repository.record_evaluation(
                run_id=run_id,
                step_id=step.id,
                operation_id=operation_id,
                case_id=case.id,
                evaluation=evaluation,
            )
            await self.repository.complete_case(
                run_id=run_id,
                operation_id=operation_id,
                result=GrayBoxExecutionResult(
                    case=case,
                    request_body=sanitized_request,
                    response=sanitized_response,
                    trace=trace.trace,
                    evaluation=evaluation,
                    policy=PolicyGateResult.model_validate(gate),
                ),
                policy_event_ids=[policy_event_id],
            )
        await self.repository.finalize_run(
            run_id=run_id,
            status="completed",
            terminal_reason="deterministic gray-box case order completed",
        )
        run = await self.repository.get_run(run_id)
        return {
            "run_id": run_id,
            "thread_id": run.thread_id,
            "status": run.status,
            "terminal_reason": run.terminal_reason,
        }
