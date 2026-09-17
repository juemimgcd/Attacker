"""Target 执行事实；与要求 Policy Evidence 的安全评估 Trace 分开。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SpanKind = Literal["agent", "loop", "model", "tool"]
TraceSource = Literal["runtime_wrapper", "model_proxy"]
TraceCoverage = Literal["instrumented_boundaries_only", "model_protocol_only"]


class ExecutionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    sequence: int = Field(ge=1)
    timestamp: datetime
    kind: SpanKind
    name: str
    phase: Literal["start", "end"]
    status: Literal["running", "ok", "error", "cancelled"]
    duration_ms: float | None = Field(default=None, ge=0)
    data: Any = None
    truncated: bool = False
    source: TraceSource = "runtime_wrapper"


class ExecutionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    events: list[ExecutionEvent] = Field(default_factory=list)
    dropped_events: int = Field(default=0, ge=0)
    capture_errors: int = Field(default=0, ge=0)
    coverage: TraceCoverage = "instrumented_boundaries_only"


class CaptureSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_type: Literal["capture_summary"] = "capture_summary"
    trace_id: str
    events: int = Field(ge=0)
    dropped_events: int = Field(ge=0)
    capture_errors: int = Field(ge=0)
    coverage: TraceCoverage = "instrumented_boundaries_only"
