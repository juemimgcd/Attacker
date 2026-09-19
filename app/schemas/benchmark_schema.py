"""Public benchmark inputs and observations, distinct from Core-authored HTTP Cases."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

BenchmarkName = Literal["agentdojo", "injecagent"]


class BenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    suite: str
    category: str
    kind: Literal["attack", "control"]
    user_task_id: str
    injection_task_id: str | None = None
    prompt: str
    attack_goal: str
    definition: dict[str, Any]
    validation_error: str | None = None


class BenchmarkManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["attacker-benchmark-v1"] = "attacker-benchmark-v1"
    benchmark: BenchmarkName
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    version: str
    source_url: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    license: str
    protocol: dict[str, str]
    environment: dict[str, Any]
    cases: list[BenchmarkCase] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def unique_cases(self) -> "BenchmarkManifest":
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate benchmark case IDs")
        return self


class BenchmarkObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    status: Literal["evaluated", "error", "not_evaluable"]
    attack_success: StrictBool | None = None
    utility: StrictBool | None = None
    reason: str
    artifact: str | None = None
    artifact_sha256: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def no_conclusion_on_error(self) -> "BenchmarkObservation":
        if self.status != "evaluated" and (
            self.attack_success is not None or self.utility is not None
        ):
            raise ValueError("incomplete execution cannot carry success metrics")
        if self.status == "evaluated" and (
            (self.attack_success is None and self.utility is None)
            or not self.artifact
            or not self.artifact_sha256
            or not self.evidence
        ):
            raise ValueError(
                "evaluated observations require a metric and upstream artifact evidence"
            )
        return self


class LoadedBenchmarkDataset(BaseModel):
    name: str
    version: str
    source_path: Path
    sha256: str
    cases: list[BenchmarkCase]
    snapshot: dict[str, Any]


class BenchmarkImportPolicy(BaseModel):
    provenance: Literal["upstream_artifacts"] = "upstream_artifacts"
    external_calls: Literal[0] = 0
    evaluator: Literal["upstream-benchmark-v1"] = "upstream-benchmark-v1"
