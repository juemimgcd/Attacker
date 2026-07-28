import json
from typing import Protocol

import httpx

from app.schemas.graybox_schema import (
    PlannerConfig,
    PlannerContext,
    PlannerDecision,
    PlannerResult,
    PlannerUsage,
)


class PlannerModelAdapter(Protocol):
    async def plan(self, context: PlannerContext) -> PlannerResult: ...


class DeterministicPlannerAdapter:
    async def plan(self, context: PlannerContext) -> PlannerResult:
        completed = set(context.completed_case_ids)
        next_case = next(
            (case for case in context.allowed_cases if case.id not in completed),
            None,
        )
        if next_case is None or context.remaining_steps <= 0:
            decision = PlannerDecision(
                action="finish_run",
                reason="all allowed cases are completed or the step budget is exhausted",
            )
        else:
            decision = PlannerDecision(
                action="execute_case",
                case_id=next_case.id,
                reason="select the next allowed case in dataset order",
            )
        return PlannerResult(decision=decision, backend="deterministic")


class OpenAICompatiblePlannerAdapter:
    def __init__(self, config: PlannerConfig) -> None:
        if config.endpoint is None:
            raise ValueError("planner endpoint is required")
        self.config = config

    async def plan(self, context: PlannerContext) -> PlannerResult:
        api_key = self.config.api_key.get_secret_value() if self.config.api_key else None
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": self.config.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Choose one allowed, incomplete case or finish. Return JSON with "
                        "action, case_id, and reason. Never invent case IDs."
                    ),
                },
                {
                    "role": "user",
                    "content": context.model_dump_json(),
                },
            ],
        }
        async with httpx.AsyncClient(
            timeout=self.config.timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                str(self.config.endpoint),
                headers=headers,
                json=payload,
            )
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        decision = PlannerDecision.model_validate(json.loads(content))
        usage = body.get("usage", {})
        return PlannerResult(
            decision=decision,
            usage=PlannerUsage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
            ),
            backend="openai_compatible",
        )


def create_planner_adapter(config: PlannerConfig) -> PlannerModelAdapter:
    if config.backend == "openai_compatible":
        return OpenAICompatiblePlannerAdapter(config)
    return DeterministicPlannerAdapter()
