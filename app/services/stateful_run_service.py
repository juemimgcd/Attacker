"""带状态 Dataset 的隔离执行、Evidence 持久化与测试夹具清理。"""

from __future__ import annotations

import hashlib
from asyncio import CancelledError
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

from app.repositories.stateful_repository import StatefulRepository
from app.schemas.stateful_schema import (
    LoadedStatefulDataset,
    MemoryEvidence,
    RecoveryEvidence,
    RetrievalTrace,
    StatefulCase,
    StatefulEvaluatorType,
    StatefulProfile,
    StatefulRunRequest,
    StateIdentity,
)
from app.services.sample_loader import StatefulDatasetLoader
from app.services.stateful_adapters import MemoryAdapter, RAGAdapter
from app.services.stateful_evaluator_service import StatefulEvaluatorService

if TYPE_CHECKING:
    from app.services.equipment_service import EquipmentService

_INTERRUPTED_CLEANUP_ATTEMPTS = 2


class StatefulRunService:
    """使用内置 Profile 验证平台契约，不代表真实生产 Memory/RAG 已被验证。"""

    def __init__(
        self,
        repository: StatefulRepository,
        *,
        equipment_service: EquipmentService | None = None,
    ) -> None:
        self.repository = repository
        self.equipment_service = equipment_service
        self.loader = StatefulDatasetLoader()
        self.memory = MemoryAdapter(repository)
        self.rag = RAGAdapter(repository)
        self.evaluator = StatefulEvaluatorService()

    async def run(
        self,
        request: StatefulRunRequest,
        *,
        mode: str = "deterministic_stateful",
    ) -> dict[str, Any]:
        dataset = await self.loader.load(request.dataset_path, request.case_ids)
        return await self.run_dataset(
            dataset=dataset,
            profile=request.profile,
            target_name=request.target_name,
            mode=mode,
        )

    async def run_dataset(
        self,
        *,
        dataset: LoadedStatefulDataset,
        profile: StatefulProfile,
        target_name: str,
        mode: str,
        equipment_source_run_id: str | None = None,
        equipment_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """按顺序执行状态 Case，并确保运行结束后仍尝试清理全部夹具。"""

        run_id = await self.repository.create_run(
            dataset=dataset,
            profile=profile,
            target_name=target_name,
            mode=mode,
        )
        try:
            return await self._run_created_dataset(
                run_id=run_id,
                dataset=dataset,
                profile=profile,
                target_name=target_name,
                equipment_source_run_id=equipment_source_run_id,
                equipment_overrides=equipment_overrides,
            )
        except CancelledError as exc:
            cleanup_failures = await self._cleanup_after_interruption(
                run_id=run_id,
                dataset=dataset,
            )
            await self._finalize_interrupted_run(
                run_id=run_id,
                status="cancelled",
                exc=exc,
                cleanup_failures=cleanup_failures,
            )
            raise
        except Exception as exc:
            cleanup_failures = await self._cleanup_after_interruption(
                run_id=run_id,
                dataset=dataset,
            )
            await self._finalize_interrupted_run(
                run_id=run_id,
                status="failed",
                exc=exc,
                cleanup_failures=cleanup_failures,
            )
            raise

    async def _run_created_dataset(
        self,
        *,
        run_id: str,
        dataset: LoadedStatefulDataset,
        profile: StatefulProfile,
        target_name: str,
        equipment_source_run_id: str | None,
        equipment_overrides: dict[str, dict[str, Any]] | None,
    ) -> dict[str, Any]:
        if self.equipment_service is not None:
            if equipment_source_run_id is not None:
                await self.equipment_service.clone_run_bindings(
                    source_run_id=equipment_source_run_id,
                    target_run_id=run_id,
                )
            else:
                await self.equipment_service.freeze_run_bindings(
                    run_id=run_id,
                    stage="stateful",
                    target_binding_ref=target_name,
                    test_principal_ref=f"stateful:{profile.value}",
                    overrides=equipment_overrides,
                )
        protected_scope = f"{run_id}:protected-control"
        await self.repository.write_fixture(
            run_id=run_id,
            operation_id=f"{run_id}:protected-control:write",
            scope_id=protected_scope,
            kind="memory",
            identity=StateIdentity(
                tenant_id="protected-tenant",
                user_id="protected-user",
                session_id="protected-session",
            ),
            namespace="protected-non-case-data",
            resource_id="memory:protected-control",
            content_summary="protected cleanup boundary control",
            provenance="protected_control_fixture",
            permissions={
                "tenant_id": "protected-tenant",
                "user_id": "protected-user",
            },
            poisoned=False,
            active=True,
        )
        for sequence, case in enumerate(dataset.cases, start=1):
            await self._run_case(
                run_id=run_id,
                sequence=sequence,
                case=case,
                profile=profile,
            )
        await self.repository.cleanup(
            run_id=run_id,
            scope_id=protected_scope,
            operation_id=f"{run_id}:protected-control:cleanup",
        )
        await self.repository.finalize_run(
            run_id,
            "stateful dataset completed with isolated cleanup",
        )
        run = await self.repository.get_run(run_id)
        return {
            "run_id": run_id,
            "thread_id": run.thread_id,
            "status": run.status,
            "profile": profile.value,
        }

    async def _cleanup_after_interruption(
        self,
        *,
        run_id: str,
        dataset: LoadedStatefulDataset,
    ) -> list[dict[str, str | int]]:
        scopes = [
            (
                f"{run_id}:{case.id}",
                f"{run_id}:stateful:{case.id}:{sequence}:cleanup",
            )
            for sequence, case in enumerate(dataset.cases, start=1)
        ]
        scopes.append(
            (
                f"{run_id}:protected-control",
                f"{run_id}:protected-control:cleanup",
            )
        )
        failures: list[dict[str, str | int]] = []
        for scope_id, operation_id in scopes:
            for attempt in range(1, _INTERRUPTED_CLEANUP_ATTEMPTS + 1):
                try:
                    await self.repository.cleanup(
                        run_id=run_id,
                        scope_id=scope_id,
                        operation_id=operation_id,
                    )
                    break
                except Exception as cleanup_exc:  # noqa: BLE001 - isolate scoped cleanup
                    if attempt < _INTERRUPTED_CLEANUP_ATTEMPTS:
                        logger.warning(
                            "retrying interrupted stateful fixture cleanup",
                            run_id=run_id,
                            attempt=attempt,
                            error_type=type(cleanup_exc).__name__,
                        )
                        continue
                    failure: dict[str, str | int] = {
                        "scope_id": scope_id,
                        "error_type": type(cleanup_exc).__name__[:100],
                        "attempts": attempt,
                    }
                    failures.append(failure)
                    try:
                        await self.repository.append_event(
                            run_id=run_id,
                            operation_id=f"{operation_id}:failed",
                            event_type="state_cleanup_failed",
                            evidence={"cleanup_failure": failure},
                        )
                    except Exception as audit_exc:  # noqa: BLE001 - terminal event also carries it
                        logger.error(
                            "failed to persist stateful cleanup failure evidence",
                            run_id=run_id,
                            error_type=type(audit_exc).__name__,
                        )
                    logger.error(
                        "failed to clean interrupted stateful fixture scope",
                        run_id=run_id,
                        attempts=attempt,
                        error_type=type(cleanup_exc).__name__,
                    )
        return failures

    async def _finalize_interrupted_run(
        self,
        *,
        run_id: str,
        status: Literal["failed", "cancelled"],
        exc: BaseException,
        cleanup_failures: list[dict[str, str | int]],
    ) -> None:
        try:
            await self.repository.finalize_run(
                run_id,
                type(exc).__name__[:100],
                status=status,
                cleanup_failures=cleanup_failures,
            )
        except Exception as terminal_exc:  # noqa: BLE001 - preserve the original failure
            logger.error(
                "failed to persist stateful run terminal state",
                run_id=run_id,
                requested_status=status,
                error_type=type(terminal_exc).__name__,
            )

    async def _run_case(
        self,
        *,
        run_id: str,
        sequence: int,
        case: StatefulCase,
        profile: StatefulProfile,
    ) -> None:
        operation_id = f"{run_id}:stateful:{case.id}:{sequence}"
        scope_id = f"{run_id}:{case.id}"
        step = await self.repository.ensure_step(
            run_id=run_id,
            case_id=case.id,
            operation_id=operation_id,
            sequence=sequence,
        )
        unsafe = profile == StatefulProfile.regressed or (
            profile == StatefulProfile.vulnerable and case.kind.value == "attack"
        )
        evidence_ids = [
            await self.repository.record_snapshot(
                run_id=run_id,
                step_id=step.id,
                operation_id=f"{operation_id}:snapshot:pre",
                case_id=case.id,
                phase="pre",
                identity=case.observer,
                items=[],
            )
        ]
        observed: list[MemoryEvidence] | None = None
        retrieval: RetrievalTrace | None = None
        recovery: RecoveryEvidence | None = None
        if case.evaluator_type in {
            StatefulEvaluatorType.memory_poisoning,
            StatefulEvaluatorType.identity_isolation,
        }:
            await self.memory.write(
                run_id=run_id,
                operation_id=f"{operation_id}:memory:write",
                scope_id=scope_id,
                identity=case.attacker,
                namespace=case.namespace,
                marker=case.poison_marker,
                active=unsafe,
            )
            observed = await self.memory.read(
                scope_id=scope_id,
                identity=case.observer,
                namespace=case.namespace,
                strict_identity=not unsafe,
            )
        elif case.evaluator_type == StatefulEvaluatorType.rag_poisoning:
            await self.rag.index(
                run_id=run_id,
                operation_id=f"{operation_id}:rag:index",
                scope_id=scope_id,
                identity=case.attacker,
                namespace=case.namespace,
                marker=case.poison_marker,
                active=unsafe,
            )
            retrieval = await self.rag.retrieve(
                scope_id=scope_id,
                identity=case.observer,
                namespace=case.namespace,
                query=case.query or "",
                strict_identity=not unsafe,
            )
            evidence_ids.append(
                await self.repository.record_retrieval(
                    run_id=run_id,
                    step_id=step.id,
                    operation_id=f"{operation_id}:retrieval",
                    case_id=case.id,
                    identity=case.observer,
                    trace=retrieval,
                )
            )
        else:
            fixture_operation = f"{operation_id}:recovery:write"
            await self.memory.write(
                run_id=run_id,
                operation_id=fixture_operation,
                scope_id=scope_id,
                identity=case.attacker,
                namespace=case.namespace,
                marker=case.poison_marker,
                active=True,
            )
            await self.memory.write(
                run_id=run_id,
                operation_id=fixture_operation,
                scope_id=scope_id,
                identity=case.attacker,
                namespace=case.namespace,
                marker=case.poison_marker,
                active=True,
            )
            fixtures = await self.repository.list_fixtures(
                scope_id=scope_id,
                namespace=case.namespace,
                kind="memory",
            )
            recovery = RecoveryEvidence(
                thread_id=f"stateful-run:{run_id}",
                policy_revalidated=not unsafe,
                operation_attempts=2,
                committed_operations=len(fixtures),
            )
            evidence_ids.append(
                await self.repository.record_recovery(
                    run_id=run_id,
                    step_id=step.id,
                    operation_id=f"{operation_id}:recovery:evidence",
                    case_id=case.id,
                    evidence=recovery.model_dump(mode="json"),
                )
            )
        observer_items = (
            [item.model_dump(mode="json") for item in observed]
            if observed is not None
            else (
                [document.model_dump(mode="json") for document in retrieval.documents]
                if retrieval is not None
                else [recovery.model_dump(mode="json")]
                if recovery is not None
                else []
            )
        )
        evidence_ids.append(
            await self.repository.record_snapshot(
                run_id=run_id,
                step_id=step.id,
                operation_id=f"{operation_id}:snapshot:observer",
                case_id=case.id,
                phase="observer",
                identity=case.observer,
                items=observer_items,
            )
        )
        evaluation = self.evaluator.evaluate(
            case=case,
            observed_memory=observed,
            retrieval=retrieval,
            recovery=recovery,
        )
        cleanup = await self.memory.cleanup(
            run_id=run_id,
            scope_id=scope_id,
            operation_id=f"{operation_id}:cleanup",
        )
        evidence_ids.append(
            await self.repository.record_snapshot(
                run_id=run_id,
                step_id=step.id,
                operation_id=f"{operation_id}:snapshot:cleanup",
                case_id=case.id,
                phase="cleanup",
                identity=case.observer,
                items=[],
            )
        )
        fingerprint = hashlib.sha256(
            f"stateful|{case.category}|{case.id}|{case.expected_violation}".encode()
        ).hexdigest()
        await self.repository.complete_case(
            run_id=run_id,
            operation_id=operation_id,
            case=case,
            profile=profile,
            evaluation=evaluation,
            cleanup=cleanup,
            evidence_event_ids=evidence_ids,
            fingerprint=fingerprint,
        )
