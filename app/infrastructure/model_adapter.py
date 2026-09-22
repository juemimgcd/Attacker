"""把受治理 Prompt 与模型 Provider 适配为 Planner 和 Model Judge 窄接口。"""

import hashlib
from typing import Any, Protocol

from app.agent.context import prepare_context
from app.agent.tools import resolve_tool_call, tool_schemas, validate_decision
from app.infrastructure.model_provider import (
    ModelProvider,
    ModelProviderError,
    OpenAICompatibleModelProvider,
)
from app.schemas.adaptive_agent_schema import PlannerCallSnapshot, PlannerReasonCode
from app.schemas.agent_schema import HistorySummary
from app.schemas.graybox_schema import (
    PlannerConfig,
    PlannerContext,
    PlannerDecision,
    PlannerResult,
    PlannerUsage,
)
from app.schemas.judge_schema import EvaluationStage, EvaluatorResult
from app.schemas.model_provider_schema import (
    ModelInferenceRequest,
    ModelProviderUsage,
)
from app.schemas.prompt_schema import (
    ObservationSummary,
    PromptBuildRequest,
    PromptSnapshot,
    PromptTask,
)
from app.services.prompt_governance import (
    PromptGovernanceService,
    prompt_governance_service,
)


def _input_fact_refs(context: PlannerContext) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            [
                context.candidate_snapshot_id,
                *context.evidence_refs,
                *context.finding_refs,
                *context.hypothesis_refs,
                *context.coverage_refs,
                *context.information_gain_refs,
                *(item.observation_ref for item in context.observations),
            ]
        )
    )


class PlannerAdapterError(RuntimeError):
    def __init__(
        self,
        *,
        error_category: str,
        provider_id: str,
        model_id: str,
        usage: PlannerUsage,
        call_snapshot: PlannerCallSnapshot | None,
    ) -> None:
        super().__init__(f"planner adapter failed: {error_category}")
        self.error_category = error_category
        self.provider_id = provider_id
        self.model_id = model_id
        self.usage = usage
        self.call_snapshot = call_snapshot


class PlannerModelAdapter(Protocol):
    provider_id: str
    model_id: str
    requires_provider: bool

    async def plan(
        self,
        context: PlannerContext,
        *,
        operation_id: str = "planner",
        max_physical_attempts: int = 1,
    ) -> PlannerResult: ...


class DeterministicPlannerAdapter:
    """不调用模型，按稳定顺序选择候选，用于流程验证和回归基线。"""

    provider_id = "deterministic"
    model_id = "deterministic"
    requires_provider = False

    async def plan(
        self,
        context: PlannerContext,
        *,
        operation_id: str = "planner",
        max_physical_attempts: int = 1,
    ) -> PlannerResult:
        del operation_id, max_physical_attempts
        next_candidate = next(iter(context.candidates), None)
        if next_candidate is None or context.remaining_steps <= 0:
            decision = PlannerDecision(
                action="finish",
                candidate_snapshot_id=context.candidate_snapshot_id,
                reason_code=(
                    PlannerReasonCode.no_candidates
                    if next_candidate is None
                    else PlannerReasonCode.budget_exhausted
                ),
                evidence_refs=context.evidence_refs,
                hypothesis_refs=context.hypothesis_refs,
            )
        else:
            decision = PlannerDecision(
                action="execute",
                candidate_snapshot_id=context.candidate_snapshot_id,
                candidate_id=next_candidate.candidate_id,
                reason_code=PlannerReasonCode.deterministic_order,
                evidence_refs=context.evidence_refs,
                hypothesis_refs=list(next_candidate.hypothesis_refs),
                expected_information_gain=next_candidate.expected_information_gain,
            )
        return PlannerResult(
            decision=decision,
            backend="deterministic",
            call_snapshot=self._call_snapshot(context),
        )

    @staticmethod
    def _call_snapshot(context: PlannerContext) -> PlannerCallSnapshot:
        template = "deterministic-candidate-order-v1"
        input_refs = tuple(
            dict.fromkeys(
                [
                    context.candidate_snapshot_id,
                    *context.evidence_refs,
                    *context.finding_refs,
                    *context.hypothesis_refs,
                    *(item.observation_ref for item in context.observations),
                ]
            )
        )
        return PlannerCallSnapshot(
            provider_id="deterministic",
            prompt_template_version=template,
            prompt_checksum=hashlib.sha256(template.encode()).hexdigest(),
            prompt_profile_id="deterministic",
            input_checksum=hashlib.sha256("|".join(input_refs).encode()).hexdigest(),
            model_id="deterministic",
            model_parameters={},
            schema_version="planner-decision-v2",
            input_fact_refs=_input_fact_refs(context),
        )


class OpenAICompatiblePlannerAdapter:
    """调用 OpenAI-compatible Provider，并严格校验结构化 PlannerDecision。"""

    requires_provider = True

    def __init__(
        self,
        config: PlannerConfig,
        *,
        provider: ModelProvider | None = None,
        prompt_governance: PromptGovernanceService = prompt_governance_service,
    ) -> None:
        if config.endpoint is None:
            raise ValueError("planner endpoint is required")
        self.config = config
        self.provider_id = config.provider_id
        self.model_id = config.model
        self.provider = provider or OpenAICompatibleModelProvider(
            endpoint=config.endpoint,
            api_key=config.api_key,
        )
        self.prompt_governance = prompt_governance

    async def plan(
        self,
        context: PlannerContext,
        *,
        operation_id: str = "planner",
        max_physical_attempts: int = 1,
    ) -> PlannerResult:
        schemas = tool_schemas() if self.config.response_mode == "tools" else ()
        view = prepare_context(context, self.config, self.prompt_governance, schemas)
        prompt = view.prompt
        call_snapshot = self._call_snapshot(prompt.snapshot, schemas, view.context.history_summary)
        allowed_attempts = min(
            self.config.max_physical_attempts,
            max_physical_attempts,
        )
        if allowed_attempts <= 0:
            raise PlannerAdapterError(
                error_category="provider_budget_exhausted",
                provider_id=self.provider_id,
                model_id=self.model_id,
                usage=PlannerUsage(),
                call_snapshot=call_snapshot,
            )
        try:
            provider_result = await self.provider.infer(
                ModelInferenceRequest(
                    operation_id=operation_id,
                    task=prompt.snapshot.task,
                    provider_id=self.provider_id,
                    model_id=self.model_id,
                    messages=tuple(prompt.messages),
                    response_schema_version="planner-decision-v2",
                    temperature=self.config.temperature,
                    timeout_seconds=self.config.timeout_seconds,
                    max_physical_attempts=allowed_attempts,
                    tools=schemas,
                    max_output_tokens=self.config.context_budget.output_tokens,
                )
            )
        except ModelProviderError as exc:
            raise PlannerAdapterError(
                error_category=exc.error_category,
                provider_id=self.provider_id,
                model_id=self.model_id,
                usage=self._planner_usage(exc.usage),
                call_snapshot=call_snapshot,
            ) from exc

        usage = self._planner_usage(provider_result.usage)
        try:
            if schemas:
                if len(provider_result.tool_calls) != 1:
                    raise ValueError("planner must request exactly one tool")
                decision = resolve_tool_call(provider_result.tool_calls[0])
            else:
                if provider_result.tool_calls:
                    raise ValueError("JSON planner unexpectedly requested tools")
                decision = PlannerDecision.model_validate(provider_result.structured_output)
            # The adapter checks the compacted view; the loop also checks every backend.
            rejection = validate_decision(view.context, decision)
            if rejection is not None:
                raise ValueError(rejection)
        except (ValueError, TypeError) as exc:
            raise PlannerAdapterError(
                error_category="invalid_structured_response",
                provider_id=self.provider_id,
                model_id=self.model_id,
                usage=usage,
                call_snapshot=call_snapshot,
            ) from exc
        return PlannerResult(
            decision=decision,
            usage=usage,
            backend="openai_compatible",
            call_snapshot=call_snapshot,
            tool_call=provider_result.tool_calls[0] if schemas else None,
        )

    def _call_snapshot(
        self,
        snapshot: PromptSnapshot,
        schemas: tuple[dict[str, Any], ...] = (),
        summary: HistorySummary | None = None,
    ) -> PlannerCallSnapshot:
        return PlannerCallSnapshot(
            provider_id=self.provider_id,
            prompt_template_version=snapshot.template_version,
            prompt_checksum=snapshot.template_checksum,
            prompt_profile_id=snapshot.profile_id,
            input_checksum=snapshot.input_checksum,
            model_id=self.model_id,
            model_parameters={
                "temperature": self.config.temperature,
                "max_tokens": self.config.context_budget.output_tokens,
                "response_mode": self.config.response_mode,
            },
            schema_version="planner-decision-v2",
            input_fact_refs=tuple(snapshot.fact_refs),
            context_snapshot=snapshot,
            tool_schemas=schemas,
            history_summary=summary,
        )

    @staticmethod
    def _planner_usage(usage: ModelProviderUsage) -> PlannerUsage:
        return PlannerUsage(
            physical_attempts=usage.physical_attempts,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=usage.latency_ms,
            estimated_cost=float(usage.estimated_cost),
            attempt_errors=[
                attempt.error_category
                for attempt in usage.attempts
                if attempt.error_category is not None
            ],
        )


class ProviderBackedModelJudgeAdapter:
    """可选模型 Judge；只消费受治理输入，不能创建授权或直接写 Finding。"""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        provider_id: str,
        model_id: str,
        timeout_seconds: float = 30,
        temperature: float = 0,
        prompt_governance: PromptGovernanceService = prompt_governance_service,
    ) -> None:
        self.provider = provider
        self.provider_id = provider_id
        self.model_id = model_id
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.prompt_governance = prompt_governance

    async def judge(
        self,
        *,
        operation_id: str,
        observations: list[ObservationSummary],
        evidence_refs: list[str],
        max_physical_attempts: int,
    ) -> tuple[EvaluatorResult, ModelProviderUsage, PromptSnapshot]:
        prompt = self.prompt_governance.build(
            PromptBuildRequest(
                task=PromptTask.model_judge,
                profile_id="core.model_judge.v1",
                caller_id="attacker_core",
                schema_version="model-judge-v1",
                trusted_payload={"evaluation_task": "semantic_ambiguity"},
                observations=observations,
                fact_refs=evidence_refs,
                model_id=self.model_id,
                provider_id=self.provider_id,
                model_parameters={"temperature": self.temperature},
            )
        )
        result = await self.provider.infer(
            ModelInferenceRequest(
                operation_id=operation_id,
                task=PromptTask.model_judge,
                provider_id=self.provider_id,
                model_id=self.model_id,
                messages=tuple(prompt.messages),
                response_schema_version="model-judge-v1",
                temperature=self.temperature,
                timeout_seconds=self.timeout_seconds,
                max_physical_attempts=max_physical_attempts,
            )
        )
        evaluation = EvaluatorResult.model_validate(result.structured_output)
        if evaluation.stage != EvaluationStage.model_judge:
            raise ValueError("model judge provider returned an unsupported evaluation stage")
        if not set(evaluation.evidence_refs).issubset(evidence_refs):
            raise ValueError("model judge referenced evidence outside the governed request")
        return evaluation, result.usage, prompt.snapshot


def create_planner_adapter(config: PlannerConfig) -> PlannerModelAdapter:
    if config.backend == "openai_compatible":
        return OpenAICompatiblePlannerAdapter(config)
    return DeterministicPlannerAdapter()
