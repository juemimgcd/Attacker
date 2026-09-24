"""Orchestrator/worker 委派契约；共享 Policy 是整组预算的上限。"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.graybox_schema import GrayBoxRunRequest, PlannerConfig


class SubagentAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
    case_ids: list[str] = Field(min_length=1)
    planner: PlannerConfig | None = None
    objective: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def unique_cases(self) -> Self:
        if len(set(self.case_ids)) != len(self.case_ids):
            raise ValueError("case_ids must be unique within a subagent")
        return self


class SubagentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: GrayBoxRunRequest
    subagents: list[SubagentAssignment] | None = Field(default=None, min_length=2, max_length=16)
    orchestrator: PlannerConfig | None = None
    worker_count: int = Field(default=2, ge=2, le=16)
    concurrency: int = Field(default=1, ge=1, le=16)
    parallel_target_safe: bool = False

    @model_validator(mode="after")
    def validate_delegation(self) -> Self:
        if (self.subagents is None) == (self.orchestrator is None):
            raise ValueError("provide either subagents or orchestrator")
        if self.orchestrator is not None and self.orchestrator.backend != "openai_compatible":
            raise ValueError("orchestrator requires an openai_compatible model")
        count = len(self.subagents) if self.subagents is not None else self.worker_count
        if (
            self.subagents is not None
            and len({agent.agent_id for agent in self.subagents}) != count
        ):
            raise ValueError("agent_id must be unique")
        if self.concurrency > 1 and not self.parallel_target_safe:
            raise ValueError("parallel execution requires parallel_target_safe=true")
        for field in ("max_steps", "max_target_calls"):
            if getattr(self.run.policy, field) < count:
                raise ValueError(f"{field} must allocate at least one per subagent")
        if self.orchestrator is not None and self.run.policy.max_provider_calls < count + 2:
            raise ValueError("max_provider_calls must cover planning, workers and summary")
        return self
