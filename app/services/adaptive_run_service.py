"""创建、暂停、恢复和控制自适应/灰盒 Run，并管理瞬时运行时凭据。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from asyncio import CancelledError, Lock
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from weakref import WeakValueDictionary

from loguru import logger

from app.agent.hooks import Hooks
from app.agent.loop import run_loop
from app.agent.runtime import AgentRuntime, RunResources
from app.agent.session import Session, load_session, save_session
from app.agent.state import RunState
from app.equipment.security import SecretBroker, validate_target_url
from app.infrastructure.model_adapter import create_planner_adapter
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
from app.services.finish_gate_service import FinishGateService
from app.services.graybox_case_pipeline import GrayBoxCasePipeline
from app.services.graybox_connector import GrayBoxConnector
from app.services.policy_service import PolicyService
from app.services.run_lifecycle import RunCreatedHook, notify_run_created
from app.services.sample_loader import GrayBoxDatasetLoader
from app.services.target_binding import canonical_target_binding, canonical_target_ref

if TYPE_CHECKING:
    from app.services.equipment_service import EquipmentService


RESUME_CLAIM_LEASE_SECONDS = 120
RESUME_CLAIM_RENEW_INTERVAL_SECONDS = 30.0
RESUME_CLAIM_RENEW_TIMEOUT_SECONDS = 10.0


class ResumeClaimLostError(RuntimeError):
    """The loop must stop because its cross-process resume claim cannot be renewed."""


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
    """冻结输入、驱动手写循环，并通过 SQL Session 恢复审批与 Planner 暂停。"""

    def __init__(
        self,
        *,
        repository: AdaptiveRepository,
        equipment_service: EquipmentService | None = None,
        secret_broker: SecretBroker | None = None,
        connector: GrayBoxConnector | None = None,
        finish_gate: FinishGateService | None = None,
        hooks: Hooks | None = None,
    ) -> None:
        self.repository = repository
        self.equipment_service = equipment_service
        self.secret_broker = secret_broker
        self.loader = GrayBoxDatasetLoader()
        self.connector = connector or GrayBoxConnector()
        self.finish_gate_service = finish_gate or FinishGateService()
        self.hooks = hooks if hooks is not None else Hooks()
        self._resume_locks: WeakValueDictionary[str, Lock] = WeakValueDictionary()

    async def _execute_runtime(self, runtime: AgentRuntime) -> None:
        await run_loop(runtime)

    def _runtime(self, resources: RunResources, state: RunState) -> AgentRuntime:
        return AgentRuntime(
            self.repository,
            resources,
            state,
            connector=self.connector,
            finish_gate=self.finish_gate_service,
            hooks=self.hooks,
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
        """创建 Run、冻结绑定和候选宇宙，然后启动该 Run 的手写循环。"""

        runtime_target = await self._prepare_target(request.target)
        return await self._start_materialized(
            request.model_copy(update={"target": runtime_target}),
            on_run_created=on_run_created,
            requested_run_id=requested_run_id,
        )

    async def start_dataset(
        self,
        request: GrayBoxRunRequest,
        dataset: LoadedGrayBoxDataset,
        *,
        requested_run_id: str,
    ) -> dict[str, Any]:
        """执行协调器已冻结的子集，避免排队期间重新读取发生变化的文件。"""

        runtime_target = await self._prepare_target(request.target)
        return await self._start_materialized(
            request.model_copy(update={"target": runtime_target}),
            on_run_created=None,
            requested_run_id=requested_run_id,
            dataset=dataset,
        )

    async def _start_materialized(
        self,
        request: GrayBoxRunRequest,
        *,
        on_run_created: RunCreatedHook | None,
        requested_run_id: str | None,
        dataset: LoadedGrayBoxDataset | None = None,
    ) -> dict[str, Any]:
        if dataset is None:
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
            raise
        except CancelledError as exc:
            await _persist_interrupted_run(
                self.repository,
                run_id=run_id,
                status="cancelled",
                operation_id="adaptive_start",
                exc=exc,
            )
            raise
        except Exception as exc:
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
        resources = RunResources(
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
        )
        state: RunState = {
            "run_id": run_id,
            "goal_id": f"adaptive_graybox:{run_id}",
            "target_id": target_id,
            "thread_id": thread_id,
            "checkpoint_ref": f"session:{run_id}",
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
            "step_count": 0,
            "last_state_fingerprint": None,
            "repeated_state_count": 0,
            "consecutive_no_gain_steps": 0,
            "next_action": "plan",
            "status": "running",
            "terminal_reason": None,
            "stop_reason": None,
            "recovery_pending": False,
        }
        await save_session(self.repository, state)
        await self._execute_runtime(self._runtime(resources, state))
        return await self._status(run_id)

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
        return await self._resume(
            run_id=run_id,
            target=target,
            planner=planner,
            approval_id=approval_id,
            approved=approved,
            resolved_by=resolved_by,
            reason=reason,
        )

    async def resume_paused(
        self,
        *,
        run_id: str,
        target: TargetConfig | None = None,
        planner: PlannerConfig | None = None,
    ) -> dict[str, Any]:
        return await self._resume(run_id=run_id, target=target, planner=planner)

    async def _require_session(
        self,
        run_id: str,
        approval_id: str | None,
    ) -> Session:
        run = await self.repository.get_run(run_id)
        session = await load_session(self.repository, run_id)
        expected = "waiting_approval" if approval_id is not None else "paused"
        if run.status not in {expected, "running"} or session.state["status"] != expected:
            raise ValueError(f"run session is not {expected}")
        if approval_id is not None and session.state["approval_id"] != approval_id:
            raise ValueError("approval does not match the current run session")
        return session

    async def _resume(
        self,
        *,
        run_id: str,
        target: TargetConfig | None,
        planner: PlannerConfig | None,
        approval_id: str | None = None,
        approved: bool = False,
        resolved_by: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        expected = await self._require_session(run_id, approval_id)
        async with self._resume_locks.setdefault(run_id, Lock()):
            session = await self._require_session(run_id, approval_id)
            if session.id != expected.id:
                raise ValueError("run session advanced while the resume request was waiting")
            claim_kind = "approval" if approval_id is not None else "planner"
            owner_token = await self.repository.acquire_resume_claim(
                run_id=run_id,
                claim_kind=claim_kind,
                checkpoint_id=session.id,
                lease_seconds=RESUME_CLAIM_LEASE_SECONDS,
            )
            try:
                claimed = await self._require_session(run_id, approval_id)
                if claimed.id != session.id:
                    raise ValueError("run session advanced before the resume claim was acquired")
                # 凭据或绑定错误不消耗暂停点，调用者可以修正输入后重试。
                resources = await self._rehydrate_runtime(
                    run_id=run_id,
                    target_override=target,
                    planner_override=planner,
                )

                async def continue_run() -> dict[str, Any]:
                    state = session.state
                    if approval_id is not None:
                        approval = await self.repository.resolve_approval(
                            run_id=run_id,
                            approval_id=approval_id,
                            approved=approved,
                            resolved_by=resolved_by,
                            reason=reason,
                        )
                        state["approval_status"] = approval["status"]
                    state.update(
                        status="running",
                        terminal_reason=None,
                        stop_reason=None,
                        recovery_pending=True,
                        next_action="policy" if approval_id is not None else "plan",
                    )
                    # 在任何工具副作用之前消耗暂停点。崩溃后的 running Session 不自动重放。
                    await save_session(self.repository, state, resumed_from=session.id)
                    await self._execute_runtime(self._runtime(resources, state))
                    return await self._status(run_id)

                try:
                    return await self._invoke_with_resume_claim(
                        run_id=run_id,
                        claim_kind=claim_kind,
                        checkpoint_id=session.id,
                        owner_token=owner_token,
                        operation=continue_run,
                    )
                except ResumeClaimLostError:
                    raise
                except CancelledError as exc:
                    await _persist_interrupted_run(
                        self.repository,
                        run_id=run_id,
                        status="cancelled",
                        operation_id=f"{claim_kind}_resume",
                        exc=exc,
                    )
                    raise
                except Exception as exc:
                    await _persist_interrupted_run(
                        self.repository,
                        run_id=run_id,
                        status="failed",
                        operation_id=f"{claim_kind}_resume",
                        exc=exc,
                    )
                    raise
            finally:
                await self._release_resume_claim(
                    run_id=run_id,
                    claim_kind=claim_kind,
                    checkpoint_id=session.id,
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

    async def _invoke_with_resume_claim(
        self,
        *,
        run_id: str,
        claim_kind: str,
        checkpoint_id: str,
        owner_token: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Renew the DB claim while loop I/O runs and cancel immediately if ownership is lost."""

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
                        "adaptive resume claim was lost while the loop was running"
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

    async def _rehydrate_runtime(
        self,
        *,
        run_id: str,
        target_override: TargetConfig | None,
        planner_override: PlannerConfig | None,
    ) -> RunResources:
        """从 SQL 快照重建运行时，并仅在工具执行期间短租 Provider Secret。"""

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
        resources = RunResources(
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
        return resources

    async def _status(self, run_id: str) -> dict[str, Any]:
        run = await self.repository.get_run(run_id)
        approvals = await self.repository.list_approvals(run_id)
        return {
            "run_id": run_id,
            "thread_id": run.thread_id,
            "status": run.status,
            "terminal_reason": run.terminal_reason,
            "pending_approvals": [item for item in approvals if item["status"] == "pending"],
            "interrupted": run.status in {"waiting_approval", "paused"},
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
            "response_mode": planner.response_mode,
            "context_budget": planner.context_budget.model_dump(mode="json"),
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
