"""Agent 输入预算和可从 SQL 事实重建的历史摘要。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContextBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    window_tokens: int = Field(default=32768, gt=0)
    output_tokens: int = Field(default=2048, gt=0)
    margin_tokens: int = Field(default=1024, ge=0)

    @model_validator(mode="after")
    def validate_input_allowance(self) -> "ContextBudget":
        if self.window_tokens <= self.output_tokens + self.margin_tokens:
            raise ValueError("context window must leave room for model input")
        return self

    def input_limit(self, tool_schema_size: int = 0) -> int:
        return self.window_tokens - self.output_tokens - self.margin_tokens - tool_schema_size


class HistorySummary(BaseModel):
    """摘要的覆盖范围单独审计，不把 covered_ids 重复塞进模型输入。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = ""
    covered_ids: tuple[str, ...] = ()
    version: int = Field(default=0, ge=0)
