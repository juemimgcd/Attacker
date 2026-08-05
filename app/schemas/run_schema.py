"""确定性黑盒 Run、预算、Evaluation、Finding 与报告的数据模型。"""

from enum import Enum
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, Field

from app.schemas.attack_sample_schema import BlackBoxCase, RiskLevel
from app.schemas.judge_schema import TargetResponse
from app.schemas.target_schema import TargetConfig


class EvaluationOutcome(str, Enum):
    violation = "violation"
    refused = "refused"
    safe = "safe"
    error = "error"
    budget_aborted = "budget_aborted"
    not_evaluable = "not_evaluable"


class RunBudget(BaseModel):
    max_cases: int = Field(default=64, ge=1, le=1_000)
    max_target_calls: int = Field(default=96, ge=1, le=10_000)
    max_duration_seconds: float = Field(default=300.0, gt=0, le=86_400)
    max_response_bytes: int = Field(default=1_048_576, ge=1, le=100_000_000)


class DeterministicRunRequest(BaseModel):
    target: TargetConfig
    dataset_path: str = "samples/blackbox/phase1.yaml"
    case_ids: list[str] | None = None
    fixture_evidence_refs: dict[
        str,
        Annotated[str, Field(min_length=1, max_length=500)],
    ] = Field(default_factory=dict)
    budget: RunBudget = Field(default_factory=RunBudget)


class EvaluationResult(BaseModel):
    outcome: EvaluationOutcome
    violated: bool
    risk_level: RiskLevel
    matched_patterns: list[str] = Field(default_factory=list)
    reason: str
    evaluator_type: str
    evidence_complete: bool = True


class TargetCallEvidence(BaseModel):
    request_body: dict[str, Any]
    response: TargetResponse


class CaseRunResult(BaseModel):
    case: BlackBoxCase
    outcome: EvaluationOutcome
    calls: list[TargetCallEvidence]
    evaluation: EvaluationResult
    budget: RunBudget
    precondition_evidence: dict[str, str] = Field(default_factory=dict)


class LoadedDataset(BaseModel):
    name: str
    version: str
    source_path: Path
    sha256: str
    cases: list[BlackBoxCase]
    snapshot: dict[str, Any]
