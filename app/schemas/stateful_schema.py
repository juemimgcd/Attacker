from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.attack_sample_schema import CaseKind, RiskLevel


class StatefulProfile(str, Enum):
    vulnerable = "vulnerable"
    hardened = "hardened"
    regressed = "regressed"


class StatefulEvaluatorType(str, Enum):
    memory_poisoning = "memory_poisoning"
    rag_poisoning = "rag_poisoning"
    identity_isolation = "identity_isolation"
    checkpoint_recovery = "checkpoint_recovery"


class StateIdentity(BaseModel):
    tenant_id: str
    user_id: str
    session_id: str


class StatefulCase(BaseModel):
    id: str
    name: str
    stage: Literal["stateful"] = "stateful"
    category: str
    severity: RiskLevel
    kind: CaseKind
    evaluator_type: StatefulEvaluatorType
    attacker: StateIdentity
    observer: StateIdentity
    namespace: str
    poison_marker: str
    query: str | None = None
    expected_violation: str
    expected_outcomes: dict[StatefulProfile, Literal["violation", "safe"]]
    required_evidence: list[str] = Field(
        default_factory=lambda: ["state_snapshot", "evaluation", "cleanup"]
    )


class LoadedStatefulDataset(BaseModel):
    name: str
    version: str
    source_path: Path
    sha256: str
    cases: list[StatefulCase]
    snapshot: dict[str, Any]


class StatefulRunRequest(BaseModel):
    profile: StatefulProfile = StatefulProfile.vulnerable
    dataset_path: str = "samples/stateful/phase3.yaml"
    case_ids: list[str] | None = None
    target_name: str = "isolated-stateful-sandbox"


class ReplayRunRequest(BaseModel):
    profile: StatefulProfile


class MemoryEvidence(BaseModel):
    resource_id: str
    tenant_id: str
    user_id: str
    session_id: str
    namespace: str
    content_summary: str
    provenance: str
    poisoned: bool
    active: bool


class RetrievalDocumentEvidence(BaseModel):
    document_id: str
    rank: int = Field(ge=1)
    source: str
    tenant_id: str
    user_id: str
    allowed: bool
    filter_reason: str
    poisoned: bool


class RetrievalTrace(BaseModel):
    query_summary: str
    documents: list[RetrievalDocumentEvidence]
    permission_filter: dict[str, Any]


class RecoveryEvidence(BaseModel):
    thread_id: str
    policy_revalidated: bool
    operation_attempts: int = Field(ge=1)
    committed_operations: int = Field(ge=0)


class CleanupResult(BaseModel):
    scope_id: str
    cleaned_count: int = Field(ge=0)
    remaining_active_count: int = Field(ge=0)
    outside_scope_affected_count: int = Field(ge=0)


class StatefulEvaluationResult(BaseModel):
    outcome: Literal["violation", "safe", "inconclusive", "error"]
    violated: bool
    risk_level: RiskLevel
    reason: str
    matched_rules: list[str] = Field(default_factory=list)
    evidence_complete: bool


class ReplayDiff(BaseModel):
    fixed: list[str] = Field(default_factory=list)
    new: list[str] = Field(default_factory=list)
    persistent: list[str] = Field(default_factory=list)
    regressed: list[str] = Field(default_factory=list)
