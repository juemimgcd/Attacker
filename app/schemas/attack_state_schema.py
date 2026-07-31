"""自适应运行中覆盖度、停止原因、失败计数和预算状态的数据模型。"""

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


# 定义 Run 的控制流状态；终止原因由 stop_reason 单独表达。
class RunStatus(str, Enum):
    running = "running"
    waiting_approval = "waiting_approval"
    paused = "paused"
    stopped = "stopped"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


# 限定所有允许持久化的终止原因。
class StopReason(str, Enum):
    completed = "completed"
    budget_exhausted = "budget_exhausted"
    no_information_gain = "no_information_gain"
    loop_detected = "loop_detected"
    policy_terminated = "policy_terminated"
    target_unavailable = "target_unavailable"
    planner_failed = "planner_failed"
    cancelled = "cancelled"


# 定义 Planner 不可用时由 Run 快照预先选择的确定性行为。
class PlannerFallbackMode(str, Enum):
    fail_closed = "fail_closed"
    deterministic_fallback = "deterministic_fallback"
    pause = "pause"


# 保存一次实际降级所使用的配置和模型身份。
class PlannerFallbackSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: PlannerFallbackMode
    reason: str = Field(min_length=1)
    actual_model_id: str = Field(min_length=1)
    actual_provider_id: str = Field(min_length=1)


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

    # 次数和时长由 Core 预先约束；实际 Provider 成本允许在单次调用后越过上限并触发停止。
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
    status: RunStatus = RunStatus.running
    stop_reason: StopReason | None = None
    checkpoint_ref: str | None = Field(default=None, min_length=1)
    planner_fallback_mode: PlannerFallbackMode = PlannerFallbackMode.fail_closed
    planner_fallback_snapshot: PlannerFallbackSnapshot | None = None
    last_state_fingerprint: str | None = Field(default=None, min_length=1)
    consecutive_no_gain_steps: int = Field(default=0, ge=0)
    repeated_state_count: int = Field(default=0, ge=0)
    planner_failure_count: int = Field(default=0, ge=0)
    target_transport_failure_count: int = Field(default=0, ge=0)

    # 保证非终止状态没有终止原因，终止状态一定有终止原因。
    @model_validator(mode="after")
    def validate_status_and_stop_reason(self) -> Self:
        terminal_statuses = {
            RunStatus.stopped,
            RunStatus.completed,
            RunStatus.failed,
            RunStatus.cancelled,
        }
        if self.status in terminal_statuses and self.stop_reason is None:
            raise ValueError("terminal run status requires stop_reason")
        if self.status not in terminal_statuses and self.stop_reason is not None:
            raise ValueError("non-terminal run status cannot have stop_reason")
        if (
            self.status in {RunStatus.waiting_approval, RunStatus.paused}
            and self.checkpoint_ref is None
        ):
            raise ValueError("waiting or paused run requires checkpoint_ref")
        return self
