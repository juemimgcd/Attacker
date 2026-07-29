from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, model_validator

from app.schemas.adaptive_agent_schema import (
    Candidate,
    InformationGain,
    PlannerCallSnapshot,
    PlannerReasonCode,
    UntrustedObservation,
)
from app.schemas.attack_sample_schema import CaseKind, RiskLevel
from app.schemas.judge_schema import TargetResponse
from app.schemas.target_schema import TargetConfig


class ToolPolicyDecision(str, Enum):
    allow = "allow"
    deny = "deny"
    approval_required = "approval_required"


class GrayBoxOutcome(str, Enum):
    violation = "violation"
    safe = "safe"
    refused = "refused"
    inconclusive = "inconclusive"
    policy_denied = "policy_denied"
    approval_rejected = "approval_rejected"
    error = "error"
    budget_aborted = "budget_aborted"
    loop_aborted = "loop_aborted"


class GrayBoxEvaluatorType(str, Enum):
    unauthorized_tool = "unauthorized_tool"
    dangerous_parameters = "dangerous_parameters"
    approval_bypass = "approval_bypass"
    tool_output_injection = "tool_output_injection"
    planner_loop = "planner_loop"


class ToolEvent(BaseModel):
    tool_call_id: str
    tool_name: str
    caller_identity: str
    arguments_summary: dict[str, Any] = Field(default_factory=dict)
    policy_decision: ToolPolicyDecision
    executed: bool
    result_summary: str | None = None
    error: str | None = None
    output_trust: Literal["trusted", "untrusted"] = "trusted"
    sequence: int = Field(ge=1)


class PolicyEvent(BaseModel):
    tool_call_id: str
    policy_name: str
    decision: ToolPolicyDecision
    reason: str
    evaluated_identity: str
    sequence: int = Field(ge=1)


class ApprovalEvent(BaseModel):
    approval_id: str
    case_id: str
    status: Literal["pending", "approved", "rejected", "expired"]
    resolved_by: str | None = None
    reason: str | None = None


class ToolTraceEnvelope(BaseModel):
    tool_events: list[ToolEvent] = Field(default_factory=list)
    policy_events: list[PolicyEvent] = Field(default_factory=list)
    approval_events: list[ApprovalEvent] = Field(default_factory=list)


class TraceAdapterResult(BaseModel):
    trace: ToolTraceEnvelope = Field(default_factory=ToolTraceEnvelope)
    errors: list[str] = Field(default_factory=list)
    evidence_complete: bool


class GrayBoxCase(BaseModel):
    id: str
    name: str
    stage: Literal["gray_box"] = "gray_box"
    category: str
    severity: RiskLevel
    kind: CaseKind
    prompts: list[str] = Field(min_length=1)
    target_scenario: str
    expected_violation: str
    expected_outcome: GrayBoxOutcome
    evaluator_type: GrayBoxEvaluatorType
    allowed_tools: list[str] = Field(default_factory=list)
    forbidden_argument_patterns: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    max_repeated_tool_calls: int = Field(default=2, ge=1, le=20)
    enabled: bool = True
    compatible: bool = True
    capability_contract: str = "target.http.v1"
    provider_instance_ref: str = "core.http"
    hypothesis_template_id: str | None = None
    coverage_tags: list[str] = Field(default_factory=list)
    prerequisite_case_ids: list[str] = Field(default_factory=list)
    repeatable: bool = False
    max_action_repeats: int = Field(default=1, ge=1, le=20)
    expected_information_gain: InformationGain = InformationGain.medium
    required_evidence: list[str] = Field(
        default_factory=lambda: [
            "target_response",
            "tool_trace",
            "policy_trace",
            "evaluator_result",
        ]
    )
    redact_fields: list[str] = Field(
        default_factory=lambda: [
            "authorization",
            "token",
            "api_key",
            "secret",
            "password",
        ]
    )


class LoadedGrayBoxDataset(BaseModel):
    name: str
    version: str
    source_path: Path
    sha256: str
    cases: list[GrayBoxCase]
    snapshot: dict[str, Any]


class AttackPolicy(BaseModel):
    allowed_target_ids: set[str] = Field(default_factory=set)
    allowed_case_ids: set[str] = Field(default_factory=set)
    allowed_capability_contracts: set[str] = Field(default_factory=set)
    allowed_provider_instance_refs: set[str] = Field(default_factory=set)
    max_steps: int = Field(default=40, ge=1, le=1_000)
    max_target_calls: int = Field(default=20, ge=1, le=10_000)
    max_duration_seconds: float = Field(default=300.0, gt=0, le=86_400)
    approval_required_severities: set[RiskLevel] = Field(
        default_factory=lambda: {RiskLevel.critical}
    )
    max_planner_failures: int = Field(default=3, ge=1, le=20)
    max_repeated_decisions: int = Field(default=3, ge=1, le=20)
    max_action_repeats: int = Field(default=1, ge=1, le=20)
    stop_on_critical: bool = False
    allow_early_finish: bool = False


class FindingSummary(BaseModel):
    finding_ref: str | None = None
    case_id: str
    category: str
    risk_level: RiskLevel
    reason: str


class PlannerContext(BaseModel):
    candidate_snapshot_id: str
    candidates: list[Candidate]
    observations: list[UntrustedObservation]
    evidence_refs: list[str]
    finding_refs: list[str]
    hypothesis_refs: list[str]
    remaining_steps: int


class PlannerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["execute", "finish"]
    candidate_snapshot_id: str
    candidate_id: str | None = None
    reason_code: PlannerReasonCode
    evidence_refs: list[str] = Field(default_factory=list)
    hypothesis_refs: list[str] = Field(default_factory=list)
    expected_information_gain: InformationGain | None = None

    @model_validator(mode="after")
    def validate_candidate_id(self) -> "PlannerDecision":
        if self.action == "execute" and self.candidate_id is None:
            raise ValueError("execute requires candidate_id")
        if self.action == "execute" and self.expected_information_gain is None:
            raise ValueError("execute requires expected_information_gain")
        if self.action == "finish" and self.candidate_id is not None:
            raise ValueError("finish cannot include candidate_id")
        if self.action == "finish" and self.expected_information_gain is not None:
            raise ValueError("finish cannot include expected_information_gain")
        return self


class PlannerUsage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class PlannerResult(BaseModel):
    decision: PlannerDecision
    usage: PlannerUsage = Field(default_factory=PlannerUsage)
    backend: str
    call_snapshot: PlannerCallSnapshot


class PlannerConfig(BaseModel):
    backend: Literal["deterministic", "openai_compatible"] = "deterministic"
    endpoint: HttpUrl | None = None
    api_key: SecretStr | None = None
    model: str = "planner"
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    temperature: float = Field(default=0, ge=0, le=2)
    prompt_template_version: str = "planner-v2"

    @model_validator(mode="after")
    def validate_http_config(self) -> "PlannerConfig":
        if self.backend == "openai_compatible" and self.endpoint is None:
            raise ValueError("openai_compatible planner requires endpoint")
        return self


class GrayBoxRunRequest(BaseModel):
    target: TargetConfig
    dataset_path: str = "samples/graybox/phase2.yaml"
    case_ids: list[str] | None = None
    policy: AttackPolicy = Field(default_factory=AttackPolicy)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    test_principal_refs: list[str] = Field(
        default_factory=lambda: ["default-test-principal"],
        min_length=1,
    )
    baseline_run_id: str | None = None


class ApprovalResolveRequest(BaseModel):
    approved: bool
    resolved_by: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2_000)
    target: TargetConfig | None = None
    planner: PlannerConfig | None = None


class PolicyGateResult(BaseModel):
    decision: ToolPolicyDecision
    reason: str
    approval_id: str | None = None


class GrayBoxEvaluationResult(BaseModel):
    outcome: GrayBoxOutcome
    violated: bool
    risk_level: RiskLevel
    reason: str
    matched_rules: list[str] = Field(default_factory=list)
    evidence_complete: bool


class GrayBoxExecutionResult(BaseModel):
    case: GrayBoxCase
    request_body: dict[str, Any] | None = None
    response: TargetResponse | None = None
    trace: ToolTraceEnvelope = Field(default_factory=ToolTraceEnvelope)
    evaluation: GrayBoxEvaluationResult
    policy: PolicyGateResult
