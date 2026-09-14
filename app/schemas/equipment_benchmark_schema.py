"""Equipment-owned benchmark composition and portable, evidence-backed metric contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.equipment_schema import Compatibility, TestPrincipal, TrustLevel


class BenchmarkModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PackageReference(BenchmarkModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]*$")
    version: str = Field(min_length=1)


class BenchmarkTask(BenchmarkModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", max_length=160)
    payload: dict[str, Any]
    expected: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class BenchmarkTasks(BenchmarkModel):
    tasks: list[BenchmarkTask] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def unique_ids(self) -> BenchmarkTasks:
        if len({task.id for task in self.tasks}) != len(self.tasks):
            raise ValueError("duplicate benchmark task IDs")
        return self


class MetricDefinition(BenchmarkModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    unit: str = Field(min_length=1)
    aggregation: Literal["mean", "sum", "ratio"]
    scope: Literal["eligible", "passed"] = "eligible"
    description: str = Field(min_length=1)


class MetricSample(BenchmarkModel):
    value: float | None = Field(default=None, strict=True, ge=0)
    numerator: float | None = Field(default=None, strict=True, ge=0)
    denominator: float | None = Field(default=None, strict=True, gt=0)

    @model_validator(mode="after")
    def scalar_or_ratio(self) -> MetricSample:
        if self.value is not None:
            if self.numerator is not None or self.denominator is not None:
                raise ValueError("a metric must be a scalar or a ratio")
        elif self.numerator is None or self.denominator is None:
            raise ValueError("ratio metrics require numerator and positive denominator")
        elif self.numerator > self.denominator:
            raise ValueError("ratio numerator cannot exceed denominator")
        return self


class BenchmarkEvaluation(BenchmarkModel):
    outcome: Literal["passed", "failed", "inconclusive", "invalid"]
    reason: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(min_length=1)
    metrics: dict[str, MetricSample] = Field(default_factory=dict)


class BenchmarkTarget(BenchmarkModel):
    bindings: dict[str, str] = Field(min_length=1)
    parallel_safe: bool = False


class BenchmarkExecution(BenchmarkModel):
    concurrency: int = Field(default=1, ge=1, le=32)
    repetitions: int = Field(default=1, ge=1, le=100)
    timeout_seconds: float = Field(default=120, gt=0, le=900)
    max_steps: int = Field(default=20, ge=1, le=100)
    max_provider_calls: int = Field(default=10, ge=1, le=1000)


class EquipmentBenchmarkManifest(BenchmarkModel):
    schema_version: Literal["benchmark.v1"]
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]*$")
    name: str
    version: str
    description: str
    attacker_compatibility: Compatibility
    casepack: PackageReference
    skill: PackageReference
    targets: dict[str, BenchmarkTarget] = Field(min_length=1)
    metrics: list[MetricDefinition] = Field(default_factory=list)
    execution: BenchmarkExecution = Field(default_factory=BenchmarkExecution)
    tags: list[str] = Field(default_factory=list)
    trust_level: TrustLevel = TrustLevel.trusted_enterprise

    @model_validator(mode="after")
    def metric_names(self) -> EquipmentBenchmarkManifest:
        names = [metric.name for metric in self.metrics]
        if len(names) != len(set(names)) or "task_completion_rate" in names:
            raise ValueError("metric names must be unique; task_completion_rate is reserved")
        return self


class BenchmarkRunPolicy(BenchmarkExecution):
    approved_high_risk_capabilities: list[str] = Field(default_factory=list)
    test_principal_ref: str


class EquipmentBenchmarkRequest(BenchmarkModel):
    target: str
    version: str | None = None
    task_ids: list[str] | None = Field(default=None, min_length=1, max_length=10000)
    approved_high_risk_capabilities: list[str] = Field(default_factory=list)
    test_principal: TestPrincipal = Field(
        default_factory=lambda: TestPrincipal(
            principal_id="benchmark", tenant_id="benchmark", session_scope_id="benchmark"
        )
    )

    @model_validator(mode="after")
    def unique_selection(self) -> EquipmentBenchmarkRequest:
        if self.task_ids is not None and len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError("duplicate selected task IDs")
        return self


class LoadedEquipmentBenchmarkDataset(BenchmarkModel):
    name: str
    version: str
    source_path: Path
    sha256: str
    cases: list[BenchmarkTask]
    snapshot: dict[str, Any]
