"""Execute standalone Benchmark lifecycles; Core does not resolve target adapters or Skills."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import yaml

from app.equipment.benchmark_metrics import validate_metrics
from app.equipment.json_schema import validate_instance
from app.equipment.runner import EquipmentRunner
from app.equipment.security import SecretBroker, redact, sensitive_values
from app.repositories.equipment_benchmark_repository import EquipmentBenchmarkRepository
from app.repositories.run_repository import RunRepository
from app.schemas.equipment_benchmark_schema import (
    BenchmarkCleanup,
    BenchmarkContext,
    BenchmarkEvaluation,
    BenchmarkObservation,
    BenchmarkPreparation,
    BenchmarkRunPolicy,
    BenchmarkTask,
    BenchmarkTasks,
    EquipmentBenchmarkManifest,
    EquipmentBenchmarkRequest,
    LoadedEquipmentBenchmarkDataset,
)
from app.schemas.equipment_schema import PackageType
from app.services.equipment_service import EquipmentService


class EquipmentBenchmarkService:
    def __init__(
        self,
        equipment: EquipmentService,
        runner: EquipmentRunner | None = None,
        secret_broker: SecretBroker | None = None,
    ) -> None:
        self.equipment = equipment
        self.runner = runner or EquipmentRunner(equipment.catalog.settings)
        self.secrets = secret_broker or equipment.secret_broker or SecretBroker()
        self.runs = RunRepository(equipment.repository.session_factory, equipment.repository.events)
        self.repository = EquipmentBenchmarkRepository(self.runs)

    async def prepare(
        self, benchmark_id: str, request: EquipmentBenchmarkRequest
    ) -> dict[str, Any]:
        """Offline preflight: one package, its own tasks and its own target configuration."""
        package = await self.equipment.get_package(
            PackageType.benchmark, benchmark_id, request.version
        )
        if package["validation_status"] != "valid" or not package["enabled"]:
            raise ValueError("Benchmark package must be valid and enabled")
        package = self.equipment.materialize_package(package)
        manifest = EquipmentBenchmarkManifest.model_validate(package["manifest"])
        if request.target not in manifest.targets:
            raise ValueError(f"unknown benchmark target: {request.target}")
        target = manifest.targets[request.target]
        if manifest.execution.concurrency > 1 and not target.parallel_safe:
            raise ValueError("concurrent execution requires target.parallel_safe=true")
        root = Path(package["source_path"])
        tasks = BenchmarkTasks.model_validate(
            yaml.safe_load((root / manifest.tasks_file).read_text(encoding="utf-8"))
        ).tasks
        if request.task_ids is not None:
            missing = set(request.task_ids) - {task.id for task in tasks}
            if missing:
                raise ValueError(f"unknown benchmark tasks: {sorted(missing)}")
            tasks = [task for task in tasks if task.id in request.task_ids]
        if len(tasks) * manifest.execution.repetitions > 10000:
            raise ValueError("a benchmark run is limited to 10000 task attempts")
        task_schema = json.loads((root / manifest.task_schema).read_text(encoding="utf-8"))
        target_schema = json.loads((root / manifest.target_schema).read_text(encoding="utf-8"))
        validate_instance(target.config, target_schema)
        for task in tasks:
            validate_instance(task.model_dump(mode="json"), task_schema)
        for reference in target.secret_refs.values():
            self.secrets.validate_reference(reference, require_configured=True)
        return {"manifest": manifest, "package": package, "tasks": tasks, "target": target}

    async def run(self, benchmark_id: str, request: EquipmentBenchmarkRequest) -> dict[str, Any]:
        plan = await self.prepare(benchmark_id, request)
        manifest: EquipmentBenchmarkManifest = plan["manifest"]
        package = plan["package"]
        run_id = str(uuid4())
        principal = request.test_principal.model_copy(
            update={"session_scope_id": f"{request.test_principal.session_scope_id}:{run_id}"}
        )
        content = {
            "benchmark_checksum": package["checksum"],
            "tasks": [task.model_dump(mode="json") for task in plan["tasks"]],
            "repetitions": manifest.execution.repetitions,
        }
        dataset = LoadedEquipmentBenchmarkDataset(
            name=manifest.id,
            version=manifest.version,
            source_path=Path(package["source_path"]) / manifest.tasks_file,
            sha256=hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest(),
            cases=plan["tasks"] * manifest.execution.repetitions,
            snapshot=redact(content),
        )
        await self.runs.create_run(
            target_snapshot={
                "name": request.target,
                "endpoint": f"equipment://{manifest.id}/{request.target}",
                "benchmark_checksum": package["checksum"],
                "target": plan["target"].model_dump(mode="json"),
            },
            dataset=dataset,
            budget=BenchmarkRunPolicy(
                **manifest.execution.model_dump(), test_principal_ref=principal.reference
            ),
            mode="equipment_benchmark",
            requested_run_id=run_id,
        )
        status = "completed"
        try:
            await self.equipment.repository.save_snapshots(
                [
                    self.equipment._package_snapshot(
                        run_id=run_id,
                        package=package,
                        target_binding_ref=request.target,
                        test_principal_ref=principal.reference,
                    )
                ]
            )
            await self.runs.events.append(
                run_id=run_id,
                operation_id=f"{run_id}:benchmark:frozen",
                event_type="equipment_benchmark_frozen",
                evidence={
                    **manifest.model_dump(mode="json"),
                    "checksum": package["checksum"],
                    "selected_target": request.target,
                    "selected_task_ids": [task.id for task in plan["tasks"]],
                },
            )
            work = iter(
                enumerate(
                    (
                        (task, repetition)
                        for repetition in range(1, manifest.execution.repetitions + 1)
                        for task in plan["tasks"]
                    ),
                    start=1,
                )
            )

            async def worker() -> None:
                for sequence, (task, repetition) in work:
                    await self._run_case(
                        run_id,
                        sequence,
                        repetition,
                        task,
                        principal.reference,
                        request.target,
                        plan,
                    )

            async with asyncio.TaskGroup() as group:
                for _ in range(manifest.execution.concurrency):
                    group.create_task(worker())
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception:
            status = "error"
            raise
        finally:
            await self.repository.finish(run_id, status, [])
        from app.services.report_service import ReportService

        return await ReportService(self.runs, self.equipment.repository).build_json(run_id)

    async def _run_case(
        self,
        run_id: str,
        sequence: int,
        repetition: int,
        task: BenchmarkTask,
        principal_ref: str,
        target_name: str,
        plan: dict[str, Any],
    ) -> None:
        manifest: EquipmentBenchmarkManifest = plan["manifest"]
        target = plan["target"]
        step_id = await self.repository.begin_case(run_id, sequence, task.id, repetition)
        operation_id = f"{run_id}:benchmark:{sequence}"
        context = BenchmarkContext(
            run_id=run_id,
            operation_id=operation_id,
            task_id=task.id,
            repetition=repetition,
            target_name=target_name,
            target_config=target.config,
            allowed_hosts=target.allowed_hosts,
            secret_names=list(target.secret_refs),
            workspace_path=str(
                Path(self.equipment.catalog.settings.workspace_root).resolve()
                / f"benchmark-{run_id}-{sequence}"
            ),
            timeout_seconds=manifest.execution.timeout_seconds,
            metric_names=[metric.name for metric in manifest.metrics],
            test_principal_ref=principal_ref,
        )
        result: dict[str, Any] = {"outcome": "error", "metrics": {}}
        state: dict[str, Any] = {}
        durations: dict[str, float] = {}
        redactions = sensitive_values(task.model_dump())
        started = perf_counter()
        try:
            async with self.secrets.lease(target.secret_refs) as secrets:
                redactions = (*redactions, *secrets.redaction_values)
                environment = {
                    f"ATTACKER_BENCHMARK_SECRET_{name.upper()}": secrets.value(name)
                    for name in target.secret_refs
                }

                async def invoke(phase: str, **kwargs: Any) -> Any:
                    remaining = manifest.execution.timeout_seconds - (perf_counter() - started)
                    timeout = (
                        manifest.execution.cleanup_timeout_seconds
                        if phase == "cleanup"
                        else remaining
                    )
                    if timeout <= 0:
                        raise TimeoutError("benchmark task deadline exceeded")
                    phase_started = perf_counter()
                    try:
                        raw = await self.runner.execute(
                            plan["package"],
                            method=phase,
                            kwargs={
                                **kwargs,
                                "context": context.model_copy(
                                    update={"timeout_seconds": timeout}
                                ).model_dump(mode="json"),
                            },
                            workspace=Path(context.workspace_path),
                            timeout_seconds=timeout,
                            secret_environment=environment if phase != "evaluate" else {},
                        )
                        encoded = json.dumps(raw, allow_nan=False).encode()
                        if len(encoded) > manifest.execution.max_output_bytes:
                            raise ValueError("benchmark phase output exceeds declared byte limit")
                        return raw
                    finally:
                        durations[phase] = (perf_counter() - phase_started) * 1000

                try:
                    prepared = BenchmarkPreparation.model_validate(
                        await invoke("prepare", task=task.model_dump(mode="json"))
                    )
                    state = prepared.state
                    await self.repository.record_phase(
                        step_id, "prepared", redact(prepared.model_dump(mode="json"), redactions)
                    )
                    if not prepared.ready:
                        result.update(outcome="invalid", reason=prepared.reason or "task not ready")
                    else:
                        observation = BenchmarkObservation.model_validate(
                            await invoke("execute", task=task.model_dump(mode="json"), state=state)
                        )
                        validate_metrics(observation.metrics, manifest.metrics)
                        await self.repository.record_phase(
                            step_id,
                            "observed",
                            redact(observation.model_dump(mode="json"), redactions),
                        )
                        result["metrics"] = {
                            name: sample.model_dump(mode="json")
                            for name, sample in observation.metrics.items()
                        }
                        if observation.status != "success":
                            result.update(outcome=observation.status, reason=observation.reason)
                        else:
                            evaluation = BenchmarkEvaluation.model_validate(
                                await invoke(
                                    "evaluate",
                                    task=task.model_dump(mode="json"),
                                    observation=observation.model_dump(mode="json"),
                                )
                            )
                            validate_metrics(evaluation.metrics, manifest.metrics)
                            result.update(evaluation.model_dump(mode="json"))
                finally:
                    try:
                        cleaned = BenchmarkCleanup.model_validate(
                            await invoke("cleanup", state=state)
                        )
                        cleanup = {
                            "status": "cleaned" if cleaned.cleaned else "cleanup_failed",
                            "reason": cleaned.reason,
                        }
                    except Exception as exc:  # noqa: BLE001 - cleanup failure must remain visible
                        cleanup = {"status": "cleanup_failed", "reason": str(exc)}
                    result["cleanup"] = [redact(cleanup, redactions)]
                    await self.repository.record_phase(step_id, "cleaned", result["cleanup"][0])
        except asyncio.CancelledError:
            result.update(outcome="error", reason="cancelled")
            raise
        except TimeoutError:
            result.update(outcome="timeout", reason="benchmark task deadline exceeded")
        except PermissionError as exc:
            result.update(outcome="denied", reason=str(exc))
        except Exception as exc:  # noqa: BLE001 - preserve independent benchmark attempts
            result.update(outcome="error", reason=str(exc))
        finally:
            result["benchmark_duration_ms"] = (perf_counter() - started) * 1000
            result["phase_durations_ms"] = durations
            await self.repository.complete_case(step_id, redact(result, redactions))
