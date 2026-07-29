from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# 区分 Planner 与 Model Judge，禁止共享配置和用量统计。
class PromptTask(str, Enum):
    planner = "planner"
    model_judge = "model_judge"


# 定义受控 Prompt 的输入上限。
class PromptLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_observations: int = Field(default=20, gt=0)
    max_fact_refs: int = Field(default=50, gt=0)
    max_chars_per_observation: int = Field(default=1000, gt=0)
    max_total_input_tokens: int = Field(default=4096, gt=0)


# 调用方只能提交结构化摘要与事实引用，不能提交 system prompt。
class ObservationSummary(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    observation_ref: str = Field(min_length=1)
    summary: str = Field(min_length=1)


# 保存经过脱敏和上限校验的实际模型输入事实。
class GovernedObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_ref: str
    summary: str


# 定义可选择的已批准 Prompt Profile。
class PromptProfile(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    profile_id: str = Field(min_length=1)
    task: PromptTask
    template_name: str = Field(min_length=1)
    template_version: str = Field(min_length=1)
    template_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved: bool = False
    allowed_callers: set[str] = Field(default_factory=set)
    compatible_schema_versions: set[str] = Field(default_factory=set)
    limits: PromptLimits = Field(default_factory=PromptLimits)


# 定义构建模型输入所需的窄请求；不存在可覆盖 Core prompt 的字段。
class PromptBuildRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    task: PromptTask
    profile_id: str = Field(min_length=1)
    caller_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    observations: list[ObservationSummary] = Field(default_factory=list)
    fact_refs: list[str] = Field(default_factory=list)
    model_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    model_parameters: dict[str, int | float | bool | None] = Field(
        default_factory=dict
    )


# 保存最终传给 Provider 的单条消息。
class PromptMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: str


# 保存可根据相同 Core 模板和规范化输入重建的模型调用快照。
class PromptSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: PromptTask
    profile_id: str
    template_name: str
    template_version: str
    template_checksum: str
    schema_version: str
    observations: list[GovernedObservation]
    fact_refs: list[str]
    model_id: str
    provider_id: str
    model_parameters: dict[str, Any]
    input_checksum: str


# 同时返回快照和实际消息，便于调用前持久化并在恢复时重建。
class PromptBuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot: PromptSnapshot
    messages: list[PromptMessage]


# 保存 Planner 或 Model Judge 独立的物理调用和用量统计。
class ModelUsageSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: PromptTask
    physical_call_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    estimated_cost: float = Field(default=0, ge=0)
