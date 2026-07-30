from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PackageType(StrEnum):
    provider = "provider"
    skill = "skill"
    casepack = "casepack"
    contract = "contract"


class TrustLevel(StrEnum):
    trusted_builtin = "trusted_builtin"
    trusted_enterprise = "trusted_enterprise"
    untrusted = "untrusted"


class Compatibility(BaseModel):
    min_version: str
    max_version: str


class ContractReference(BaseModel):
    contract: str


class SkillCapabilityRequirement(ContractReference):
    binding: str


class NetworkRuntime(BaseModel):
    required: bool = False
    allowed_hosts: list[str] = Field(default_factory=list)


class ProviderRuntime(BaseModel):
    network: NetworkRuntime = Field(default_factory=NetworkRuntime)
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    max_response_bytes: int = Field(default=1_048_576, gt=0)


class ProviderConfiguration(BaseModel):
    schema_file: str


class ProviderManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["provider.v1"]
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]*$")
    name: str
    version: str
    description: str
    attacker_compatibility: Compatibility
    entrypoint: str
    implements: list[ContractReference]
    configuration: ProviderConfiguration
    runtime: ProviderRuntime = Field(default_factory=ProviderRuntime)
    healthcheck: dict[str, Any] = Field(default_factory=dict)
    trust_level: TrustLevel = TrustLevel.trusted_enterprise
    tags: list[str] = Field(default_factory=list)


class SkillPermissions(BaseModel):
    network: bool = False
    filesystem: dict[str, list[str]] = Field(default_factory=dict)


class SkillExecution(BaseModel):
    timeout_seconds: float = Field(default=120, gt=0, le=900)
    max_steps: int = Field(default=20, gt=0)
    max_provider_calls: int = Field(default=10, ge=0)
    max_output_bytes: int = Field(default=1_048_576, gt=0)


class SkillRequirements(BaseModel):
    capabilities: list[SkillCapabilityRequirement] = Field(default_factory=list)


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["skill.v1"]
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]*$")
    name: str
    version: str
    description: str
    attacker_compatibility: Compatibility
    entrypoint: str
    types: list[
        Literal[
            "case_generator",
            "fixture",
            "executor",
            "trace_adapter",
            "evaluator",
            "cleanup",
            "report_enricher",
        ]
    ]
    requires: SkillRequirements = Field(default_factory=SkillRequirements)
    permissions: SkillPermissions = Field(default_factory=SkillPermissions)
    execution: SkillExecution = Field(default_factory=SkillExecution)
    input_schema: str
    output_schema: str
    cleanup: dict[str, Any] = Field(default_factory=dict)
    trust_level: TrustLevel = TrustLevel.trusted_enterprise
    tags: list[str] = Field(default_factory=list)


class CasePackManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["casepack.v1"]
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]*$")
    name: str
    version: str
    description: str
    attacker_compatibility: Compatibility
    cases_file: str
    required_capabilities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    trust_level: TrustLevel = TrustLevel.trusted_enterprise


class ContractIdempotency(BaseModel):
    operation_id_required: bool = True
    provider_retry: Literal["forbidden", "allowed"] = "forbidden"


class ContractEvidence(BaseModel):
    required: list[str] = Field(default_factory=list)


class ContractLimits(BaseModel):
    timeout_seconds_max: float = Field(default=120, gt=0)
    response_bytes_max: int = Field(default=1_048_576, gt=0)


class CapabilityContractManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["capability-contract.v1"]
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*\.v[0-9]+$")
    version: str
    attacker_compatibility: Compatibility
    request_schema: str
    response_schema: str
    side_effect: Literal["none", "read", "external_call", "resource_create", "resource_cleanup"]
    risk_level: Literal["low", "medium", "high", "critical"]
    idempotency: ContractIdempotency = Field(default_factory=ContractIdempotency)
    evidence: ContractEvidence = Field(default_factory=ContractEvidence)
    cleanup_contract: str | None = None
    limits: ContractLimits = Field(default_factory=ContractLimits)
    redaction: dict[str, list[str]] = Field(default_factory=dict)
    error_codes: list[str] = Field(default_factory=list)
    trust_level: TrustLevel = TrustLevel.trusted_builtin


Manifest = ProviderManifest | SkillManifest | CasePackManifest | CapabilityContractManifest


class DiscoveredPackage(BaseModel):
    package_type: PackageType
    package_id: str
    version: str
    source_path: str
    source_type: Literal["builtin", "local_directory", "offline_archive"] = "local_directory"
    source_ref: str
    checksum: str
    manifest: dict[str, Any]
    enabled: bool = False
    validation_status: Literal["valid", "invalid"]
    validation_errors: list[str] = Field(default_factory=list)
    signature_status: Literal["not_present", "not_required", "verified", "invalid", "revoked"]
    publisher_id: str | None = None
    signature_id: str | None = None


class ProviderInstanceCreate(BaseModel):
    instance_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    provider_package_id: str
    provider_version: str
    display_name: str
    environment: str
    config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    allowed_hosts: list[str] = Field(default_factory=list)
    enabled: bool = False


class ProviderInstanceView(ProviderInstanceCreate):
    package_checksum: str
    config_revision: str
    secret_binding_revision: str
    health_status: str


class TestPrincipal(BaseModel):
    principal_id: str
    tenant_id: str
    role_labels: list[str] = Field(default_factory=list)
    credential_ref: str | None = None
    session_scope_id: str

    @property
    def reference(self) -> str:
        return (
            f"principal:{self.principal_id}:tenant:{self.tenant_id}:session:{self.session_scope_id}"
        )


class CapabilityBindingRef(BaseModel):
    capability: str
    provider_instance_id: str
    provider_package_id: str
    provider_version: str
    provider_checksum: str
    config_revision: str
    secret_binding_revision: str
    contract_checksum: str


class ExecutionBudget(BaseModel):
    max_provider_calls: int = Field(default=10, ge=0)
    provider_calls_used: int = Field(default=0, ge=0)
    timeout_seconds: float = Field(default=120, gt=0)


class ProviderContext(BaseModel):
    run_id: str
    step_id: str
    operation_id: str
    target_id: str
    case_id: str
    provider_package_id: str
    provider_version: str
    provider_checksum: str
    provider_instance_id: str
    config_revision: str
    secret_binding_revision: str
    capability_contract_checksum: str | None = None
    test_principal_ref: str
    approved_host_set: list[str]
    budget: ExecutionBudget
    config: dict[str, Any] = Field(default_factory=dict)


class SkillContext(BaseModel):
    run_id: str
    step_id: str
    operation_id: str
    case_id: str
    target_id: str
    test_principal_ref: str
    allowed_capabilities: list[str]
    capability_bindings: dict[str, CapabilityBindingRef]
    budget: ExecutionBudget
    workspace_path: str
    resource_lease_refs: list[str] = Field(default_factory=list)


class EvidenceDraft(BaseModel):
    evidence_type: str
    summary: dict[str, Any]
    redacted: bool = True


class ResourceLeaseDraft(BaseModel):
    resource_type: str
    external_resource_id: str
    cleanup_contract: str
    cleanup_payload: dict[str, Any] = Field(default_factory=dict)


class ProviderResult(BaseModel):
    status: Literal["success", "denied", "error", "timeout"]
    output: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceDraft] = Field(default_factory=list)
    resource_leases: list[ResourceLeaseDraft] = Field(default_factory=list)
    external_operation_id: str | None = None
    physical_attempts: int = Field(default=1, ge=1)
    retryable: bool = False
    error_code: str | None = None
    error_message: str | None = None


class SkillPreparation(BaseModel):
    ready: bool = True
    message: str | None = None


class CapabilityRequest(BaseModel):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    binding: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CapabilityResult(BaseModel):
    request_id: str
    binding: str
    capability: str
    status: Literal["success", "denied", "error", "timeout"]
    output: dict[str, Any] = Field(default_factory=dict)
    operation_id: str
    error_code: str | None = None


class SkillResult(BaseModel):
    status: Literal["success", "denied", "error", "timeout"]
    output: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceDraft] = Field(default_factory=list)
    capability_requests: list[CapabilityRequest] = Field(default_factory=list)
    provider_calls: int = Field(default=0, ge=0)
    error_code: str | None = None
    error_message: str | None = None


class HarnessPolicy(BaseModel):
    allowed_capabilities: list[str]
    allowed_targets: list[str]
    allowed_cases: list[str]
    allowed_principal_refs: list[str]
    default_bindings: dict[str, str] = Field(default_factory=dict)
    approved_high_risk_capabilities: list[str] = Field(default_factory=list)
    max_provider_calls: int = Field(default=10, ge=0)
    max_steps: int = Field(default=20, ge=0)
    max_duration_seconds: float = Field(default=120, gt=0)


class SkillDryRunRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    target_id: str = "dry-run-target"
    case_id: str = "dry-run-case"
    test_principal: TestPrincipal = Field(
        default_factory=lambda: TestPrincipal(
            principal_id="dry-run",
            tenant_id="dry-run",
            session_scope_id="dry-run",
        )
    )
    bindings: dict[str, str] = Field(default_factory=dict)


class EquipmentReplayMode(StrEnum):
    evidence_reevaluate = "evidence_reevaluate"
    same_binding_rerun = "same_binding_rerun"
    upgrade_comparison = "upgrade_comparison"
