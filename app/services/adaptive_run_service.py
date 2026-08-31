"""创建、暂停、恢复和控制自适应/灰盒 Run，并管理瞬时运行时凭据。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from asyncio import CancelledError, Lock
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from weakref import WeakValueDictionary

from langgraph.types import Command
from loguru import logger

from app.equipment.security import SecretBroker, validate_target_url
from app.infrastructure.model_adapter import PlannerModelAdapter, create_planner_adapter
from app.repositories.adaptive_repository import AdaptiveRepository
from app.schemas.graybox_schema import (
    AdaptiveControlAction,
    AttackPolicy,
    DeterministicGrayBoxRunRequest,
    GrayBoxCase,
    GrayBoxOutcome,
    GrayBoxRunRequest,
    LoadedGrayBoxDataset,
    PlannerConfig,
    PolicyGateResult,
    ToolPolicyDecision,
)
from app.schemas.target_schema import TargetConfig
from app.services.graybox_case_pipeline import GrayBoxCasePipeline
from app.services.graybox_connector import GrayBoxConnector
from app.services.policy_service import PolicyService
from app.services.run_lifecycle import RunCreatedHook, notify_run_created
from app.services.sample_loader import GrayBoxDatasetLoader
from app.services.target_binding import canonical_target_binding, canonical_target_ref
from app.workflows.attack_graph import AttackGraph
from app.workflows.attack_state import AttackGraphState

if TYPE_CHECKING:
    from app.services.equipment_service import EquipmentService


RESUME_CLAIM_LEASE_SECONDS = 120
RESUME_CLAIM_RENEW_INTERVAL_SECONDS = 30.0
RESUME_CLAIM_RENEW_TIMEOUT_SECONDS = 10.0


class ResumeClaimLostError(RuntimeError):
    """The graph must stop because its cross-process resume claim cannot be renewed."""


@dataclass
class AdaptiveRuntime:
    """仅驻留内存的 Target 与 Planner 运行时对象；Secret 不进入 checkpoint。"""

    target: TargetConfig
    cases: dict[str, Any]
    policy: Any
    planner: PlannerModelAdapter
    started_at: datetime
    secret_values: set[str]
    target_runtime: Callable[[], AbstractAsyncContextManager[TargetConfig]]


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

    def discard(self, run_id: str) -> None:
        self._runtimes.pop(run_id, None)


async def _persist_interrupted_run(
    repository: AdaptiveRepository,
    *,
    run_id: str,
    status: Literal["failed", "cancelled"],
    operation_id: str,
    exc: BaseException,
) -> None:
    """Persist a safe terminal reason while preserving the original exception."""

    try:
        await repository.finalize_run(
            run_id=run_id,
            status=status,
            terminal_reason=type(exc).__name__[:100],
            stop_reason=f"{status}_at:{operation_id}",
        )
    except Exception as terminal_exc:  # noqa: BLE001 - preserve the original failure
        logger.error(
            "failed to persist gray-box run terminal state",
            run_id=run_id,
            requested_status=status,
            error_type=type(terminal_exc).__name__,
        )


class AdaptiveRunService:
    """冻结输入并驱动 LangGraph；SQL 记录事实，checkpoint 记录继续位置。"""

    def __init__(
        self,
        *,
        repository: AdaptiveRepository,
        checkpointer: Any,
        equipment_service: EquipmentService | None = None,
        secret_broker: SecretBroker | None = None,
    ) -> None:
        self.repository = repository
        self.equipment_service = equipment_service
        self.secret_broker = secret_broker
        self.loader = GrayBoxDatasetLoader()
        self.registry = AdaptiveRuntimeRegistry()
        self._resume_locks: WeakValueDictionary[str, Lock] = WeakValueDictionary()
        self.graph = AttackGraph(
            repository=repository,
            runtime_registry=self.registry,
            checkpointer=checkpointer,
        )

    async def list_approvals(self, run_id: str) -> list[dict[str, Any]]:
        await self.repository.get_run(run_id)
        return await self.repository.list_approvals(run_id)

    async def start(
        self,
        request: GrayBoxRunRequest,
        *,
        on_run_created: RunCreatedHook | None = None,
        requested_run_id: str | None = None,
    ) -> dict[str, Any]:
        """创建 Run、冻结绑定和候选宇宙，然后启动同一 thread 的图执行。"""

        runtime_target = await self._prepare_target(request.target)
        return await self._start_materialized(
            request.model_copy(update={"target": runtime_target}),
            on_run_created=on_run_created,
            requested_run_id=requested_run_id,
        )

    async def _start_materialized(
        self,
        request: GrayBoxRunRequest,
        *,
        on_run_created: RunCreatedHook | None,
        requested_run_id: str | None,
    ) -> dict[str, Any]:
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
            requested_run_id=requested_run_id,
        )
        try:
            async with self._resume_locks.setdefault(run_id, Lock()):
                await notify_run_created(on_run_created, run_id)
                claim_kind = "start"
                checkpoint_id = f"initial:{thread_id}"
                owner_token = await self.repository.acquire_resume_claim(
                    run_id=run_id,
                    claim_kind=claim_kind,
                    checkpoint_id=checkpoint_id,
                    lease_seconds=RESUME_CLAIM_LEASE_SECONDS,
                )
                try:
                    return await self._invoke_with_resume_claim(
                        run_id=run_id,
                        claim_kind=claim_kind,
                        checkpoint_id=checkpoint_id,
                        owner_token=owner_token,
                        operation=lambda: self._start_created_run(
                            request=request,
                            dataset=dataset,
                            run_id=run_id,
                            target_id=target_id,
                            thread_id=thread_id,
                            policy=policy,
                        ),
                    )
                finally:
                    await self._release_resume_claim(
                        run_id=run_id,
                        claim_kind=claim_kind,
                        checkpoint_id=checkpoint_id,
                        owner_token=owner_token,
                    )
        except ResumeClaimLostError:
            self.registry.discard(run_id)
            raise
        except CancelledError as exc:
            self.registry.discard(run_id)
            await _persist_interrupted_run(
                self.repository,
                run_id=run_id,
                status="cancelled",
                operation_id="adaptive_start",
                exc=exc,
            )
            raise
        except Exception as exc:
            self.registry.discard(run_id)
            await _persist_interrupted_run(
                self.repository,
                run_id=run_id,
                status="failed",
                operation_id="adaptive_start",
                exc=exc,
            )
            raise

    async def _start_created_run(
        self,
        *,
        request: GrayBoxRunRequest,
        dataset: LoadedGrayBoxDataset,
        run_id: str,
        target_id: str,
        thread_id: str,
        policy: AttackPolicy,
    ) -> dict[str, Any]:
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
                target_runtime=lambda: self._target_runtime(
                    request.target,
                    frozen=request.target.provider_instance_id is not None,
                ),
            ),
        )
        state: AttackGraphState = {
            "run_id": run_id,
            "goal_id": f"adaptive_graybox:{run_id}",
            "target_id": target_id,
            "thread_id": thread_id,
            "checkpoint_ref": f"langgraph:{thread_id}",
            "allowed_case_ids": [
                case.id for case in dataset.cases if case.id in policy.allowed_case_ids
            ],
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
            provider_instance_id=target.provider_instance_id,
            provider_package_checksum=target.provider_package_checksum,
            provider_config_revision=target.provider_config_revision,
            provider_secret_binding_revision=target.provider_secret_binding_revision,
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
        """处理审批并恢复；手工凭据需重供，Provider 凭据按冻结 revision 短租。"""

        run = await self.repository.get_run(run_id)
        if run.status not in {"waiting_approval", "running"}:
            raise ValueError("run is not waiting for approval")
        if run.thread_id is None:
            raise ValueError("run does not have a resumable thread")
        checkpoint_id = await self._require_checkpoint_node(
            thread_id=run.thread_id,
            node_name="human_review",
            approval_id=approval_id,
        )
        async with self._resume_locks.setdefault(run_id, Lock()):
            run = await self.repository.get_run(run_id)
            if run.status not in {"waiting_approval", "running"}:
                raise ValueError("run is not waiting for approval")
            if run.thread_id is None:
                raise ValueError("run does not have a resumable thread")
            current_checkpoint_id = await self._require_checkpoint_node(
                thread_id=run.thread_id,
                node_name="human_review",
                approval_id=approval_id,
            )
            if current_checkpoint_id != checkpoint_id:
                raise ValueError("run checkpoint advanced while the resume request was waiting")
            claim_kind = "approval"
            owner_token = await self.repository.acquire_resume_claim(
                run_id=run_id,
                claim_kind=claim_kind,
                checkpoint_id=checkpoint_id,
                lease_seconds=RESUME_CLAIM_LEASE_SECONDS,
            )
            try:
                run = await self.repository.get_run(run_id)
                if run.status not in {"waiting_approval", "running"}:
                    raise ValueError("run is not waiting for approval")
                if run.thread_id is None:
                    raise ValueError("run does not have a resumable thread")
                claimed_checkpoint_id = await self._require_checkpoint_node(
                    thread_id=run.thread_id,
                    node_name="human_review",
                    approval_id=approval_id,
                )
                if claimed_checkpoint_id != checkpoint_id:
                    raise ValueError("run checkpoint advanced before the resume claim was acquired")
                await self._renew_resume_claim(
                    run_id=run_id,
                    claim_kind=claim_kind,
                    checkpoint_id=checkpoint_id,
                    owner_token=owner_token,
                )
                recovered = False
                if not self.registry.contains(run_id):
                    runtime_context = self._rehydrate_runtime(
                        run_id=run_id,
                        target_override=target,
                        planner_override=planner,
                    )
                    recovered = True
                else:
                    runtime_context = nullcontext()
                async with runtime_context:
                    await self._renew_resume_claim(
                        run_id=run_id,
                        claim_kind=claim_kind,
                        checkpoint_id=checkpoint_id,
                        owner_token=owner_token,
                    )
                    approval = await self.repository.resolve_approval(
                        run_id=run_id,
                        approval_id=approval_id,
                        approved=approved,
                        resolved_by=resolved_by,
                        reason=reason,
                    )
                    try:
                        result = await self._invoke_with_resume_claim(
                            run_id=run_id,
                            claim_kind=claim_kind,
                            checkpoint_id=checkpoint_id,
                            owner_token=owner_token,
                            operation=lambda: self.graph.graph.ainvoke(
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
                            ),
                        )
                    except ResumeClaimLostError:
                        self.registry.discard(run_id)
                        raise
                    except CancelledError as exc:
                        self.registry.discard(run_id)
                        await _persist_interrupted_run(
                            self.repository,
                            run_id=run_id,
                            status="cancelled",
                            operation_id="approval_resume",
                            exc=exc,
                        )
                        raise
                    except Exception as exc:
                        self.registry.discard(run_id)
                        await _persist_interrupted_run(
                            self.repository,
                            run_id=run_id,
                            status="failed",
                            operation_id="approval_resume",
                            exc=exc,
                        )
                        raise
                    return await self._status(run_id, result)
            finally:
                await self._release_resume_claim(
                    run_id=run_id,
                    claim_kind=claim_kind,
                    checkpoint_id=checkpoint_id,
                    owner_token=owner_token,
                )

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
        checkpoint_id = await self._require_checkpoint_node(
            thread_id=run.thread_id,
            node_name="planner_pause",
        )
        async with self._resume_locks.setdefault(run_id, Lock()):
            run = await self.repository.get_run(run_id)
            if run.status not in {"paused", "running"}:
                raise ValueError("run is not paused")
            if run.thread_id is None:
                raise ValueError("run does not have a resumable thread")
            current_checkpoint_id = await self._require_checkpoint_node(
                thread_id=run.thread_id,
                node_name="planner_pause",
            )
            if current_checkpoint_id != checkpoint_id:
                raise ValueError("run checkpoint advanced while the resume request was waiting")
            claim_kind = "planner"
            owner_token = await self.repository.acquire_resume_claim(
                run_id=run_id,
                claim_kind=claim_kind,
                checkpoint_id=checkpoint_id,
                lease_seconds=RESUME_CLAIM_LEASE_SECONDS,
            )
            try:
                run = await self.repository.get_run(run_id)
                if run.status not in {"paused", "running"}:
                    raise ValueError("run is not paused")
                if run.thread_id is None:
                    raise ValueError("run does not have a resumable thread")
                claimed_checkpoint_id = await self._require_checkpoint_node(
                    thread_id=run.thread_id,
                    node_name="planner_pause",
                )
                if claimed_checkpoint_id != checkpoint_id:
                    raise ValueError("run checkpoint advanced before the resume claim was acquired")
                await self._renew_resume_claim(
                    run_id=run_id,
                    claim_kind=claim_kind,
                    checkpoint_id=checkpoint_id,
                    owner_token=owner_token,
                )
                recovered = False
                if not self.registry.contains(run_id) or target is not None or planner is not None:
                    runtime_context = self._rehydrate_runtime(
                        run_id=run_id,
                        target_override=target,
                        planner_override=planner,
                    )
                    recovered = True
                else:
                    runtime_context = nullcontext()
                async with runtime_context:
                    try:
                        result = await self._invoke_with_resume_claim(
                            run_id=run_id,
                            claim_kind=claim_kind,
                            checkpoint_id=checkpoint_id,
                            owner_token=owner_token,
                            operation=lambda: self.graph.graph.ainvoke(
                                Command(resume={"recovered": recovered}),
                                config={
                                    "configurable": {"thread_id": run.thread_id},
                                    "recursion_limit": 1_000,
                                },
                            ),
                        )
                    except ResumeClaimLostError:
                        self.registry.discard(run_id)
                        raise
                    except CancelledError as exc:
                        self.registry.discard(run_id)
                        await _persist_interrupted_run(
                            self.repository,
                            run_id=run_id,
                            status="cancelled",
                            operation_id="planner_resume",
                            exc=exc,
                        )
                        raise
                    except Exception as exc:
                        self.registry.discard(run_id)
                        await _persist_interrupted_run(
                            self.repository,
                            run_id=run_id,
                            status="failed",
                            operation_id="planner_resume",
                            exc=exc,
                        )
                        raise
                    return await self._status(run_id, result)
            finally:
                await self._release_resume_claim(
                    run_id=run_id,
                    claim_kind=claim_kind,
                    checkpoint_id=checkpoint_id,
                    owner_token=owner_token,
                )

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
        approval_id: str | None = None,
    ) -> str:
        snapshot = await self.graph.graph.aget_state({"configurable": {"thread_id": thread_id}})
        if node_name not in snapshot.next:
            raise ValueError(f"run checkpoint is not waiting at {node_name}")
        if approval_id is not None and snapshot.values.get("approval_id") != approval_id:
            raise ValueError("approval does not match the current run checkpoint")
        checkpoint_id = snapshot.config.get("configurable", {}).get("checkpoint_id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ValueError("run checkpoint does not expose a stable identity")
        return checkpoint_id

    async def _invoke_with_resume_claim(
        self,
        *,
        run_id: str,
        claim_kind: str,
        checkpoint_id: str,
        owner_token: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Renew the DB claim while graph I/O runs and cancel immediately if ownership is lost."""

        await self._renew_resume_claim(
            run_id=run_id,
            claim_kind=claim_kind,
            checkpoint_id=checkpoint_id,
            owner_token=owner_token,
        )

        operation_task = asyncio.ensure_future(operation())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {operation_task},
                    timeout=RESUME_CLAIM_RENEW_INTERVAL_SECONDS,
                )
                if operation_task in done:
                    await self._renew_resume_claim(
                        run_id=run_id,
                        claim_kind=claim_kind,
                        checkpoint_id=checkpoint_id,
                        owner_token=owner_token,
                    )
                    return await operation_task
                try:
                    await self._renew_resume_claim(
                        run_id=run_id,
                        claim_kind=claim_kind,
                        checkpoint_id=checkpoint_id,
                        owner_token=owner_token,
                    )
                except CancelledError:
                    raise
                except ResumeClaimLostError as exc:
                    operation_task.cancel()
                    await asyncio.gather(operation_task, return_exceptions=True)
                    raise ResumeClaimLostError(
                        "adaptive resume claim was lost while the graph was running"
                    ) from exc
        finally:
            if not operation_task.done():
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)

    async def _renew_resume_claim(
        self,
        *,
        run_id: str,
        claim_kind: str,
        checkpoint_id: str,
        owner_token: str,
    ) -> None:
        try:
            await asyncio.wait_for(
                self.repository.renew_resume_claim(
                    run_id=run_id,
                    claim_kind=claim_kind,
                    checkpoint_id=checkpoint_id,
                    owner_token=owner_token,
                    lease_seconds=RESUME_CLAIM_LEASE_SECONDS,
                ),
                timeout=RESUME_CLAIM_RENEW_TIMEOUT_SECONDS,
            )
        except CancelledError:
            raise
        except Exception as exc:
            raise ResumeClaimLostError("adaptive resume claim could not be renewed") from exc

    async def _release_resume_claim(
        self,
        *,
        run_id: str,
        claim_kind: str,
        checkpoint_id: str,
        owner_token: str,
    ) -> None:
        try:
            await self.repository.release_resume_claim(
                run_id=run_id,
                claim_kind=claim_kind,
                checkpoint_id=checkpoint_id,
                owner_token=owner_token,
            )
        except Exception as exc:  # noqa: BLE001 - expiry safely recovers a failed release
            logger.error(
                "failed to release adaptive resume claim",
                run_id=run_id,
                claim_kind=claim_kind,
                error_type=type(exc).__name__,
            )

    @asynccontextmanager
    async def _rehydrate_runtime(
        self,
        *,
        run_id: str,
        target_override: TargetConfig | None,
        planner_override: PlannerConfig | None,
    ) -> AsyncIterator[None]:
        """从 SQL 快照重建运行时，并在图调用期间短租 Provider Secret。"""

        snapshot = await self.repository.load_runtime_snapshot(run_id)
        stored_target = dict(snapshot["target"])
        stored_provider_id = stored_target.get("provider_instance_id")
        revision_fields = (
            "provider_package_checksum",
            "provider_config_revision",
            "provider_secret_binding_revision",
        )
        if stored_provider_id is not None and not all(
            stored_target.get(field) for field in revision_fields
        ):
            raise ValueError("stored Target Provider Instance revision binding is incomplete")
        stored_auth = stored_target.get("auth")
        if target_override is None:
            target_payload = dict(stored_target)
            if stored_provider_id is not None and isinstance(stored_auth, dict):
                target_payload["auth"] = {**stored_auth, "token": None}
            if self._contains_redaction(target_payload):
                raise ValueError(
                    "target credentials were redacted; resupply target configuration to resume"
                )
            target = TargetConfig.model_validate(target_payload)
        else:
            target = target_override.model_copy(deep=True)
            if target.provider_instance_id != stored_provider_id:
                raise ValueError("resupplied target must match the original non-secret behavior")
            for field in revision_fields:
                stored_value = stored_target.get(field)
                override_value = getattr(target, field)
                if override_value is not None and override_value != stored_value:
                    raise ValueError(
                        "resupplied target must match the original non-secret behavior"
                    )
                setattr(target, field, stored_value)

        stored_planner = {
            **self._planner_snapshot(PlannerConfig()),
            **(snapshot.get("planner") or {}),
        }
        if planner_override is None:
            if stored_planner.get("api_key_required"):
                raise ValueError(
                    "planner credentials were redacted; resupply planner configuration to resume"
                )
            planner_config = PlannerConfig.model_validate(stored_planner)
        else:
            planner_override_snapshot = self._planner_snapshot(planner_override)
            for field, override_value in planner_override_snapshot.items():
                stored_value = stored_planner.get(field)
                if stored_value != override_value:
                    raise ValueError(f"resupplied planner {field} must match the original run")
            planner_config = planner_override

        try:
            runtime_target = await self._prepare_target(
                target,
                frozen=stored_provider_id is not None,
            )
            if canonical_target_binding(runtime_target) != canonical_target_binding(stored_target):
                raise ValueError("resupplied target must match the original non-secret behavior")

            raw_cases = snapshot["dataset"].get("cases", [])
            cases = [GrayBoxCase.model_validate(raw_case["inputs"]) for raw_case in raw_cases]
            started_at = snapshot["started_at"]
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            self.registry.add(
                run_id,
                AdaptiveRuntime(
                    target=runtime_target,
                    cases={case.id: case for case in cases},
                    policy=AttackPolicy.model_validate(snapshot["policy"]),
                    planner=create_planner_adapter(planner_config),
                    started_at=started_at,
                    secret_values=self._secret_values(runtime_target),
                    target_runtime=lambda: self._target_runtime(
                        runtime_target,
                        frozen=stored_provider_id is not None,
                    ),
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
            yield
        finally:
            self.registry.discard(run_id)

    async def _status(self, run_id: str, graph_result: dict[str, Any]) -> dict[str, Any]:
        try:
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
        finally:
            self.registry.discard(run_id)

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

    async def _validate_target(self, target: TargetConfig) -> dict[str, Any] | None:
        if target.provider_instance_id is not None:
            if self.equipment_service is None:
                raise RuntimeError("Provider Instance Target validation is unavailable")
            return await self.equipment_service.validate_target(target)
        validate_target_url(
            str(target.endpoint),
            allow_public_target=target.allow_public_target,
            allowed_hosts=[],
        )
        return None

    @asynccontextmanager
    async def _target_runtime(
        self,
        target: TargetConfig,
        *,
        frozen: bool = False,
    ) -> AsyncIterator[TargetConfig]:
        if target.provider_instance_id is None:
            await self._validate_target(target)
            yield target.model_copy(deep=True)
            return
        if self.equipment_service is not None:
            async with self.equipment_service.materialize_target(
                target,
                frozen=frozen,
            ) as runtime_target:
                yield runtime_target
            return
        raise RuntimeError("Provider Instance Target materialization is unavailable")

    async def _prepare_target(
        self,
        target: TargetConfig,
        *,
        frozen: bool = False,
    ) -> TargetConfig:
        if target.provider_instance_id is None:
            await self._validate_target(target)
            return target.model_copy(deep=True)
        if self.equipment_service is None:
            raise RuntimeError("Provider Instance Target materialization is unavailable")
        return await self.equipment_service.prepare_target(target, frozen=frozen)


class DeterministicGrayBoxRunService:
    """固定顺序执行灰盒 Case，用作自适应模式的可比较基线。"""

    def __init__(
        self,
        repository: AdaptiveRepository,
        *,
        equipment_service: EquipmentService | None = None,
        connector: GrayBoxConnector | None = None,
    ) -> None:
        self.repository = repository
        self.equipment_service = equipment_service
        self.loader = GrayBoxDatasetLoader()
        self.policy_service = PolicyService()
        self.case_pipeline = GrayBoxCasePipeline(repository, connector=connector)

    async def run(
        self,
        request: DeterministicGrayBoxRunRequest,
        *,
        on_run_created: RunCreatedHook | None = None,
        requested_run_id: str | None = None,
    ) -> dict[str, Any]:
        dataset = await self.loader.load(request.dataset_path, request.case_ids)
        return await self.run_dataset(
            target=request.target,
            dataset=dataset,
            policy=request.policy,
            mode="deterministic_graybox",
            baseline_run_id=request.baseline_run_id,
            test_principal_refs=request.test_principal_refs,
            preauthorize_approvals=request.preauthorize_approvals,
            on_run_created=on_run_created,
            requested_run_id=requested_run_id,
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
        preauthorize_approvals: bool = False,
        on_run_created: RunCreatedHook | None = None,
        requested_run_id: str | None = None,
    ) -> dict[str, Any]:
        runtime_target = await self._prepare_target(
            target,
            frozen=equipment_source_run_id is not None,
        )
        return await self._run_materialized_dataset(
            target=runtime_target,
            dataset=dataset,
            policy=policy,
            mode=mode,
            baseline_run_id=baseline_run_id,
            test_principal_refs=test_principal_refs,
            equipment_source_run_id=equipment_source_run_id,
            equipment_overrides=equipment_overrides,
            preauthorize_approvals=preauthorize_approvals,
            on_run_created=on_run_created,
            requested_run_id=requested_run_id,
        )

    async def _run_materialized_dataset(
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
        preauthorize_approvals: bool = False,
        on_run_created: RunCreatedHook | None = None,
        requested_run_id: str | None = None,
    ) -> dict[str, Any]:
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
            requested_run_id=requested_run_id,
        )
        try:
            await notify_run_created(on_run_created, run_id)
            return await self._run_created_dataset(
                run_id=run_id,
                target_id=target_id,
                target=target,
                dataset=dataset,
                policy=policy,
                test_principal_refs=test_principal_refs,
                equipment_source_run_id=equipment_source_run_id,
                equipment_overrides=equipment_overrides,
                preauthorize_approvals=preauthorize_approvals,
            )
        except CancelledError as exc:
            await _persist_interrupted_run(
                self.repository,
                run_id=run_id,
                status="cancelled",
                operation_id="deterministic_graybox",
                exc=exc,
            )
            raise
        except Exception as exc:
            await _persist_interrupted_run(
                self.repository,
                run_id=run_id,
                status="failed",
                operation_id="deterministic_graybox",
                exc=exc,
            )
            raise

    @asynccontextmanager
    async def _target_runtime(
        self,
        target: TargetConfig,
        *,
        frozen: bool = False,
    ) -> AsyncIterator[TargetConfig]:
        if target.provider_instance_id is None:
            validate_target_url(
                str(target.endpoint),
                allow_public_target=target.allow_public_target,
                allowed_hosts=[],
            )
            yield target.model_copy(deep=True)
            return
        if self.equipment_service is not None:
            async with self.equipment_service.materialize_target(
                target,
                frozen=frozen,
            ) as runtime_target:
                yield runtime_target
            return
        raise RuntimeError("Provider Instance Target materialization is unavailable")

    async def _prepare_target(
        self,
        target: TargetConfig,
        *,
        frozen: bool = False,
    ) -> TargetConfig:
        if target.provider_instance_id is not None:
            if self.equipment_service is None:
                raise RuntimeError("Provider Instance Target materialization is unavailable")
            return await self.equipment_service.prepare_target(target, frozen=frozen)
        validate_target_url(
            str(target.endpoint),
            allow_public_target=target.allow_public_target,
            allowed_hosts=[],
        )
        return target.model_copy(deep=True)

    async def _run_created_dataset(
        self,
        *,
        run_id: str,
        target_id: str,
        target: TargetConfig,
        dataset: LoadedGrayBoxDataset,
        policy: AttackPolicy,
        test_principal_refs: list[str] | None,
        equipment_source_run_id: str | None,
        equipment_overrides: dict[str, dict[str, Any]] | None,
        preauthorize_approvals: bool,
    ) -> dict[str, Any]:
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
                    provider_instance_id=target.provider_instance_id,
                    provider_package_checksum=target.provider_package_checksum,
                    provider_config_revision=target.provider_config_revision,
                    provider_secret_binding_revision=target.provider_secret_binding_revision,
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
            remaining_steps = policy.max_steps - sequence + 1
            elapsed_seconds = (datetime.now(UTC) - started_at).total_seconds()
            gate = self.policy_service.evaluate(
                policy=policy,
                target_id=target_id,
                case=case,
                remaining_steps=remaining_steps,
                target_call_count=target_calls,
                elapsed_seconds=elapsed_seconds,
                approval_status=approval_status,
                approval_id=approval_id,
            )
            if gate.decision == ToolPolicyDecision.approval_required and preauthorize_approvals:
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
                    reason="explicitly pre-authorized deterministic baseline",
                )
                approval_status = str(resolved["status"])
                gate = self.policy_service.evaluate(
                    policy=policy,
                    target_id=target_id,
                    case=case,
                    remaining_steps=remaining_steps,
                    target_call_count=target_calls,
                    elapsed_seconds=elapsed_seconds,
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
            async with self._target_runtime(
                target,
                frozen=target.provider_instance_id is not None,
            ) as call_target:
                call_secret_values = AdaptiveRunService._secret_values(call_target)
                await self.case_pipeline.run_case(
                    run_id=run_id,
                    case=case,
                    target=call_target,
                    operation_id=operation_id,
                    sequence=sequence,
                    approval_id=approval_id,
                    secret_values=secret_values | call_secret_values,
                    policy=PolicyGateResult.model_validate(gate),
                    policy_event_ids=[policy_event_id],
                )
            target_calls += 1
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
