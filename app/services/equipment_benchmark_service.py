"""Run Equipment-defined benchmarks through the existing frozen Harness boundary."""

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
from app.equipment.security import redact, sensitive_values
from app.repositories.equipment_benchmark_repository import EquipmentBenchmarkRepository
from app.repositories.run_repository import RunRepository
from app.schemas.equipment_benchmark_schema import (
    BenchmarkEvaluation,
    BenchmarkRunPolicy,
    BenchmarkTask,
    BenchmarkTasks,
    EquipmentBenchmarkManifest,
    EquipmentBenchmarkRequest,
    LoadedEquipmentBenchmarkDataset,
)
from app.schemas.equipment_schema import (
    ExecutionBudget,
    HarnessPolicy,
    PackageType,
    SkillContext,
    SkillManifest,
)
from app.services.equipment_service import EquipmentService
from app.services.harness_service import HarnessService


class EquipmentBenchmarkService:
    def __init__(self, equipment: EquipmentService, harness: HarnessService) -> None:
        self.equipment = equipment
        self.harness = harness
        self.runs = RunRepository(equipment.repository.session_factory, equipment.repository.events)
        self.repository = EquipmentBenchmarkRepository(self.runs)

    async def _package(
        self, kind: PackageType, package_id: str, version: str | None = None
    ) -> dict[str, Any]:
        package = await self.equipment.get_package(kind, package_id, version)
        if package["validation_status"] != "valid" or not package["enabled"]:
            raise ValueError(f"{kind.value} {package_id} must be valid and enabled")
        return self.equipment.materialize_package(package)

    async def prepare(
        self, benchmark_id: str, request: EquipmentBenchmarkRequest
    ) -> dict[str, Any]:
        """Resolve all data and bindings before making any target calls."""
        benchmark = await self._package(PackageType.benchmark, benchmark_id, request.version)
        manifest = EquipmentBenchmarkManifest.model_validate(benchmark["manifest"])
        if request.target not in manifest.targets:
            raise ValueError(f"unknown benchmark target: {request.target}")
        target = manifest.targets[request.target]
        if manifest.execution.concurrency > 1 and not target.parallel_safe:
            raise ValueError("concurrent benchmark execution requires target.parallel_safe=true")
        casepack = await self._package(
            PackageType.casepack, manifest.casepack.id, manifest.casepack.version
        )
        if casepack["manifest"]["schema_version"] != "casepack.v2":
            raise ValueError("Equipment benchmarks require a casepack.v2 task dataset")
        tasks_path = Path(casepack["source_path"]) / casepack["manifest"]["cases_file"]
        tasks = BenchmarkTasks.model_validate(
            yaml.safe_load(tasks_path.read_text(encoding="utf-8"))
        )
        if request.task_ids is not None:
            selected = set(request.task_ids)
            missing = selected - {task.id for task in tasks.tasks}
            if missing:
                raise ValueError(f"unknown benchmark tasks: {sorted(missing)}")
            tasks.tasks = [task for task in tasks.tasks if task.id in selected]
        if len(tasks.tasks) * manifest.execution.repetitions > 10000:
            raise ValueError("a benchmark run is limited to 10000 task attempts")
        skill = await self._package(PackageType.skill, manifest.skill.id, manifest.skill.version)
        skill_manifest = SkillManifest.model_validate(skill["manifest"])
        if "evaluator" not in skill_manifest.types:
            raise ValueError("benchmark Skill must declare the evaluator type")
        requirements = skill_manifest.requires.capabilities
        required_names = {item.binding for item in requirements}
        if len(required_names) != len(requirements) or set(target.bindings) != required_names:
            raise ValueError("target bindings must exactly match the Skill's unique bindings")
        input_schema = json.loads(
            (Path(skill["source_path"]) / skill_manifest.input_schema).read_text(encoding="utf-8")
        )
        for task in tasks.tasks:
            validate_instance(
                {
                    "task": task.model_dump(mode="json"),
                    "metric_names": [metric.name for metric in manifest.metrics],
                },
                input_schema,
            )
        bindings = {}
        packages = [benchmark, casepack, skill]
        instances = {}
        contracts: list[tuple[dict[str, Any], str]] = []
        for requirement in requirements:
            binding = await self.equipment.resolve(
                requirement.contract, explicit_instance_id=target.bindings[requirement.binding]
            )
            bindings[requirement.binding] = binding
            instance = await self.equipment.repository.get_provider_instance_revision(
                binding.provider_instance_id,
                binding.provider_checksum,
                binding.config_revision,
                binding.secret_binding_revision,
            )
            instances[binding.provider_instance_id] = instance
            provider = await self._package(
                PackageType.provider, binding.provider_package_id, binding.provider_version
            )
            packages.append(provider)
            contract_id: str | None = requirement.contract
            seen: set[str] = set()
            while contract_id is not None:
                if contract_id in seen:
                    raise ValueError("cyclic cleanup contract references")
                seen.add(contract_id)
                contract = await self._package(PackageType.contract, contract_id)
                contracts.append((contract, binding.provider_instance_id))
                if (
                    contract_id == requirement.contract
                    and contract["checksum"] != binding.contract_checksum
                ):
                    raise ValueError("contract changed while resolving the benchmark")
                contract_id = contract["manifest"].get("cleanup_contract")
        declared_capabilities = {item.contract for item in requirements}
        required_capabilities = set(casepack["manifest"].get("required_capabilities", []))
        if not required_capabilities.issubset(declared_capabilities):
            raise ValueError(
                "benchmark Skill does not provide the CasePack's required capabilities"
            )
        return {
            "manifest": manifest,
            "tasks": tasks.tasks,
            "casepack": casepack,
            "bindings": bindings,
            "packages": packages,
            "instances": instances,
            "contracts": contracts,
        }

    async def run(self, benchmark_id: str, request: EquipmentBenchmarkRequest) -> dict[str, Any]:
        plan = await self.prepare(benchmark_id, request)
        manifest: EquipmentBenchmarkManifest = plan["manifest"]
        run_id = str(uuid4())
        principal = request.test_principal.model_copy(
            update={"session_scope_id": f"{request.test_principal.session_scope_id}:{run_id}"}
        )
        dataset_content = {
            "casepack_checksum": plan["casepack"]["checksum"],
            "tasks": [task.model_dump(mode="json") for task in plan["tasks"]],
            "repetitions": manifest.execution.repetitions,
        }
        dataset = LoadedEquipmentBenchmarkDataset(
            name=manifest.casepack.id,
            version=manifest.casepack.version,
            source_path=Path(plan["casepack"]["source_path"]),
            sha256=hashlib.sha256(
                json.dumps(dataset_content, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            cases=plan["tasks"] * manifest.execution.repetitions,
            snapshot=redact(dataset_content),
        )
        await self.runs.create_run(
            target_snapshot={
                "name": request.target,
                "endpoint": f"equipment://{manifest.id}/{request.target}",
                "bindings": {
                    name: binding.model_dump(mode="json")
                    for name, binding in plan["bindings"].items()
                },
            },
            dataset=dataset,
            budget=BenchmarkRunPolicy(
                **manifest.execution.model_dump(),
                approved_high_risk_capabilities=request.approved_high_risk_capabilities,
                test_principal_ref=principal.reference,
            ),
            mode="equipment_benchmark",
            requested_run_id=run_id,
        )
        status = "completed"
        cleanup: list[dict[str, Any]] = []
        try:
            await self._freeze(run_id, request.target, principal.reference, plan)
            work = iter(
                (task, repetition)
                for repetition in range(1, manifest.execution.repetitions + 1)
                for task in plan["tasks"]
            )
            numbered = iter(enumerate(work, start=1))

            async def worker() -> None:
                for sequence, (task, repetition) in numbered:
                    await self._run_case(
                        run_id, sequence, repetition, task, principal.reference, request, plan
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
            try:
                cleanup = await self.harness.cleanup_leases(
                    run_id=run_id, test_principal_ref=principal.reference
                )
                if any(item.get("status") == "cleanup_failed" for item in cleanup):
                    status = "cleanup_failed" if status == "completed" else status
            except Exception:
                status = "cleanup_failed" if status == "completed" else status
                raise
            finally:
                await self.repository.finish(run_id, status, cleanup)
        from app.services.report_service import ReportService

        return await ReportService(self.runs, self.equipment.repository).build_json(run_id)

    async def _freeze(
        self, run_id: str, target: str, principal_ref: str, plan: dict[str, Any]
    ) -> None:
        snapshots: dict[tuple[str, str, str | None], dict[str, Any]] = {}

        def add(package: dict[str, Any], **extra: Any) -> None:
            value = {
                **self.equipment._package_snapshot(
                    run_id=run_id,
                    package=package,
                    target_binding_ref=target,
                    test_principal_ref=principal_ref,
                ),
                **extra,
            }
            key = (value["package_type"], value["package_id"], value["provider_instance_id"])
            if key in snapshots and snapshots[key] != value:
                raise ValueError("conflicting benchmark package versions")
            snapshots[key] = value

        for package in plan["packages"]:
            if package["package_type"] != PackageType.provider.value:
                add(package)
                continue
            for instance in plan["instances"].values():
                if instance["provider_package_id"] != package["package_id"]:
                    continue
                if instance["package_checksum"] != package["checksum"]:
                    raise ValueError("provider changed while preparing benchmark bindings")
                add(
                    package,
                    provider_instance_id=instance["instance_id"],
                    config_revision=instance["config_revision"],
                    config_json=instance["config"],
                    secret_binding_revision=instance["secret_binding_revision"],
                )
        for contract, instance_id in plan["contracts"]:
            add(
                contract,
                provider_instance_id=instance_id,
                capability_contract_id=contract["package_id"],
                capability_contract_checksum=contract["checksum"],
            )
        await self.equipment.repository.save_snapshots(list(snapshots.values()))
        manifest: EquipmentBenchmarkManifest = plan["manifest"]
        await self.runs.events.append(
            run_id=run_id,
            operation_id=f"{run_id}:benchmark:frozen",
            event_type="equipment_benchmark_frozen",
            evidence={
                **manifest.model_dump(mode="json"),
                "selected_target": target,
                "selected_task_ids": [task.id for task in plan["tasks"]],
                "bindings": {
                    name: binding.model_dump(mode="json")
                    for name, binding in plan["bindings"].items()
                },
            },
        )

    async def _run_case(
        self,
        run_id: str,
        sequence: int,
        repetition: int,
        task: BenchmarkTask,
        principal_ref: str,
        request: EquipmentBenchmarkRequest,
        plan: dict[str, Any],
    ) -> None:
        manifest: EquipmentBenchmarkManifest = plan["manifest"]
        step_id = await self.repository.begin_case(run_id, sequence, task.id, repetition)
        operation_id = f"{run_id}:benchmark:{sequence}:skill"
        capabilities = [binding.capability for binding in plan["bindings"].values()]
        context = SkillContext(
            run_id=run_id,
            step_id=step_id,
            operation_id=operation_id,
            target_id=request.target,
            case_id=task.id,
            test_principal_ref=principal_ref,
            allowed_capabilities=capabilities,
            capability_bindings=plan["bindings"],
            budget=ExecutionBudget(
                max_provider_calls=manifest.execution.max_provider_calls,
                timeout_seconds=manifest.execution.timeout_seconds,
            ),
            workspace_path=str(self.harness._workspace(operation_id)),
        )
        policy = HarnessPolicy(
            allowed_capabilities=capabilities,
            allowed_targets=[request.target],
            allowed_cases=[task.id],
            allowed_principal_refs=[principal_ref],
            approved_high_risk_capabilities=request.approved_high_risk_capabilities,
            max_provider_calls=manifest.execution.max_provider_calls,
            max_steps=manifest.execution.max_steps,
            max_duration_seconds=manifest.execution.timeout_seconds,
        )
        result: dict[str, Any] = {"outcome": "error", "metrics": {}}
        started = perf_counter()
        try:
            execution = await self.harness.execute_skill(
                skill_id=manifest.skill.id,
                payload={
                    "task": task.model_dump(mode="json"),
                    "metric_names": [metric.name for metric in manifest.metrics],
                },
                context=context,
                policy=policy,
                run_id=run_id,
                step_id=step_id,
            )
            if execution["status"] != "success":
                result.update(
                    outcome=execution["status"],
                    reason=execution.get("error_code") or "Skill execution failed",
                )
            else:
                evaluation = BenchmarkEvaluation.model_validate(execution["output"]["benchmark"])
                validate_metrics(evaluation.metrics, manifest.metrics)
                result.update(evaluation.model_dump(mode="json"))
            result["execution_operation_id"] = operation_id
        except asyncio.CancelledError:
            result.update(outcome="error", reason="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - preserve every independent plugin attempt
            result.update(reason=str(redact(str(exc), sensitive_values(task.model_dump()))))
        finally:
            result["harness_duration_ms"] = (perf_counter() - started) * 1000
            try:
                leases = await self.equipment.repository.list_active_leases(run_id)
                lease_ids = {
                    lease["id"]
                    for lease in leases
                    if lease["created_by_operation_id"].startswith(f"{operation_id}:capability:")
                }
                if lease_ids:
                    result["cleanup"] = await self.harness.cleanup_leases(
                        run_id=run_id, test_principal_ref=principal_ref, lease_ids=lease_ids
                    )
            finally:
                await self.repository.complete_case(step_id, result)
