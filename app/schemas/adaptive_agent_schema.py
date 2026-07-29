from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.attack_sample_schema import RiskLevel
from app.schemas.attack_state_schema import CoverageStatus


class InformationGain(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class HypothesisStatus(str, Enum):
    pending = "pending"
    supported = "supported"
    rejected = "rejected"
    inconclusive = "inconclusive"


class CandidateFilterReason(str, Enum):
    disabled = "disabled"
    incompatible = "incompatible"
    target_not_allowed = "target_not_allowed"
    case_not_allowed = "case_not_allowed"
    capability_not_allowed = "capability_not_allowed"
    budget_insufficient = "budget_insufficient"
    already_completed = "already_completed"
    policy_denied = "policy_denied"
    repeat_limit = "repeat_limit"
    prerequisite_missing = "prerequisite_missing"
    invalid_binding = "invalid_binding"
    no_expected_gain = "no_expected_gain"


class PlannerReasonCode(str, Enum):
    deterministic_order = "deterministic_order"
    coverage_gap = "coverage_gap"
    evidence_gap = "evidence_gap"
    hypothesis_validation = "hypothesis_validation"
    control_pair = "control_pair"
    all_requirements_satisfied = "all_requirements_satisfied"
    no_candidates = "no_candidates"
    budget_exhausted = "budget_exhausted"


class ObservationSource(str, Enum):
    target = "target"
    tool_trace = "tool_trace"
    rag_document = "rag_document"
    skill_output = "skill_output"
    case_pack = "case_pack"


class FinishGateReason(str, Enum):
    accepted = "accepted"
    coverage_incomplete = "coverage_incomplete"
    control_missing = "control_missing"
    evidence_gap = "evidence_gap"
    pending_approval = "pending_approval"
    early_finish_denied = "early_finish_denied"
    no_candidates = "no_candidates"


class CandidateAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    test_principal_ref: str = Field(min_length=1)
    provider_instance_ref: str = Field(min_length=1)
    capability_contract: str = Field(min_length=1)
    enabled: bool = True
    compatible: bool = True
    risk_level: RiskLevel
    requires_approval: bool = False
    repeatable: bool = False
    max_repeats: int = Field(default=1, ge=1)
    prerequisite_action_ids: tuple[str, ...] = ()
    hypothesis_template_id: str = Field(min_length=1)
    coverage_tags: tuple[str, ...] = Field(min_length=1)
    expected_information_gain: InformationGain = InformationGain.medium
    estimated_target_calls: int = Field(default=1, ge=0)
    estimated_provider_calls: int = Field(default=0, ge=0)
    estimated_cost: Decimal = Field(default=Decimal(0), ge=0)
    is_control: bool = False
    similarity_key: str = Field(min_length=1)


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    test_principal_ref: str = Field(min_length=1)
    provider_instance_ref: str = Field(min_length=1)
    capability_contract: str = Field(min_length=1)
    risk_level: RiskLevel
    requires_approval: bool
    hypothesis_refs: tuple[str, ...] = ()
    coverage_tags: tuple[str, ...] = Field(min_length=1)
    expected_information_gain: InformationGain
    estimated_target_calls: int = Field(ge=0)
    estimated_provider_calls: int = Field(ge=0)
    estimated_cost: Decimal = Field(ge=0)
    is_control: bool
    priority: tuple[int, ...]


class CandidateRejection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str
    reason: CandidateFilterReason
    detail: str


class CandidateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    candidates: tuple[Candidate, ...]
    rejected: tuple[CandidateRejection, ...] = ()


class UntrustedObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_ref: str = Field(min_length=1)
    source: ObservationSource
    trust: Literal["untrusted"] = "untrusted"
    summary: str = Field(max_length=2_000)


class HypothesisFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_ref: str = Field(min_length=1)
    template_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    status: HypothesisStatus
    evidence_refs: tuple[str, ...] = ()


class CoverageFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: str = Field(min_length=1)
    status: CoverageStatus
    evidence_refs: tuple[str, ...] = ()


class InformationGainMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    coverage_delta: int = Field(ge=0)
    evidence_completeness_delta: int = Field(ge=0)
    confirmed_finding_delta: int = Field(ge=0)
    target_call_cost: int = Field(ge=0)
    planner_cost: int = Field(ge=0)
    duration_delta: float = Field(ge=0)


class FinishGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason_code: FinishGateReason
    detail: str


class PlannerCallSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str
    prompt_template_version: str
    prompt_checksum: str
    prompt_profile_id: str
    input_checksum: str
    model_id: str
    model_parameters: dict[str, int | float | str | bool]
    schema_version: str
    input_fact_refs: tuple[str, ...]


class DerivedCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    derived_case_id: str = Field(min_length=1)
    parent_case_id: str = Field(min_length=1)
    generator_id: str = Field(min_length=1)
    generator_version: str = Field(min_length=1)
    input_fact_refs: tuple[str, ...]
    output: dict[str, Any]
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_mode: Literal["deterministic"] | None = None
    deterministic_evidence_refs: tuple[str, ...] = ()
    deterministic_verified: bool = False

    @model_validator(mode="after")
    def validate_deterministic_verification(self) -> Self:
        if self.deterministic_verified:
            if self.verification_mode != "deterministic":
                raise ValueError("verified derived cases require deterministic replay")
            if not self.deterministic_evidence_refs:
                raise ValueError("verified derived cases require persisted evidence refs")
        elif self.verification_mode is not None or self.deterministic_evidence_refs:
            raise ValueError("unverified derived cases cannot carry verification facts")
        return self
