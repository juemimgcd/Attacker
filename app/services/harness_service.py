from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.equipment.json_schema import validate_instance
from app.equipment.runner import EquipmentProtocolError, EquipmentRunner
from app.equipment.security import SecretBroker, redact, summarize
from app.repositories.equipment_repository import EquipmentRepository
from app.schemas.equipment_schema import (
    CapabilityContractManifest,
    ExecutionBudget,
    HarnessPolicy,
    PackageType,
    ProviderContext,
    ProviderResult,
    SkillContext,
    SkillDryRunRequest,
    SkillManifest,
    SkillResult,
)
from app.services.equipment_service import EquipmentService
from conf.settings import EquipmentSettings


class PolicyDeniedError(PermissionError):
    pass


class HarnessService:
    def __init__(
        self,
        repository: EquipmentRepository,
        equipment_service: EquipmentService,
        runner: EquipmentRunner,
        settings: EquipmentSettings,
        secret_broker: SecretBroker | None = None,
    ) -> None:
        self.repository = repository
        self.equipment_service = equipment_service
        self.runner = runner
        self.settings = settings
        self.secret_broker = secret_broker or SecretBroker()

    async def healthcheck(self, instance_id: str) -> dict[str, Any]:
        instance = await self.repository.get_provider_instance(instance_id)
        package = await self.repository.get_package(
            PackageType.provider,
            instance["provider_package_id"],
            instance["provider_version"],
        )
        package = self.equipment_service.materialize_package(package, instance["package_checksum"])
        if not package["enabled"] or not instance["enabled"]:
            raise PolicyDeniedError("Provider package and instance must both be enabled")
        context = ProviderContext(
            run_id="dry-run",
            step_id="healthcheck",
            operation_id=f"healthcheck:{instance_id}:{uuid4()}",
            target_id="healthcheck",
            case_id="healthcheck",
            provider_package_id=package["package_id"],
            provider_version=package["version"],
            provider_checksum=package["checksum"],
            provider_instance_id=instance_id,
            config_revision=instance["config_revision"],
            secret_binding_revision=instance["secret_binding_revision"],
            capability_contract_checksum=None,
            test_principal_ref="control-plane:healthcheck",
            approved_host_set=instance["allowed_hosts"],
            budget=ExecutionBudget(max_provider_calls=1),
            config=instance["config"],
        )
        result = await self.runner.execute(
            package,
            method="healthcheck",
            kwargs={"context": context},
            workspace=self._workspace(context.operation_id),
            timeout_seconds=float(
                package["manifest"].get("healthcheck", {}).get("timeout_seconds", 5)
            ),
        )
        healthy = bool(result.get("healthy", result.get("status") == "healthy"))
        await self.repository.update_health(instance_id, "healthy" if healthy else "unhealthy")
        return {"instance_id": instance_id, **result}

    async def invoke_provider(
        self,
        *,
        capability: str,
        payload: dict[str, Any],
        context: ProviderContext,
        policy: HarnessPolicy,
        run_id: str | None = None,
        step_id: str | None = None,
    ) -> dict[str, Any]:
        instance = await self.repository.get_provider_instance(context.provider_instance_id)
        package = await self.repository.get_package(
            PackageType.provider,
            context.provider_package_id,
            context.provider_version,
        )
        package = self.equipment_service.materialize_package(package, context.provider_checksum)
        contract_package = await self.repository.get_package(
            PackageType.contract, capability, "1.0.0"
        )
        contract_package = self.equipment_service.materialize_package(
            contract_package, context.capability_contract_checksum
        )
        contract = CapabilityContractManifest.model_validate(contract_package["manifest"])
        self._gate_provider(
            capability=capability,
            context=context,
            policy=policy,
            package=package,
            instance=instance,
            contract_package=contract_package,
        )
        context = context.model_copy(
            update={
                "config": instance["config"],
                "approved_host_set": instance["allowed_hosts"],
            }
        )
        request_schema = self._schema(contract_package, contract.request_schema)
        validate_instance(payload, request_schema)
        existing, created = await self.repository.begin_execution(
            {
                "run_id": run_id,
                "step_id": step_id,
                "operation_id": context.operation_id,
                "package_id": package["package_id"],
                "package_version": package["version"],
                "package_checksum": package["checksum"],
                "provider_instance_id": instance["instance_id"],
                "config_revision": instance["config_revision"],
                "capability": capability,
                "contract_checksum": contract_package["checksum"],
                "test_principal_ref": context.test_principal_ref,
                "input_summary": summarize(payload),
            }
        )
        if not created:
            return existing
        try:
            with self.secret_broker.lease(instance["secret_refs"]) as secrets:
                raw_result = await self.runner.execute(
                    package,
                    method="invoke",
                    kwargs={
                        "capability": capability,
                        "payload": payload,
                        "context": context,
                    },
                    workspace=self._workspace(context.operation_id),
                    timeout_seconds=min(
                        context.budget.timeout_seconds,
                        contract.limits.timeout_seconds_max,
                    ),
                    secret_environment=secrets.provider_environment(list(instance["secret_refs"])),
                )
                result = ProviderResult.model_validate(raw_result)
                remaining_calls = policy.max_provider_calls - context.budget.provider_calls_used
                if result.physical_attempts > remaining_calls:
                    raise ValueError("Provider physical attempts exceed the remaining budget")
                if (
                    contract.idempotency.provider_retry == "forbidden"
                    and result.physical_attempts != 1
                ):
                    raise ValueError("Provider retried a Contract that forbids retries")
                response_bytes = len(
                    json.dumps(result.output, separators=(",", ":"), default=str).encode()
                )
                response_limit = min(
                    contract.limits.response_bytes_max,
                    int(package["manifest"]["runtime"]["max_response_bytes"]),
                )
                if response_bytes > response_limit:
                    raise ValueError("Provider response exceeds the Contract limit")
                validate_instance(
                    result.output,
                    self._schema(contract_package, contract.response_schema),
                )
                evidence_types = {item.evidence_type for item in result.evidence}
                missing_evidence = set(contract.evidence.required) - evidence_types
                if missing_evidence:
                    raise ValueError(
                        f"Provider result is missing required Evidence: {sorted(missing_evidence)}"
                    )
                safe_output = redact(result.output, secrets.redaction_values)
                safe_evidence = redact(
                    [item.model_dump(mode="json") for item in result.evidence],
                    secrets.redaction_values,
                )
        except TimeoutError:
            result = ProviderResult(
                status="timeout",
                error_code="provider_timeout",
                error_message="Provider execution timed out",
            )
            safe_output = {}
            safe_evidence = []
        except (ValueError, EquipmentProtocolError) as exc:
            result = ProviderResult(
                status="error",
                error_code="provider_protocol_error",
                error_message=str(exc),
            )
            safe_output = {}
            safe_evidence = []
        evidence = [
            {
                "event_type": (
                    "provider_call_completed"
                    if result.status == "success"
                    else "provider_call_failed"
                ),
                "package_id": package["package_id"],
                "package_version": package["version"],
                "package_checksum": package["checksum"],
                "provider_instance_id": instance["instance_id"],
                "config_revision": instance["config_revision"],
                "secret_binding_revision": instance["secret_binding_revision"],
                "capability": capability,
                "operation_id": context.operation_id,
                "test_principal_ref": context.test_principal_ref,
                "policy_decision": "allowed",
                "redacted": True,
                "evidence": safe_evidence,
            }
        ]
        completed = await self.repository.complete_execution(
            context.operation_id,
            status=result.status,
            output_summary=summarize(safe_output),
            evidence=evidence,
            physical_attempts=result.physical_attempts,
            error_code=result.error_code,
        )
        if result.resource_leases:
            completed["resource_leases"] = await self.repository.create_leases(
                run_id=run_id,
                operation_id=context.operation_id,
                provider_instance_id=instance["instance_id"],
                leases=[lease.model_dump(mode="json") for lease in result.resource_leases],
            )
        return completed

    async def cleanup_leases(
        self,
        *,
        run_id: str | None,
        test_principal_ref: str,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for lease in await self.repository.list_active_leases(run_id):
            instance = await self.repository.get_provider_instance(lease["provider_instance_id"])
            package = await self.repository.get_package(
                PackageType.provider,
                instance["provider_package_id"],
                instance["provider_version"],
            )
            package = self.equipment_service.materialize_package(
                package, instance["package_checksum"]
            )
            operation_id = f"cleanup:{lease['id']}"
            context = ProviderContext(
                run_id=run_id or "dry-run",
                step_id="cleanup",
                operation_id=operation_id,
                target_id="cleanup",
                case_id="cleanup",
                provider_package_id=package["package_id"],
                provider_version=package["version"],
                provider_checksum=package["checksum"],
                provider_instance_id=instance["instance_id"],
                config_revision=instance["config_revision"],
                secret_binding_revision=instance["secret_binding_revision"],
                test_principal_ref=test_principal_ref,
                approved_host_set=instance["allowed_hosts"],
                budget=ExecutionBudget(max_provider_calls=1, timeout_seconds=30),
                config=instance["config"],
            )
            try:
                result = await self.runner.execute(
                    package,
                    method="cleanup",
                    kwargs={"resource": lease, "context": context},
                    workspace=self._workspace(operation_id),
                    timeout_seconds=30,
                )
                if not result.get("cleaned", False):
                    raise RuntimeError(str(result.get("reason", "cleanup failed")))
                await self.repository.mark_lease_cleaned(lease["id"], operation_id)
                results.append({**lease, "status": "cleaned", "operation_id": operation_id})
            except (RuntimeError, ValueError, TimeoutError, EquipmentProtocolError) as exc:
                await self.repository.mark_lease_cleanup_failed(lease["id"], operation_id, str(exc))
                results.append(
                    {
                        **lease,
                        "status": "cleanup_failed",
                        "operation_id": operation_id,
                        "error": str(exc),
                    }
                )
        return results

    async def dry_run_skill(self, skill_id: str, request: SkillDryRunRequest) -> dict[str, Any]:
        package = await self.repository.get_package(PackageType.skill, skill_id)
        package = self.equipment_service.materialize_package(package)
        if not package["enabled"]:
            raise PolicyDeniedError("Skill package is disabled")
        manifest = SkillManifest.model_validate(package["manifest"])
        input_schema = self._schema(package, manifest.input_schema)
        validate_instance(request.payload, input_schema)
        bindings = {}
        for requirement in manifest.requires.capabilities:
            binding = await self.equipment_service.resolve(
                requirement.contract,
                explicit_instance_id=request.bindings.get(requirement.binding),
            )
            bindings[requirement.binding] = binding
        operation_id = f"dry-run:{skill_id}:{uuid4()}"
        context = SkillContext(
            run_id="dry-run",
            step_id="dry-run",
            operation_id=operation_id,
            case_id=request.case_id,
            target_id=request.target_id,
            test_principal_ref=request.test_principal.reference,
            allowed_capabilities=[item.contract for item in manifest.requires.capabilities],
            capability_bindings=bindings,
            budget=ExecutionBudget(
                max_provider_calls=manifest.execution.max_provider_calls,
                timeout_seconds=manifest.execution.timeout_seconds,
            ),
            workspace_path=str(self._workspace(operation_id)),
        )
        existing, created = await self.repository.begin_execution(
            {
                "operation_id": operation_id,
                "package_id": package["package_id"],
                "package_version": package["version"],
                "package_checksum": package["checksum"],
                "test_principal_ref": context.test_principal_ref,
                "input_summary": summarize(request.payload),
            }
        )
        if not created:
            return existing
        try:
            preparation = await self.runner.execute(
                package,
                method="prepare",
                kwargs={"context": context},
                workspace=self._workspace(operation_id),
                timeout_seconds=manifest.execution.timeout_seconds,
            )
            if not preparation.get("ready", False):
                raise ValueError(preparation.get("message") or "Skill is not ready")
            raw_result = await self.runner.execute(
                package,
                method="execute",
                kwargs={"payload": request.payload, "context": context},
                workspace=self._workspace(operation_id),
                timeout_seconds=manifest.execution.timeout_seconds,
            )
            result = SkillResult.model_validate(raw_result)
            validate_instance(result.output, self._schema(package, manifest.output_schema))
        except TimeoutError:
            result = SkillResult(
                status="timeout",
                error_code="skill_timeout",
                error_message="Skill execution timed out",
            )
        except (ValueError, EquipmentProtocolError) as exc:
            result = SkillResult(
                status="error",
                error_code="skill_protocol_error",
                error_message=str(exc),
            )
        return await self.repository.complete_execution(
            operation_id,
            status=result.status,
            output_summary=summarize(result.output),
            evidence=[
                {
                    "event_type": (
                        "skill_completed" if result.status == "success" else "skill_failed"
                    ),
                    "operation_id": operation_id,
                    "package_id": package["package_id"],
                    "package_version": package["version"],
                    "package_checksum": package["checksum"],
                    "test_principal_ref": context.test_principal_ref,
                    "redacted": True,
                    "evidence": [item.model_dump(mode="json") for item in result.evidence],
                }
            ],
            physical_attempts=1,
            error_code=result.error_code,
        )

    def _gate_provider(
        self,
        *,
        capability: str,
        context: ProviderContext,
        policy: HarnessPolicy,
        package: dict[str, Any],
        instance: dict[str, Any],
        contract_package: dict[str, Any],
    ) -> None:
        checks = {
            "provider package is disabled": package["enabled"],
            "provider instance is disabled": instance["enabled"],
            "package checksum changed": package["checksum"] == context.provider_checksum,
            "instance package checksum changed": (
                instance["package_checksum"] == context.provider_checksum
            ),
            "config revision changed": instance["config_revision"] == context.config_revision,
            "Secret binding revision changed": (
                instance["secret_binding_revision"] == context.secret_binding_revision
            ),
            "capability is not allowed": capability in policy.allowed_capabilities,
            "target is not allowed": context.target_id in policy.allowed_targets,
            "case is not allowed": context.case_id in policy.allowed_cases,
            "test principal is not allowed": (
                context.test_principal_ref in policy.allowed_principal_refs
            ),
            "provider call budget exhausted": (
                context.budget.provider_calls_used < policy.max_provider_calls
            ),
            "contract checksum changed": bool(contract_package["checksum"]),
            "Capability Contract is disabled": contract_package["enabled"],
        }
        manifest_hosts = set(
            package["manifest"].get("runtime", {}).get("network", {}).get("allowed_hosts", [])
        )
        checks["network allowlist exceeds the Provider Instance"] = set(
            context.approved_host_set
        ).issubset(instance["allowed_hosts"])
        checks["network allowlist exceeds the Provider manifest"] = set(
            context.approved_host_set
        ).issubset(manifest_hosts)
        checks["contract checksum changed"] = (
            context.capability_contract_checksum == contract_package["checksum"]
        )
        implements = {item["contract"] for item in package["manifest"].get("implements", [])}
        checks["Provider does not implement capability"] = capability in implements
        risk = contract_package["manifest"]["risk_level"]
        checks["high-risk capability is not approved"] = (
            risk not in {"high", "critical"} or capability in policy.approved_high_risk_capabilities
        )
        for message, passed in checks.items():
            if not passed:
                raise PolicyDeniedError(message)

    @staticmethod
    def _schema(package: dict[str, Any], filename: str) -> dict[str, Any]:
        path = Path(package["source_path"]) / filename
        return json.loads(path.read_text(encoding="utf-8"))

    def _workspace(self, operation_id: str) -> Path:
        safe_name = "".join(
            character if character.isalnum() or character in "._-" else "_"
            for character in operation_id
        )
        return Path(self.settings.workspace_root) / safe_name
