"""运行暂停、取消、撤销授权和恢复校验所需的控制面模型。"""

from datetime import UTC, datetime
from enum import Enum
from typing import Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.attack_state_schema import (
    PlannerFallbackMode,
    StopReason,
)


# 定义停止控制器允许返回的三种控制流动作。
class StopAction(str, Enum):
    continue_run = "continue_run"
    wait_for_approval = "wait_for_approval"
    stop = "stop"


# 定义软停止计数器的确定性阈值。
class StopLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_consecutive_no_gain_steps: int = Field(default=3, gt=0)
    max_repeated_state_count: int = Field(default=3, gt=0)
    max_planner_failures: int = Field(default=3, gt=0)
    max_target_transport_failures: int = Field(default=3, gt=0)


# 汇总本次停止判断需要、但不应由 Planner 决定的 Core 事实。
class StopContext(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    required_coverage_ids: list[str] = Field(default_factory=list)
    target_authorization_revoked: bool = False
    policy_termination_requested: bool = False
    cancelled: bool = False
    has_pending_approval: bool = False
    pending_approval_ref: str | None = Field(default=None, min_length=1)
    checkpoint_ref: str | None = Field(default=None, min_length=1)
    has_executable_candidates: bool = True

    # 等待审批必须同时具备审批引用和可恢复 checkpoint。
    @model_validator(mode="after")
    def validate_pending_approval(self) -> Self:
        if self.has_pending_approval:
            if self.pending_approval_ref is None:
                raise ValueError("pending approval requires pending_approval_ref")
            if self.checkpoint_ref is None:
                raise ValueError("pending approval requires checkpoint_ref")
        return self


# 返回停止判断及其可审计依据。
class StopDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: StopAction
    stop_reason: StopReason | None = None
    reason_code: str = Field(min_length=1)
    checkpoint_ref: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_action_fields(self) -> Self:
        if self.action == StopAction.stop and self.stop_reason is None:
            raise ValueError("stop action requires stop_reason")
        if self.action != StopAction.stop and self.stop_reason is not None:
            raise ValueError("non-stop action cannot include stop_reason")
        if self.action == StopAction.wait_for_approval and self.checkpoint_ref is None:
            raise ValueError("approval wait requires checkpoint_ref")
        return self


# 接收一次已持久化步骤的真实收益和标准化状态指纹。
class StepProgress(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    state_fingerprint: str = Field(min_length=1)
    coverage_delta: int = Field(default=0, ge=0)
    evidence_delta: int = Field(default=0, ge=0)
    finding_delta: int = Field(default=0, ge=0)
    planner_failed: bool = False
    target_transport_failed: bool = False


# 定义 Planner 降级后交还给编排层的下一步动作。
class FallbackAction(str, Enum):
    terminate = "terminate"
    pause = "pause"
    route_to_policy_gate = "route_to_policy_gate"


# 接收 Planner 失败时必须写入快照和 Event 的实际调用身份。
class PlannerUnavailableContext(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    reason: str = Field(min_length=1)
    actual_model_id: str = Field(min_length=1)
    actual_provider_id: str = Field(min_length=1)
    checkpoint_ref: str | None = Field(default=None, min_length=1)


# 保存一次确定性降级事件，供业务数据库持久化。
class FallbackEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: str = "planner_fallback"
    mode: PlannerFallbackMode
    reason: str
    actual_model_id: str
    actual_provider_id: str
    candidate_snapshot_id: str | None = None
    candidate_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# 返回降级后的状态、候选和强制路由信息。
class FallbackDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: FallbackAction
    stop_reason: StopReason | None = None
    candidate_snapshot_id: str | None = None
    candidate_id: str | None = None
    next_route: str | None = None
    event: FallbackEvent

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        if self.action == FallbackAction.terminate and self.stop_reason is None:
            raise ValueError("terminate fallback requires stop_reason")
        if self.action != FallbackAction.terminate and self.stop_reason is not None:
            raise ValueError("non-terminal fallback cannot include stop_reason")
        if self.action == FallbackAction.route_to_policy_gate:
            if self.candidate_snapshot_id is None or self.candidate_id is None:
                raise ValueError("deterministic fallback requires current candidate")
            if self.next_route != "policy_gate":
                raise ValueError("deterministic fallback must route to policy_gate")
        return self
