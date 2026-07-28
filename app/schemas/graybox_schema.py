from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, SecretStr, model_validator

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
    max_steps: int = Field(default=40, ge=1, le=1_000)
    max_target_calls: int = Field(default=20, ge=1, le=10_000)
    max_duration_seconds: float = Field(default=300.0, gt=0, le=86_400)
    approval_required_severities: set[RiskLevel] = Field(
        default_factory=lambda: {RiskLevel.critical}
    )
    max_planner_failures: int = Field(default=3, ge=1, le=20)
    max_repeated_decisions: int = Field(default=3, ge=1, le=20)
    stop_on_critical: bool = False


class CaseSummary(BaseModel):
    id: str
    name: str
    category: str
    severity: RiskLevel
    requires_approval: bool


class FindingSummary(BaseModel):
    case_id: str
    category: str
    risk_level: RiskLevel
    reason: str


class PlannerContext(BaseModel):
    allowed_cases: list[CaseSummary]
    completed_case_ids: list[str]
    finding_summaries: list[FindingSummary]
    remaining_steps: int


class PlannerDecision(BaseModel):
    action: Literal["execute_case", "finish_run"]
    case_id: str | None = None
    reason: str

    @model_validator(mode="after")
    def validate_case_id(self) -> "PlannerDecision":
        if self.action == "execute_case" and self.case_id is None:
            raise ValueError("execute_case requires case_id")
        if self.action == "finish_run" and self.case_id is not None:
            raise ValueError("finish_run cannot include case_id")
        return self


class PlannerUsage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class PlannerResult(BaseModel):
    decision: PlannerDecision
    usage: PlannerUsage = Field(default_factory=PlannerUsage)
    backend: str


class PlannerConfig(BaseModel):
    backend: Literal["deterministic", "openai_compatible"] = "deterministic"
    endpoint: HttpUrl | None = None
    api_key: SecretStr | None = None
    model: str = "planner"
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)

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
