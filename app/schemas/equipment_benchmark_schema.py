"""Self-contained Benchmark packages and their execution, observation and grading contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.equipment_schema import Compatibility, TestPrincipal, TrustLevel


class BenchmarkModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


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
    config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    allowed_hosts: list[str] = Field(default_factory=list)
    parallel_safe: bool = False

    @model_validator(mode="after")
    def validate_secrets(self) -> BenchmarkTarget:
        import re

        from app.equipment.security import sensitive_values, validate_secret_reference

        names = list(self.secret_refs)
        if any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None for name in names):
            raise ValueError("secret names must be environment-safe identifiers")
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("secret names must be case-insensitively unique")
        for reference in self.secret_refs.values():
            validate_secret_reference(reference)
        if sensitive_values(self.config):
            raise ValueError("target config must not contain credentials; use secret_refs")
        return self


class BenchmarkExecution(BenchmarkModel):
    concurrency: int = Field(default=1, ge=1, le=32)
    repetitions: int = Field(default=1, ge=1, le=100)
    timeout_seconds: float = Field(default=120, gt=0, le=900)
    cleanup_timeout_seconds: float = Field(default=30, gt=0, le=120)
    max_output_bytes: int = Field(default=1048576, ge=1024, le=10485760)


class BenchmarkContext(BenchmarkModel):
    run_id: str
    operation_id: str
    task_id: str
    repetition: int = Field(ge=1)
    target_name: str
    target_config: dict[str, Any]
    allowed_hosts: list[str]
    secret_names: list[str]
    workspace_path: str
    timeout_seconds: float = Field(gt=0)
    metric_names: list[str]
    test_principal_ref: str


class BenchmarkPreparation(BenchmarkModel):
    ready: bool = True
    reason: str | None = None
    state: dict[str, Any] = Field(default_factory=dict)


class BenchmarkObservation(BenchmarkModel):
    status: Literal["success", "error", "timeout", "denied"]
    output: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, MetricSample] = Field(default_factory=dict)
    reason: str | None = None


class BenchmarkCleanup(BenchmarkModel):
    cleaned: bool
    reason: str | None = None


class EquipmentBenchmarkManifest(BenchmarkModel):
    schema_version: Literal["benchmark.v2"]
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]*$")
    name: str
    version: str
    description: str
    attacker_compatibility: Compatibility
    entrypoint: str
    tasks_file: str
    task_schema: str
    target_schema: str
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
    test_principal_ref: str


class EquipmentBenchmarkRequest(BenchmarkModel):
    target: str
    version: str | None = None
    task_ids: list[str] | None = Field(default=None, min_length=1, max_length=10000)
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
