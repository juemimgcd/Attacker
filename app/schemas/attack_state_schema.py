from decimal import Decimal
from enum import Enum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


# 定义单个风险覆盖项在当前运行中的控制流状态。
class CoverageStatus(str, Enum):
    not_started = "not_started"
    in_progress = "in_progress"
    covered = "covered"
    blocked = "blocked"
    inconclusive = "inconclusive"


# 定义运行预算上限及其当前消耗快照。
class RunBudgetSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    max_steps: int = Field(gt=0)
    max_target_calls: int = Field(gt=0)
    max_provider_calls: int = Field(ge=0)
    max_duration_seconds: float = Field(gt=0)
    max_cost: Decimal | None = Field(default=None, ge=0)
    steps_used: int = Field(default=0, ge=0)
    target_calls_used: int = Field(default=0, ge=0)
    provider_calls_used: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0, ge=0)
    cost_used: Decimal = Field(default=Decimal(0), ge=0)

    # 拒绝不可能的预算快照，避免恢复时出现负的剩余预算。
    @model_validator(mode="after")
    def validate_usage_within_limits(self) -> Self:
        if self.steps_used > self.max_steps:
            raise ValueError("steps_used cannot exceed max_steps")
        if self.target_calls_used > self.max_target_calls:
            raise ValueError("target_calls_used cannot exceed max_target_calls")
        if self.provider_calls_used > self.max_provider_calls:
            raise ValueError("provider_calls_used cannot exceed max_provider_calls")
        if self.elapsed_seconds > self.max_duration_seconds:
            raise ValueError("elapsed_seconds cannot exceed max_duration_seconds")
        if self.max_cost is not None and self.cost_used > self.max_cost:
            raise ValueError("cost_used cannot exceed max_cost")
        return self


# 定义调用被测目标时使用的测试主体；credential_ref 只能保存凭据引用。
class TestPrincipal(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    principal_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    role_labels: list[str] = Field(default_factory=list)
    credential_ref: str | None = Field(default=None, min_length=1)
    session_scope_id: str = Field(min_length=1)


# 定义 Adaptive Run 的最小控制流状态，业务事实仍以持久化 Event/Evidence 为准。
class AttackState(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    run_id: str = Field(min_length=1)
    goal_id: str = Field(min_length=1)
    test_principal_refs: list[str] = Field(default_factory=list)
    candidate_snapshot_id: str | None = Field(default=None, min_length=1)
    candidate_action_ids: list[str] = Field(default_factory=list)
    completed_action_ids: list[str] = Field(default_factory=list)
    denied_action_ids: list[str] = Field(default_factory=list)
    coverage: dict[str, CoverageStatus] = Field(default_factory=dict)
    hypothesis_refs: list[str] = Field(default_factory=list)
    observation_refs: list[str] = Field(default_factory=list)
    finding_refs: list[str] = Field(default_factory=list)
    budget: RunBudgetSnapshot
    consecutive_no_gain_steps: int = Field(default=0, ge=0)
    repeated_state_count: int = Field(default=0, ge=0)
    planner_failure_count: int = Field(default=0, ge=0)
