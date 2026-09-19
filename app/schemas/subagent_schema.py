"""主 Agent 的显式委派契约；共享 Policy 是整组预算的上限。"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.graybox_schema import GrayBoxRunRequest, PlannerConfig


class SubagentAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
    case_ids: list[str] = Field(min_length=1)
    planner: PlannerConfig | None = None

    @model_validator(mode="after")
    def unique_cases(self) -> Self:
        if len(set(self.case_ids)) != len(self.case_ids):
            raise ValueError("case_ids must be unique within a subagent")
        return self


class SubagentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: GrayBoxRunRequest
    subagents: list[SubagentAssignment] = Field(min_length=2, max_length=16)
    concurrency: int = Field(default=1, ge=1, le=16)
    parallel_target_safe: bool = False

    @model_validator(mode="after")
    def validate_delegation(self) -> Self:
        count = len(self.subagents)
        if len({agent.agent_id for agent in self.subagents}) != count:
            raise ValueError("agent_id must be unique")
        if self.concurrency > 1 and not self.parallel_target_safe:
            raise ValueError("parallel execution requires parallel_target_safe=true")
        for field in ("max_steps", "max_target_calls"):
            if getattr(self.run.policy, field) < count:
                raise ValueError(f"{field} must allocate at least one per subagent")
        return self
