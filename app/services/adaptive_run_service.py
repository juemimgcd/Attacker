"""创建、暂停、恢复和控制自适应/灰盒 Run，并管理瞬时运行时凭据。"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from langgraph.types import Command

from app.infrastructure.model_adapter import PlannerModelAdapter, create_planner_adapter
from app.repositories.adaptive_repository import AdaptiveRepository
from app.schemas.adaptive_agent_schema import ObservationSource
from app.schemas.graybox_schema import (
    AdaptiveControlAction,
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
from app.services.observation_normalizer import ObservationNormalizer
from app.services.policy_service import PolicyService
from app.services.sample_loader import GrayBoxDatasetLoader
from app.services.target_binding import canonical_target_binding, canonical_target_ref
from app.services.tool_trace_adapter import ToolTraceAdapter
from app.workflows.attack_graph import AttackGraph
from app.workflows.attack_state import AttackGraphState

if TYPE_CHECKING:
    from app.services.equipment_service import EquipmentService


@dataclass
class AdaptiveRuntime:
    """仅驻留内存的 Target 与 Planner 运行时对象；Secret 不进入 checkpoint。"""

    target: TargetConfig
    cases: dict[str, Any]
    policy: Any
    planner: PlannerModelAdapter
    started_at: datetime
    secret_values: set[str]


class AdaptiveRuntimeRegistry:
    """按 run_id 保存当前进程可用的瞬时运行时，重启后必须重新构建。"""

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
    """冻结输入并驱动 LangGraph；SQL 记录事实，checkpoint 记录继续位置。"""

    def __init__(
        self,
        *,
        repository: AdaptiveRepository,
        checkpointer: Any,
        equipment_service: EquipmentService | None = None,
    ) -> None:
        self.repository = repository
        self.equipment_service = equipment_service
        self.loader = GrayBoxDatasetLoader()
        self.registry = AdaptiveRuntimeRegistry()
        self.graph = AttackGraph(
            repository=repository,
            runtime_registry=self.registry,
            checkpointer=checkpointer,
        )

    async def start(self, request: GrayBoxRunRequest) -> dict[str, Any]:
        """创建 Run、冻结绑定和候选宇宙，然后启动同一 thread 的图执行。"""

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
            test_principal_refs=request.test_principal_refs,
            evaluator_snapshot=self._evaluator_snapshot(),
            candidate_universe_checksum=self._candidate_universe_checksum(dataset.cases),
            equipment_snapshot=self._equipment_snapshot(dataset.cases),
        )
        await self._freeze_equipment(
            run_id,
            request.target,
            request.test_principal_refs,
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
            "goal_id": f"adaptive_graybox:{run_id}",
            "target_id": target_id,
            "thread_id": thread_id,
            "checkpoint_ref": f"langgraph:{thread_id}",
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
            "expected_information_gain": None,
            "last_coverage_delta": 0,
            "last_evidence_delta": 0,
            "last_finding_delta": 0,
            "last_target_transport_failed": False,
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
            "provider_call_count": 0,
            "planner_token_count": 0,
            "planner_latency_ms": 0,
            "planner_estimated_cost": 0,
            "planner_failures": 0,
            "planner_fallback_snapshot": None,
            "decision_history": [],
            "target_call_count": 0,
            "target_transport_failure_count": 0,
            "graph_step_count": 0,
            "last_state_fingerprint": None,
            "repeated_state_count": 0,
            "consecutive_no_gain_steps": 0,
            "next_action": "initialize",
            "status": "running",
            "terminal_reason": None,
            "stop_reason": None,
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

    async def _freeze_equipment(
        self,
        run_id: str,
        target: TargetConfig,
        test_principal_refs: list[str],
    ) -> None:
        if self.equipment_service is None:
            return
        principal_ref = test_principal_refs[0] if test_principal_refs else "default-test-principal"
        await self.equipment_service.freeze_run_bindings(
            run_id=run_id,
            stage="graybox",
            target_binding_ref=canonical_target_ref(target),
            test_principal_ref=principal_ref,
        )

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
        """处理审批并恢复；运行时凭据必须重新提供且与冻结绑定一致。"""

        run = await self.repository.get_run(run_id)
        if run.status not in {"waiting_approval", "running"}:
            raise ValueError("run is not waiting for approval")
        if run.thread_id is None:
            raise ValueError("run does not have a resumable thread")
        await self._require_checkpoint_node(
            thread_id=run.thread_id,
            node_name="human_review",
        )
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

    async def resume_paused(
        self,
        *,
        run_id: str,
        target: TargetConfig | None = None,
        planner: PlannerConfig | None = None,
    ) -> dict[str, Any]:
        run = await self.repository.get_run(run_id)
        if run.status not in {"paused", "running"}:
            raise ValueError("run is not paused")
        if run.thread_id is None:
            raise ValueError("run does not have a resumable thread")
        await self._require_checkpoint_node(
            thread_id=run.thread_id,
            node_name="planner_pause",
        )
        recovered = False
        if not self.registry.contains(run_id) or target is not None or planner is not None:
            await self._rehydrate_runtime(
                run_id=run_id,
                target_override=target,
                planner_override=planner,
            )
            recovered = True
        result = await self.graph.graph.ainvoke(
            Command(resume={"recovered": recovered}),
            config={
                "configurable": {"thread_id": run.thread_id},
                "recursion_limit": 1_000,
            },
        )
        return await self._status(run_id, result)

    async def request_control(
        self,
        *,
        run_id: str,
        action: AdaptiveControlAction,
        reason: str,
    ) -> dict[str, Any]:
        run = await self.repository.get_run(run_id)
        event_id = await self.repository.record_control_request(
            run_id=run_id,
            action=action,
            reason=reason,
        )
        if run.status in {"waiting_approval", "paused"}:
            stop_reason = (
                "cancelled" if action == AdaptiveControlAction.cancel else "policy_terminated"
            )
            status = "cancelled" if action == AdaptiveControlAction.cancel else "aborted"
            await self.repository.finalize_run(
                run_id=run_id,
                status=status,
                terminal_reason=reason,
                stop_reason=stop_reason,
            )
        current = await self.repository.get_run(run_id)
        return {
            "run_id": run_id,
            "action": action.value,
            "reason": reason,
            "event_id": event_id,
            "status": current.status,
        }

    async def _require_checkpoint_node(
        self,
        *,
        thread_id: str,
        node_name: str,
    ) -> None:
        snapshot = await self.graph.graph.aget_state({"configurable": {"thread_id": thread_id}})
        if node_name not in snapshot.next:
            raise ValueError(f"run checkpoint is not waiting at {node_name}")

    async def _rehydrate_runtime(
        self,
        *,
        run_id: str,
        target_override: TargetConfig | None,
        planner_override: PlannerConfig | None,
    ) -> None:
        """从 SQL 快照重建非 Secret 配置，并验证调用方重新提供的凭据。"""

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
            for field in (
                "backend",
                "provider_id",
                "model",
                "endpoint",
                "prompt_template_version",
            ):
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
        snapshot = canonical_target_binding(target)
        if snapshot is None:
            raise ValueError("target binding is required")
        return snapshot

    @staticmethod
    def _planner_snapshot(planner: PlannerConfig) -> dict[str, Any]:
        return {
            "backend": planner.backend,
            "endpoint": str(planner.endpoint) if planner.endpoint else None,
            "model": planner.model,
            "provider_id": planner.provider_id,
            "timeout_seconds": planner.timeout_seconds,
            "temperature": planner.temperature,
            "max_physical_attempts": planner.max_physical_attempts,
            "prompt_template_version": planner.prompt_template_version,
            "api_key_required": planner.api_key is not None,
        }

    @staticmethod
    def _evaluator_snapshot() -> dict[str, str]:
        return {
            "evaluator_id": "core.graybox_evaluator",
            "evaluator_version": "1.0.0",
        }

    @staticmethod
    def _candidate_universe_checksum(cases: list[GrayBoxCase]) -> str:
        payload = [
            {
                "id": case.id,
                "enabled": case.enabled,
                "compatible": case.compatible,
                "provider_instance_ref": case.provider_instance_ref,
                "capability_contract": case.capability_contract,
                "coverage_tags": sorted(case.coverage_tags or [case.category]),
            }
            for case in sorted(cases, key=lambda item: item.id)
        ]
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _equipment_snapshot(cases: list[GrayBoxCase]) -> list[dict[str, str]]:
        return [
            {
                "provider_instance_ref": provider_instance_ref,
                "capability_contract": capability_contract,
            }
            for provider_instance_ref, capability_contract in sorted(
                {(case.provider_instance_ref, case.capability_contract) for case in cases}
            )
        ]

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
    """固定顺序执行灰盒 Case，用作自适应模式的可比较基线。"""

    def __init__(
        self,
        repository: AdaptiveRepository,
        *,
        equipment_service: EquipmentService | None = None,
    ) -> None:
        self.repository = repository
        self.equipment_service = equipment_service
        self.loader = GrayBoxDatasetLoader()
        self.policy_service = PolicyService()
        self.connector = GrayBoxConnector()
        self.trace_adapter = ToolTraceAdapter()
        self.evaluator = GrayBoxEvaluatorService()
        self.observation_normalizer = ObservationNormalizer()

    async def run(self, request: GrayBoxRunRequest) -> dict[str, Any]:
        dataset = await self.loader.load(request.dataset_path, request.case_ids)
        return await self.run_dataset(
            target=request.target,
            dataset=dataset,
            policy=request.policy,
            mode="deterministic_graybox",
            baseline_run_id=request.baseline_run_id,
            test_principal_refs=request.test_principal_refs,
        )

    async def run_dataset(
        self,
        *,
        target: TargetConfig,
        dataset: LoadedGrayBoxDataset,
        policy: AttackPolicy,
        mode: str,
        baseline_run_id: str | None = None,
        test_principal_refs: list[str] | None = None,
        equipment_source_run_id: str | None = None,
        equipment_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        AdaptiveRunService._validate_target(target)
        run_id, target_id, _, policy = await self.repository.create_run(
            target_snapshot=AdaptiveRunService._redacted_target_snapshot(target),
            dataset=dataset,
            policy=policy,
            mode=mode,
            baseline_run_id=baseline_run_id,
            test_principal_refs=test_principal_refs or ["default-test-principal"],
            evaluator_snapshot=AdaptiveRunService._evaluator_snapshot(),
            candidate_universe_checksum=(
                AdaptiveRunService._candidate_universe_checksum(dataset.cases)
            ),
            equipment_snapshot=AdaptiveRunService._equipment_snapshot(dataset.cases),
        )
        if self.equipment_service is not None:
            if equipment_source_run_id is not None:
                await self.equipment_service.clone_run_bindings(
                    source_run_id=equipment_source_run_id,
                    target_run_id=run_id,
                )
            else:
                principal_refs = test_principal_refs or ["default-test-principal"]
                await self.equipment_service.freeze_run_bindings(
                    run_id=run_id,
                    stage="graybox",
                    target_binding_ref=canonical_target_ref(target),
                    test_principal_ref=principal_refs[0],
                    overrides=equipment_overrides,
                )
        started_at = datetime.now(UTC)
        secret_values = AdaptiveRunService._secret_values(target)
        target_calls = 0
        for sequence, case in enumerate(dataset.cases, start=1):
            operation_id = f"{run_id}:case:{case.id}:{sequence}"
            await self.repository.append_event(
                run_id=run_id,
                operation_id=f"{operation_id}:decision",
                event_type="decision_bound",
                evidence={
                    "decision_source": "deterministic_case_order",
                    "case_id": case.id,
                },
            )
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
            normalized = self.observation_normalizer.normalize_target(
                observation_ref=f"{operation_id}:observation",
                response=sanitized_response,
                trace=trace,
            )
            await self.repository.record_observation(
                run_id=run_id,
                operation_id=f"{operation_id}:observation",
                source=ObservationSource.target,
                summary=normalized.summary,
                step_id=step.id,
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
