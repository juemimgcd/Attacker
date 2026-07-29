from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.prompt_schema import PromptMessage, PromptTask


class ProviderAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: int = Field(gt=0)
    status: Literal["success", "error", "timeout"]
    latency_ms: int = Field(ge=0)
    error_category: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_error_category(self) -> Self:
        if self.status == "success" and self.error_category is not None:
            raise ValueError("successful attempt cannot include an error category")
        if self.status != "success" and self.error_category is None:
            raise ValueError("failed attempt requires an error category")
        return self


class ModelProviderUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    physical_attempts: int = Field(gt=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    estimated_cost: Decimal = Field(default=Decimal(0), ge=0)
    attempts: tuple[ProviderAttempt, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_attempts(self) -> Self:
        if self.physical_attempts != len(self.attempts):
            raise ValueError("physical_attempts must equal the attempt list length")
        if [attempt.attempt for attempt in self.attempts] != list(
            range(1, self.physical_attempts + 1)
        ):
            raise ValueError("provider attempts must be contiguous and one-indexed")
        if self.latency_ms != sum(attempt.latency_ms for attempt in self.attempts):
            raise ValueError("latency_ms must equal the sum of physical attempt latency")
        return self


class ModelInferenceRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    contract: Literal["model.inference.v1"] = "model.inference.v1"
    operation_id: str = Field(min_length=1)
    task: PromptTask
    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    messages: tuple[PromptMessage, ...] = Field(min_length=2)
    response_schema_version: str = Field(min_length=1)
    temperature: float = Field(default=0, ge=0, le=2)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_physical_attempts: int = Field(default=1, ge=1, le=10)


class ModelInferenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    structured_output: dict[str, Any]
    usage: ModelProviderUsage

    @model_validator(mode="after")
    def validate_successful_result(self) -> Self:
        if self.usage.attempts[-1].status != "success":
            raise ValueError("successful inference result requires a final successful attempt")
        if sum(attempt.status == "success" for attempt in self.usage.attempts) != 1:
            raise ValueError("provider result must contain exactly one successful attempt")
        return self
