from __future__ import annotations

import hashlib
import json
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.equipment.json_schema import validate_instance
from app.equipment.metrics import EquipmentMetrics
from app.equipment.runner import EquipmentProtocolError, EquipmentRunner
from app.equipment.security import SecretBroker, redact, sensitive_values, summarize
from app.repositories.equipment_repository import EquipmentRepository
from app.schemas.equipment_schema import (
    CapabilityContractManifest,
    CapabilityResult,
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
        metrics: EquipmentMetrics | None = None,
    ) -> None:
        self.repository = repository
        self.equipment_service = equipment_service
        self.runner = runner
        self.settings = settings
        self.secret_broker = secret_broker or SecretBroker()
        self.metrics = metrics

    async def healthcheck(self, instance_id: str) -> dict[str, Any]:
        started = perf_counter()
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
        health_redaction_values: tuple[str, ...] = ()
        try:
            with self.secret_broker.lease(instance["secret_refs"]) as secrets:
                health_redaction_values = secrets.redaction_values
                result = await self.runner.execute(
                    package,
                    method="healthcheck",
                    kwargs={"context": context},
                    workspace=self._workspace(context.operation_id),
                    timeout_seconds=float(
                        package["manifest"].get("healthcheck", {}).get("timeout_seconds", 5)
                    ),
                    secret_environment=secrets.provider_environment(list(instance["secret_refs"])),
                )
        except (LookupError, TimeoutError, EquipmentProtocolError, ValueError) as exc:
            await self.repository.update_health(instance_id, "unhealthy")
            if self.metrics is not None:
                self.metrics.increment("provider_instance_error")
                self.metrics.observe(
                    "provider_instance_healthcheck",
                    (perf_counter() - started) * 1000,
                )
            safe_error = str(redact(str(exc), health_redaction_values))
            raise type(exc)(safe_error) from None
        healthy = bool(result.get("healthy", result.get("status") == "healthy"))
        await self.repository.update_health(instance_id, "healthy" if healthy else "unhealthy")
        if self.metrics is not None:
            self.metrics.increment(
                "provider_instance_healthy" if healthy else "provider_instance_error"
            )
            self.metrics.observe(
                "provider_instance_healthcheck",
                (perf_counter() - started) * 1000,
            )
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
        effective_run_id = run_id or (context.run_id if context.run_id != "dry-run" else None)
        frozen = effective_run_id is not None
        if frozen:
            provider_snapshot = await self.repository.get_snapshot(
                effective_run_id,
                PackageType.provider,
                context.provider_package_id,
                provider_instance_id=context.provider_instance_id,
            )
            contract_snapshot = await self.repository.get_snapshot(
                effective_run_id,
                PackageType.contract,
                capability,
                provider_instance_id=context.provider_instance_id,
            )
            self._verify_frozen_provider_context(
                context=context,
                capability=capability,
                provider_snapshot=provider_snapshot,
                contract_snapshot=contract_snapshot,
            )
            instance = await self.repository.get_provider_instance_revision(
                context.provider_instance_id,
                provider_snapshot["checksum"],
                str(provider_snapshot["config_revision"]),
                str(provider_snapshot["secret_binding_revision"]),
            )
            package = await self.repository.get_package(
                PackageType.provider,
                provider_snapshot["package_id"],
                provider_snapshot["version"],
            )
            package = self.equipment_service.materialize_package(
                package,
                provider_snapshot["checksum"],
            )
            contract_package = await self.repository.get_package(
                PackageType.contract,
                capability,
                contract_snapshot["version"],
            )
            contract_package = self.equipment_service.materialize_package(
                contract_package,
                contract_snapshot["checksum"],
            )
        else:
            instance = await self.repository.get_provider_instance(context.provider_instance_id)
            package = await self.repository.get_package(
                PackageType.provider,
                context.provider_package_id,
                context.provider_version,
            )
            package = self.equipment_service.materialize_package(
                package,
                context.provider_checksum,
            )
            contract_package = await self.repository.get_package(
                PackageType.contract,
                capability,
                "1.0.0",
            )
            contract_package = self.equipment_service.materialize_package(
                contract_package,
                context.capability_contract_checksum,
            )
        contract = CapabilityContractManifest.model_validate(contract_package["manifest"])
        self._gate_provider(
            capability=capability,
            context=context,
            policy=policy,
            package=package,
            instance=instance,
            contract_package=contract_package,
            frozen=frozen,
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
                "run_id": effective_run_id,
                "step_id": step_id if effective_run_id is not None else None,
                "operation_id": context.operation_id,
                "package_id": package["package_id"],
                "package_version": package["version"],
                "package_checksum": package["checksum"],
                "provider_instance_id": instance["instance_id"],
                "config_revision": instance["config_revision"],
                "secret_binding_revision": instance["secret_binding_revision"],
                "capability": capability,
                "contract_checksum": contract_package["checksum"],
                "test_principal_ref": context.test_principal_ref,
                "request_fingerprint": self._fingerprint(
                    {
                        "capability": capability,
                        "payload": payload,
                        "context": context.model_dump(mode="json"),
                    }
                ),
                "input_summary": summarize(payload),
            }
        )
        if not created:
            return existing
        started = perf_counter()
        observed_attempts = 1
        safe_output: dict[str, Any] = {}
        safe_evidence: list[dict[str, Any]] = []
        safe_leases: list[dict[str, Any]] = []
        provider_redaction_values: tuple[str, ...] = ()
        result = ProviderResult(status="error", error_code="provider_protocol_error")
        try:
            with self.secret_broker.lease(instance["secret_refs"]) as secrets:
                provider_redaction_values = secrets.redaction_values
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
                observed_attempts = result.physical_attempts
                result_bytes = len(
                    json.dumps(
                        result.model_dump(mode="json"),
                        separators=(",", ":"),
                        default=str,
                    ).encode()
                )
                if result_bytes > int(package["manifest"]["runtime"]["max_response_bytes"]):
                    raise ValueError("Provider result exceeds its declared byte limit")
                remaining_calls = (
                    min(
                        policy.max_provider_calls,
                        context.budget.max_provider_calls,
                    )
                    - context.budget.provider_calls_used
                )
                if result.physical_attempts > remaining_calls:
                    raise ValueError("Provider physical attempts exceed the remaining budget")
                if (
                    contract.idempotency.provider_retry == "forbidden"
                    and result.physical_attempts != 1
                ):
                    raise ValueError("Provider retried a Contract that forbids retries")
                if result.status == "success":
                    response_bytes = len(
                        json.dumps(
                            result.output,
                            separators=(",", ":"),
                            default=str,
                        ).encode()
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
                            "Provider result is missing required Evidence: "
                            f"{sorted(missing_evidence)}"
                        )
                elif result.error_code not in contract.error_codes:
                    raise ValueError("Provider failure must use a declared Contract error code")
                safe_output = redact(result.output, secrets.redaction_values)
                safe_evidence = [
                    {
                        **item.model_dump(mode="json"),
                        "summary": redact(item.summary, secrets.redaction_values),
                    }
                    for item in result.evidence
                ]
                raw_leases = [item.model_dump(mode="json") for item in result.resource_leases]
                safe_leases = [
                    {
                        **lease,
                        "external_resource_id": redact(
                            lease["external_resource_id"],
                            secrets.redaction_values,
                        ),
                        "cleanup_payload": redact(
                            lease["cleanup_payload"],
                            secrets.redaction_values,
                        ),
                    }
                    for lease in raw_leases
                ]
                if safe_leases != raw_leases:
                    raise ValueError("Resource Lease contains secret or sensitive data")
                if safe_leases and result.status != "success":
                    raise ValueError("only successful Provider results may create Resource Leases")
                implemented = {
                    str(item["contract"]) for item in package["manifest"].get("implements", [])
                }
                unknown_cleanup = sorted(
                    {
                        str(lease["cleanup_contract"])
                        for lease in safe_leases
                        if str(lease["cleanup_contract"]) not in implemented
                    }
                )
                if unknown_cleanup:
                    raise ValueError(
                        f"Resource Lease uses undeclared cleanup Contracts: {unknown_cleanup}"
                    )
        except TimeoutError:
            result = ProviderResult(
                status="timeout",
                error_code="provider_timeout",
                error_message="Provider execution timed out",
                physical_attempts=observed_attempts,
            )
            safe_output = {}
            safe_evidence = []
            safe_leases = []
        except (ValueError, EquipmentProtocolError) as exc:
            result = ProviderResult(
                status="error",
                error_code="provider_protocol_error",
                error_message=str(redact(str(exc), provider_redaction_values)),
                physical_attempts=observed_attempts,
            )
            safe_output = {}
            safe_evidence = []
            safe_leases = []
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
        persisted_result = result.model_dump(mode="json")
        persisted_result["output"] = safe_output
        persisted_result["evidence"] = safe_evidence
        persisted_result["resource_leases"] = safe_leases
        persisted_result["external_operation_id"] = redact(
            result.external_operation_id,
            provider_redaction_values,
        )
        persisted_result["error_message"] = redact(
            result.error_message,
            provider_redaction_values,
        )
        completed = await self.repository.complete_execution(
            context.operation_id,
            status=result.status,
            result=persisted_result,
            output_summary=summarize(safe_output),
            evidence=evidence,
            physical_attempts=result.physical_attempts,
            error_code=result.error_code,
            leases=safe_leases,
        )
        if self.metrics is not None:
            self.metrics.increment("provider_call")
            self.metrics.increment(
                "provider_physical_attempt",
                result.physical_attempts,
            )
            if result.status != "success":
                self.metrics.increment("provider_error")
            if result.status == "timeout":
                self.metrics.increment("provider_timeout")
            self.metrics.observe(
                "provider_call",
                (perf_counter() - started) * 1000,
            )
        return completed

    async def execute_skill(
        self,
        *,
        skill_id: str,
        payload: dict[str, Any],
        context: SkillContext,
        policy: HarnessPolicy,
        run_id: str | None = None,
        step_id: str | None = None,
    ) -> dict[str, Any]:
        effective_run_id = run_id or (context.run_id if context.run_id != "dry-run" else None)
        if effective_run_id is not None:
            snapshot = await self.repository.get_snapshot(
                effective_run_id,
                PackageType.skill,
                skill_id,
            )
            if snapshot["test_principal_ref"] != context.test_principal_ref:
                raise PolicyDeniedError("test principal differs from Run snapshot")
            package = await self.repository.get_package(
                PackageType.skill,
                skill_id,
                snapshot["version"],
            )
            package = self.equipment_service.materialize_package(
                package,
                snapshot["checksum"],
            )
        else:
            package = await self.repository.get_package(PackageType.skill, skill_id)
            package = self.equipment_service.materialize_package(package)
            if not package["enabled"]:
                raise PolicyDeniedError("Skill package is disabled")

        manifest = SkillManifest.model_validate(package["manifest"])
        validate_instance(payload, self._schema(package, manifest.input_schema))
        requirements = {
            requirement.binding: requirement.contract
            for requirement in manifest.requires.capabilities
        }
        self._gate_skill(
            context=context,
            policy=policy,
            requirements=requirements,
        )
        existing, created = await self.repository.begin_execution(
            {
                "run_id": effective_run_id,
                "step_id": step_id if effective_run_id is not None else None,
                "operation_id": context.operation_id,
                "package_id": package["package_id"],
                "package_version": package["version"],
                "package_checksum": package["checksum"],
                "test_principal_ref": context.test_principal_ref,
                "request_fingerprint": self._fingerprint(
                    {
                        "payload": payload,
                        "context": context.model_dump(mode="json"),
                    }
                ),
                "input_summary": summarize(payload),
            }
        )
        if not created:
            return existing
        started = perf_counter()
        skill_redaction_values = sensitive_values(payload)

        capability_results: dict[str, CapabilityResult] = {}
        request_fingerprints: dict[str, str] = {}
        broker_leases: list[dict[str, Any]] = []
        skill_attempts = 0
        provider_calls = 0
        result = SkillResult(status="error", error_code="skill_protocol_error")
        try:
            preparation = await self.runner.execute(
                package,
                method="prepare",
                kwargs={"context": context},
                workspace=self._workspace(context.operation_id),
                timeout_seconds=min(
                    manifest.execution.timeout_seconds,
                    policy.max_duration_seconds,
                ),
            )
            if not preparation.get("ready", False):
                raise ValueError(preparation.get("message") or "Skill is not ready")

            max_steps = min(manifest.execution.max_steps, policy.max_steps)
            for _ in range(max_steps):
                remaining_duration = policy.max_duration_seconds - (perf_counter() - started)
                if remaining_duration <= 0:
                    raise TimeoutError
                skill_attempts += 1
                invocation_payload = dict(payload)
                if capability_results:
                    invocation_payload["capability_results"] = [
                        item.model_dump(mode="json") for item in capability_results.values()
                    ]
                raw_result = await self.runner.execute(
                    package,
                    method="execute",
                    kwargs={"payload": invocation_payload, "context": context},
                    workspace=self._workspace(context.operation_id),
                    timeout_seconds=min(
                        manifest.execution.timeout_seconds,
                        remaining_duration,
                    ),
                )
                result = SkillResult.model_validate(raw_result)
                result_bytes = len(
                    json.dumps(
                        result.model_dump(mode="json"),
                        separators=(",", ":"),
                        default=str,
                    ).encode()
                )
                if result_bytes > manifest.execution.max_output_bytes:
                    raise ValueError("Skill result exceeds its declared byte limit")
                if not result.capability_requests:
                    if result.status == "success":
                        validate_instance(
                            result.output,
                            self._schema(package, manifest.output_schema),
                        )
                    break
                if result.status != "success":
                    raise ValueError(
                        "only a successful Skill continuation may request Capabilities"
                    )

                for capability_request in result.capability_requests:
                    capability = requirements.get(capability_request.binding)
                    if capability is None:
                        raise PolicyDeniedError(
                            f"Skill requested undeclared binding {capability_request.binding}"
                        )
                    if capability not in context.allowed_capabilities:
                        raise PolicyDeniedError(
                            f"Skill context does not allow Capability {capability}"
                        )
                    fingerprint = json.dumps(
                        capability_request.payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                    previous_fingerprint = request_fingerprints.get(capability_request.request_id)
                    if previous_fingerprint is not None:
                        if previous_fingerprint != fingerprint:
                            raise ValueError(
                                "Skill reused a capability request ID with different payload"
                            )
                        continue
                    if provider_calls >= min(
                        manifest.execution.max_provider_calls,
                        policy.max_provider_calls,
                    ):
                        raise PolicyDeniedError("Skill provider call budget exhausted")
                    provider_remaining_duration = policy.max_duration_seconds - (
                        perf_counter() - started
                    )
                    if provider_remaining_duration <= 0:
                        raise TimeoutError
                    binding = context.capability_bindings.get(capability_request.binding)
                    if binding is None or binding.capability != capability:
                        raise PolicyDeniedError(f"missing binding for {capability_request.binding}")
                    instance = await self.repository.get_provider_instance_revision(
                        binding.provider_instance_id,
                        binding.provider_checksum,
                        binding.config_revision,
                        binding.secret_binding_revision,
                    )
                    provider_operation_id = (
                        f"{context.operation_id}:capability:{capability_request.request_id}"
                    )
                    provider_context = ProviderContext(
                        run_id=context.run_id,
                        step_id=context.step_id,
                        operation_id=provider_operation_id,
                        target_id=context.target_id,
                        case_id=context.case_id,
                        provider_package_id=binding.provider_package_id,
                        provider_version=binding.provider_version,
                        provider_checksum=binding.provider_checksum,
                        provider_instance_id=binding.provider_instance_id,
                        config_revision=binding.config_revision,
                        secret_binding_revision=binding.secret_binding_revision,
                        capability_contract_checksum=binding.contract_checksum,
                        test_principal_ref=context.test_principal_ref,
                        approved_host_set=instance["allowed_hosts"],
                        budget=context.budget.model_copy(
                            update={
                                "max_provider_calls": min(
                                    context.budget.max_provider_calls,
                                    manifest.execution.max_provider_calls,
                                    policy.max_provider_calls,
                                ),
                                "provider_calls_used": provider_calls,
                                "timeout_seconds": min(
                                    context.budget.timeout_seconds,
                                    provider_remaining_duration,
                                ),
                            }
                        ),
                        config=instance["config"],
                    )
                    provider_execution = await self.invoke_provider(
                        capability=capability,
                        payload=capability_request.payload,
                        context=provider_context,
                        policy=policy,
                        run_id=effective_run_id,
                        step_id=step_id,
                    )
                    broker_leases.extend(provider_execution.get("resource_leases", []))
                    provider_calls += int(provider_execution.get("physical_attempts", 1))
                    request_fingerprints[capability_request.request_id] = fingerprint
                    capability_results[capability_request.request_id] = CapabilityResult(
                        request_id=capability_request.request_id,
                        binding=capability_request.binding,
                        capability=capability,
                        status=provider_execution["status"],
                        output=provider_execution.get("output", {}),
                        operation_id=provider_operation_id,
                        error_code=provider_execution.get("error_code"),
                    )
            else:
                raise ValueError("Skill exceeded its step budget")
        except TimeoutError:
            result = SkillResult(
                status="timeout",
                error_code="skill_timeout",
                error_message="Skill execution timed out",
                provider_calls=provider_calls,
            )
        except (PolicyDeniedError, ValueError, EquipmentProtocolError) as exc:
            result = SkillResult(
                status="denied" if isinstance(exc, PolicyDeniedError) else "error",
                error_code=(
                    "skill_policy_denied"
                    if isinstance(exc, PolicyDeniedError)
                    else "skill_protocol_error"
                ),
                error_message=str(exc),
                provider_calls=provider_calls,
            )

        safe_output = redact(result.output, skill_redaction_values)
        safe_evidence = [
            {
                **item.model_dump(mode="json"),
                "summary": redact(item.summary, skill_redaction_values),
            }
            for item in result.evidence
        ]
        safe_result = result.model_dump(mode="json")
        safe_result["output"] = safe_output
        safe_result["evidence"] = safe_evidence
        safe_result["capability_requests"] = [
            {
                **request.model_dump(mode="json"),
                "payload": redact(request.payload, skill_redaction_values),
            }
            for request in result.capability_requests
        ]
        safe_result["error_message"] = redact(
            result.error_message,
            skill_redaction_values,
        )
        completed = await self.repository.complete_execution(
            context.operation_id,
            status=result.status,
            result=safe_result,
            output_summary=summarize(safe_output),
            evidence=[
                {
                    "event_type": (
                        "skill_completed" if result.status == "success" else "skill_failed"
                    ),
                    "operation_id": context.operation_id,
                    "package_id": package["package_id"],
                    "package_version": package["version"],
                    "package_checksum": package["checksum"],
                    "test_principal_ref": context.test_principal_ref,
                    "provider_calls": provider_calls,
                    "redacted": True,
                    "evidence": safe_evidence,
                }
            ],
            physical_attempts=max(skill_attempts, 1),
            error_code=result.error_code,
        )
        if self.metrics is not None:
            self.metrics.increment("skill_execution")
            if result.status == "timeout":
                self.metrics.increment("skill_timeout")
            if result.status == "denied":
                self.metrics.increment("capability_denied")
            self.metrics.observe(
                "skill_execution",
                (perf_counter() - started) * 1000,
            )
        if broker_leases:
            completed["resource_leases"] = broker_leases
        return completed

    async def cleanup_leases(
        self,
        *,
        run_id: str | None,
        test_principal_ref: str,
        lease_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for lease in await self.repository.list_active_leases(run_id):
            if lease_ids is not None and lease["id"] not in lease_ids:
                continue
            operation_id = f"cleanup:{lease['id']}"
            await self.repository.mark_lease_cleanup_started(
                lease["id"],
                operation_id,
            )
            cleanup_redaction_values: tuple[str, ...] = ()
            try:
                if lease["run_id"] is not None:
                    provider_snapshots = [
                        snapshot
                        for snapshot in await self.repository.list_snapshots(lease["run_id"])
                        if snapshot["package_type"] == PackageType.provider.value
                        and snapshot["provider_instance_id"] == lease["provider_instance_id"]
                    ]
                    if len(provider_snapshots) != 1:
                        raise ValueError(
                            "Resource Lease does not have exactly one Provider snapshot"
                        )
                    provider_snapshot = provider_snapshots[0]
                    instance = await self.repository.get_provider_instance_revision(
                        lease["provider_instance_id"],
                        provider_snapshot["checksum"],
                        provider_snapshot["config_revision"],
                        provider_snapshot["secret_binding_revision"],
                    )
                    package = await self.repository.get_package(
                        PackageType.provider,
                        provider_snapshot["package_id"],
                        provider_snapshot["version"],
                    )
                    package = self.equipment_service.materialize_package(
                        package,
                        provider_snapshot["checksum"],
                    )
                else:
                    instance = await self.repository.get_provider_instance(
                        lease["provider_instance_id"]
                    )
                    package = await self.repository.get_package(
                        PackageType.provider,
                        instance["provider_package_id"],
                        instance["provider_version"],
                    )
                    package = self.equipment_service.materialize_package(
                        package,
                        instance["package_checksum"],
                    )
                cleanup_contract_id = lease["cleanup_contract"]
                if lease["run_id"] is not None:
                    cleanup_snapshot = await self.repository.get_snapshot(
                        lease["run_id"],
                        PackageType.contract,
                        cleanup_contract_id,
                        provider_instance_id=lease["provider_instance_id"],
                    )
                    contract_package = await self.repository.get_package(
                        PackageType.contract,
                        cleanup_contract_id,
                        cleanup_snapshot["version"],
                    )
                    contract_package = self.equipment_service.materialize_package(
                        contract_package,
                        cleanup_snapshot["checksum"],
                    )
                else:
                    contract_package = await self.repository.get_package(
                        PackageType.contract,
                        cleanup_contract_id,
                    )
                    if not contract_package["enabled"]:
                        raise PolicyDeniedError("Cleanup Capability Contract is disabled")
                    contract_package = self.equipment_service.materialize_package(contract_package)
                cleanup_contract = CapabilityContractManifest.model_validate(
                    contract_package["manifest"]
                )
                if cleanup_contract.side_effect != "resource_cleanup":
                    raise ValueError("Resource Lease cleanup Contract is not a cleanup capability")
                implements = {
                    item["contract"] for item in package["manifest"].get("implements", [])
                }
                if cleanup_contract_id not in implements:
                    raise ValueError(
                        "Provider does not implement the Resource Lease cleanup Contract"
                    )
                validate_instance(
                    lease["cleanup_payload"],
                    self._schema(
                        contract_package,
                        cleanup_contract.request_schema,
                    ),
                )
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
                    budget=ExecutionBudget(
                        max_provider_calls=1,
                        timeout_seconds=30,
                    ),
                    config=instance["config"],
                )
                with self.secret_broker.lease(instance["secret_refs"]) as secrets:
                    cleanup_redaction_values = secrets.redaction_values
                    result = await self.runner.execute(
                        package,
                        method="cleanup",
                        kwargs={"resource": lease, "context": context},
                        workspace=self._workspace(operation_id),
                        timeout_seconds=30,
                        secret_environment=secrets.provider_environment(
                            list(instance["secret_refs"])
                        ),
                    )
                validate_instance(
                    result,
                    self._schema(
                        contract_package,
                        cleanup_contract.response_schema,
                    ),
                )
                if not result.get("cleaned", False):
                    raise RuntimeError(str(result.get("reason", "cleanup failed")))
                await self.repository.mark_lease_cleaned(lease["id"], operation_id)
                results.append({**lease, "status": "cleaned", "operation_id": operation_id})
            except (
                LookupError,
                OSError,
                RuntimeError,
                ValueError,
                TimeoutError,
                EquipmentProtocolError,
            ) as exc:
                safe_error = str(redact(str(exc), cleanup_redaction_values))
                await self.repository.mark_lease_cleanup_failed(
                    lease["id"],
                    operation_id,
                    safe_error,
                )
                if self.metrics is not None:
                    self.metrics.increment("cleanup_failure")
                results.append(
                    {
                        **lease,
                        "status": "cleanup_failed",
                        "operation_id": operation_id,
                        "error": safe_error,
                    }
                )
        return results

    async def recover_pending_cleanups(self) -> list[dict[str, Any]]:
        pending = await self.repository.list_pending_leases()
        groups: dict[tuple[str | None, str], set[str]] = {}
        for lease in pending:
            try:
                execution = await self.repository.get_execution(lease["created_by_operation_id"])
                principal_ref = execution["test_principal_ref"]
            except LookupError:
                principal_ref = "control-plane:cleanup-recovery"
            groups.setdefault((lease["run_id"], principal_ref), set()).add(lease["id"])
        results: list[dict[str, Any]] = []
        for (run_id, principal_ref), lease_ids in groups.items():
            results.extend(
                await self.cleanup_leases(
                    run_id=run_id,
                    test_principal_ref=principal_ref,
                    lease_ids=lease_ids,
                )
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
        policy = HarnessPolicy(
            allowed_capabilities=context.allowed_capabilities,
            allowed_targets=[context.target_id],
            allowed_cases=[context.case_id],
            allowed_principal_refs=[context.test_principal_ref],
            approved_high_risk_capabilities=context.allowed_capabilities,
            max_provider_calls=manifest.execution.max_provider_calls,
            max_steps=manifest.execution.max_steps,
            max_duration_seconds=manifest.execution.timeout_seconds,
        )
        result = await self.execute_skill(
            skill_id=skill_id,
            payload=request.payload,
            context=context,
            policy=policy,
        )
        lease_ids = {lease["id"] for lease in result.get("resource_leases", [])}
        if lease_ids:
            result["cleanup"] = await self.cleanup_leases(
                run_id=None,
                test_principal_ref=context.test_principal_ref,
                lease_ids=lease_ids,
            )
        return result

    def _gate_skill(
        self,
        *,
        context: SkillContext,
        policy: HarnessPolicy,
        requirements: dict[str, str],
    ) -> None:
        declared_capabilities = set(requirements.values())
        checks = {
            "Skill capability allowance exceeds its Manifest": (
                set(context.allowed_capabilities).issubset(declared_capabilities)
            ),
            "Skill capability is not allowed by Run Policy": (
                declared_capabilities.issubset(policy.allowed_capabilities)
            ),
            "Skill target is not allowed": context.target_id in policy.allowed_targets,
            "Skill case is not allowed": context.case_id in policy.allowed_cases,
            "Skill test principal is not allowed": (
                context.test_principal_ref in policy.allowed_principal_refs
            ),
        }
        for message, passed in checks.items():
            if not passed:
                if self.metrics is not None:
                    self.metrics.increment("capability_denied")
                raise PolicyDeniedError(message)

    def _gate_provider(
        self,
        *,
        capability: str,
        context: ProviderContext,
        policy: HarnessPolicy,
        package: dict[str, Any],
        instance: dict[str, Any],
        contract_package: dict[str, Any],
        frozen: bool = False,
    ) -> None:
        checks = {
            "provider package is disabled": frozen or package["enabled"],
            "provider instance is disabled": frozen or instance["enabled"],
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
                context.budget.provider_calls_used
                < min(
                    policy.max_provider_calls,
                    context.budget.max_provider_calls,
                )
            ),
            "contract checksum changed": bool(contract_package["checksum"]),
            "Capability Contract is disabled": frozen or contract_package["enabled"],
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
                if self.metrics is not None:
                    self.metrics.increment("capability_denied")
                    if "checksum" in message.lower():
                        self.metrics.increment("package_checksum_mismatch")
                raise PolicyDeniedError(message)

    def _verify_frozen_provider_context(
        self,
        *,
        context: ProviderContext,
        capability: str,
        provider_snapshot: dict[str, Any],
        contract_snapshot: dict[str, Any],
    ) -> None:
        checks = {
            "provider version differs from Run snapshot": (
                context.provider_version == provider_snapshot["version"]
            ),
            "provider checksum differs from Run snapshot": (
                context.provider_checksum == provider_snapshot["checksum"]
            ),
            "Provider Instance differs from Run snapshot": (
                context.provider_instance_id == provider_snapshot["provider_instance_id"]
            ),
            "config revision differs from Run snapshot": (
                context.config_revision == provider_snapshot["config_revision"]
            ),
            "Secret binding revision differs from Run snapshot": (
                context.secret_binding_revision == provider_snapshot["secret_binding_revision"]
            ),
            "Capability Contract differs from Run snapshot": (
                contract_snapshot["capability_contract_id"] == capability
            ),
            "Capability Contract checksum differs from Run snapshot": (
                context.capability_contract_checksum
                == contract_snapshot["capability_contract_checksum"]
            ),
            "test principal differs from Run snapshot": (
                context.test_principal_ref == provider_snapshot["test_principal_ref"]
            ),
        }
        for message, passed in checks.items():
            if not passed:
                if self.metrics is not None:
                    self.metrics.increment("capability_denied")
                    if "checksum" in message.lower():
                        self.metrics.increment("package_checksum_mismatch")
                raise PolicyDeniedError(message)

    @staticmethod
    def _schema(package: dict[str, Any], filename: str) -> dict[str, Any]:
        path = Path(package["source_path"]) / filename
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _fingerprint(value: Any) -> str:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _workspace(self, operation_id: str) -> Path:
        safe_name = "".join(
            character if character.isalnum() or character in "._-" else "_"
            for character in operation_id
        )
        return Path(self.settings.workspace_root) / safe_name
