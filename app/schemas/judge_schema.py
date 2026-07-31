"""Target 响应、Evaluator 输出、冲突和校准指标的统一 Judge 契约。"""

from enum import Enum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.attack_sample_schema import RiskLevel


# 区分目标调用失败的传输类别，避免把模型或网络错误记为安全通过。
class TargetErrorType(str, Enum):
    connection_error = "connection_error"
    timeout = "timeout"


# 定义 Core Finding Aggregator 可以输出的三态结论。
class EvaluationVerdict(str, Enum):
    violation = "violation"
    safe = "safe"
    inconclusive = "inconclusive"


# 标记每个结果来自评估流水线的哪个阶段。
class EvaluationStage(str, Enum):
    transport = "transport"
    deterministic_rule = "deterministic_rule"
    structured_trace = "structured_trace"
    domain_evaluator = "domain_evaluator"
    model_judge = "model_judge"
    finding_aggregator = "finding_aggregator"


# 对连接、超时、拒绝、违规、证据不足和冲突分别建模。
class EvaluationIssue(str, Enum):
    connection_error = "connection_error"
    timeout = "timeout"
    server_error = "server_error"
    refusal = "refusal"
    violation = "violation"
    insufficient_evidence = "insufficient_evidence"
    conflict = "conflict"


# 区分校准集中的正常行为、安全拒绝和真实违规。
class CalibrationCategory(str, Enum):
    normal_behavior = "normal_behavior"
    safe_refusal = "safe_refusal"
    true_violation = "true_violation"


# 定义目标 Agent 返回内容、状态和调用错误信息。
class TargetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status_code: int | None = None
    body: dict[str, Any] | list[Any] | None = None
    text: str = ""
    latency_ms: int = Field(default=0, ge=0)
    response_bytes: int = Field(default=0, ge=0)
    error: str | None = None
    error_type: TargetErrorType | None = None

    @model_validator(mode="after")
    def validate_error(self) -> Self:
        if self.error is None and self.error_type is not None:
            raise ValueError("error_type requires error")
        if self.error is not None and self.error_type is None:
            self.error_type = TargetErrorType.connection_error
        return self


# 保存评估流水线中一个可追溯的组件判断。
class EvaluatorResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    verdict: EvaluationVerdict
    risk_level: RiskLevel = RiskLevel.low
    matched_patterns: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    stage: EvaluationStage
    evaluator_id: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)
    rule_version: str | None = Field(default=None, min_length=1)
    prompt_version: str | None = Field(default=None, min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    issues: list[EvaluationIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_version_source(self) -> Self:
        if self.stage == EvaluationStage.model_judge:
            if self.prompt_version is None:
                raise ValueError("model judge result requires prompt_version")
        elif self.rule_version is None:
            raise ValueError("non-model evaluator result requires rule_version")
        return self


# 定义 Core 聚合后的最终三态结论及全部组件依据。
class JudgeResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    verdict: EvaluationVerdict
    violated: bool | None
    risk_level: RiskLevel
    matched_patterns: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    evaluator_id: str = "core.finding_aggregator"
    evaluator_version: str = "1.0.0"
    rule_version: str = "finding-aggregation-v1"
    evidence_refs: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    issues: list[EvaluationIssue] = Field(default_factory=list)
    evaluator_results: list[EvaluatorResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_verdict_flag(self) -> Self:
        expected = {
            EvaluationVerdict.violation: True,
            EvaluationVerdict.safe: False,
            EvaluationVerdict.inconclusive: None,
        }[self.verdict]
        if self.violated is not expected:
            raise ValueError("violated must match verdict")
        return self


# 将一个校准 Case 的预期与 Core 聚合结论配对。
class CalibrationObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    category: CalibrationCategory
    expected_verdict: EvaluationVerdict
    actual_result: JudgeResult


# 汇总 Judge 在校准集上的误报、漏报和未决比例。
class CalibrationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_cases: int = Field(ge=0)
    false_positive_count: int = Field(ge=0)
    false_negative_count: int = Field(ge=0)
    inconclusive_count: int = Field(ge=0)
    false_positive_rate: float = Field(ge=0, le=1)
    false_negative_rate: float = Field(ge=0, le=1)
    inconclusive_rate: float = Field(ge=0, le=1)
    category_counts: dict[CalibrationCategory, int]


# 定义一次攻击执行从请求到判断结果的完整记录。
class AttackRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1)
    target_name: str
    sample_id: str
    request_body: dict[str, Any]
    target_response: TargetResponse
    judge_result: JudgeResult
