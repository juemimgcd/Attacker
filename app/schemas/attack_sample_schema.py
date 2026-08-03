"""黑盒攻击样本及数据集的受信任输入模型。"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


# 定义攻击样本的风险等级枚举。
class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


# 定义单条攻击样本的结构化字段。
class AttackSample(BaseModel):
    id: str
    name: str
    category: str
    severity: RiskLevel = RiskLevel.medium
    role: str = "user"
    prompt: str
    expected_violation: str
    judge_patterns: list[str] = Field(default_factory=list)


class CaseKind(str, Enum):
    attack = "attack"
    control = "control"


class EvaluatorType(str, Enum):
    direct_prompt_injection = "direct_prompt_injection"
    indirect_prompt_injection = "indirect_prompt_injection"
    obfuscated_prompt_injection = "obfuscated_prompt_injection"
    jailbreak = "jailbreak"
    instruction_hierarchy = "instruction_hierarchy"
    system_prompt_leakage = "system_prompt_leakage"
    sensitive_data_canary = "sensitive_data_canary"
    data_exfiltration = "data_exfiltration"
    output_integrity = "output_integrity"
    multi_turn_pollution = "multi_turn_pollution"
    resource_consumption = "resource_consumption"


class EvaluatorRules(BaseModel):
    type: EvaluatorType
    violation_patterns: list[str] = Field(default_factory=list)
    exact_match_patterns: list[str] = Field(default_factory=list)
    line_match_patterns: list[str] = Field(default_factory=list)
    refusal_patterns: list[str] = Field(
        default_factory=lambda: [
            "cannot comply",
            "can't comply",
            "cannot reveal",
            "won't reveal",
            "not authorized",
            "拒绝",
            "不能提供",
        ]
    )
    canary: str | None = None
    max_latency_ms: int | None = Field(default=None, gt=0)
    max_response_bytes: int | None = Field(default=None, gt=0)


class BlackBoxCase(BaseModel):
    id: str
    name: str
    stage: Literal["black_box"] = "black_box"
    category: str
    severity: RiskLevel
    kind: CaseKind
    prompts: list[str] = Field(min_length=1)
    expected_violation: str
    expected_outcome: Literal["violation", "refused", "safe"]
    evaluator: EvaluatorRules
    technique_ids: list[str] = Field(default_factory=list)
    attack_variant: str | None = None
    delivery_mode: Literal[
        "direct",
        "multi_turn",
        "embedded_untrusted_content",
        "target_fixture",
    ] = "direct"
    setup_requirements: list[str] = Field(default_factory=list)
    target_capabilities: list[str] = Field(default_factory=lambda: ["http_json"])
    requires_approval: bool = False
    required_evidence: list[str] = Field(
        default_factory=lambda: ["target_request", "target_response", "evaluator_result"]
    )
    cleanup_steps: list[str] = Field(default_factory=list)
    redact_fields: list[str] = Field(
        default_factory=lambda: ["authorization", "token", "api_key", "secret"]
    )
    repeat_count: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def validate_delivery_requirements(self) -> "BlackBoxCase":
        if self.delivery_mode == "multi_turn" and len(self.prompts) < 2:
            raise ValueError("multi_turn cases require at least two prompts")
        if self.delivery_mode == "target_fixture" and not self.setup_requirements:
            raise ValueError("target_fixture cases require setup_requirements")
        return self
