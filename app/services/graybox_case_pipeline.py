"""灰盒 Case 的共享执行流水线；确定性与自适应编排复用相同业务阶段。"""

from __future__ import annotations

from dataclasses import dataclass

from app.repositories.adaptive_repository import AdaptiveRepository
from app.schemas.adaptive_agent_schema import UntrustedObservation
from app.schemas.graybox_schema import (
    GrayBoxCase,
    GrayBoxEvaluationResult,
    GrayBoxExecutionResult,
    PolicyGateResult,
)
from app.schemas.target_schema import TargetConfig
from app.services.graybox_connector import GrayBoxConnector
from app.services.graybox_evaluator_service import GrayBoxEvaluatorService
from app.services.observation_normalizer import ObservationNormalizer
from app.services.tool_trace_adapter import ToolTraceAdapter


@dataclass(frozen=True, slots=True)
class TargetExecutionStage:
    step_id: str
    target_called: bool


@dataclass(frozen=True, slots=True)
class GrayBoxCasePipelineResult:
    step_id: str
    target_called: bool
    observation: UntrustedObservation
    evaluation: GrayBoxEvaluationResult
    evaluation_event_id: str
    finding_id: str | None


class GrayBoxCasePipeline:
    """执行、脱敏、Trace 解析、Observation、评估和落库的唯一实现。"""

    def __init__(
        self,
        repository: AdaptiveRepository,
        *,
        connector: GrayBoxConnector | None = None,
        trace_adapter: ToolTraceAdapter | None = None,
        evaluator: GrayBoxEvaluatorService | None = None,
        observation_normalizer: ObservationNormalizer | None = None,
    ) -> None:
        self.repository = repository
        self.connector = connector or GrayBoxConnector()
        self.trace_adapter = trace_adapter or ToolTraceAdapter()
        self.evaluator = evaluator or GrayBoxEvaluatorService()
        self.observation_normalizer = observation_normalizer or ObservationNormalizer()

    async def execute_target(
        self,
        *,
        run_id: str,
        case: GrayBoxCase,
        target: TargetConfig,
        operation_id: str,
        sequence: int,
        approval_id: str | None,
        secret_values: set[str],
    ) -> TargetExecutionStage:
        """以 operation_id 保证物理调用可恢复，并在持久化前统一脱敏。"""

        step = await self.repository.ensure_step(
            run_id=run_id,
            case_id=case.id,
            operation_id=operation_id,
            sequence=sequence,
        )
        try:
            await self.repository.load_target_execution(operation_id)
            return TargetExecutionStage(step_id=step.id, target_called=False)
        except LookupError:
            pass

        request_body, response = await self.connector.execute(
            target=target,
            case=case,
            operation_id=operation_id,
            approval_id=approval_id,
        )
        redacted_fields = set(case.redact_fields)
        sanitized_request = self.trace_adapter.sanitize(
            request_body,
            redacted_fields=redacted_fields,
            secret_values=secret_values,
        )
        sanitized_response = response.model_copy(
            update={
                "body": self.trace_adapter.sanitize(
                    response.body,
                    redacted_fields=redacted_fields,
                    secret_values=secret_values,
                ),
                "text": self.trace_adapter.sanitize(
                    response.text,
                    redacted_fields=redacted_fields,
                    secret_values=secret_values,
                ),
            }
        )
        trace = self.trace_adapter.parse(
            sanitized_response,
            redacted_fields=redacted_fields,
            secret_values=secret_values,
        )
        await self.repository.record_target_execution(
            run_id=run_id,
            step_id=step.id,
            operation_id=operation_id,
            request_body=sanitized_request,
            response=sanitized_response,
            trace_result=trace,
        )
        return TargetExecutionStage(step_id=step.id, target_called=True)

    async def normalize_observation(
        self,
        *,
        run_id: str,
        operation_id: str,
        step_id: str,
    ) -> UntrustedObservation:
        _, response, trace = await self.repository.load_target_execution(operation_id)
        normalized = self.observation_normalizer.normalize_target(
            observation_ref=f"{operation_id}:observation",
            response=response,
            trace=trace,
        )
        return await self.repository.record_observation(
            run_id=run_id,
            operation_id=f"{operation_id}:observation",
            source=normalized.source,
            summary=normalized.summary,
            step_id=step_id,
        )

    async def evaluate(
        self,
        *,
        run_id: str,
        case: GrayBoxCase,
        operation_id: str,
        step_id: str,
    ) -> tuple[GrayBoxEvaluationResult, str]:
        _, response, trace = await self.repository.load_target_execution(operation_id)
        evaluation = self.evaluator.evaluate(
            case=case,
            response=response,
            trace_result=trace,
        )
        event_id = await self.repository.record_evaluation(
            run_id=run_id,
            step_id=step_id,
            operation_id=operation_id,
            case_id=case.id,
            evaluation=evaluation,
        )
        return evaluation, event_id

    async def persist(
        self,
        *,
        run_id: str,
        case: GrayBoxCase,
        operation_id: str,
        policy: PolicyGateResult,
        policy_event_ids: list[str],
    ) -> str | None:
        request_body, response, trace = await self.repository.load_target_execution(operation_id)
        evaluation = await self.repository.load_evaluation(operation_id)
        return await self.repository.complete_case(
            run_id=run_id,
            operation_id=operation_id,
            result=GrayBoxExecutionResult(
                case=case,
                request_body=request_body,
                response=response,
                trace=trace.trace,
                evaluation=evaluation,
                policy=policy,
            ),
            policy_event_ids=policy_event_ids,
        )

    async def run_case(
        self,
        *,
        run_id: str,
        case: GrayBoxCase,
        target: TargetConfig,
        operation_id: str,
        sequence: int,
        approval_id: str | None,
        secret_values: set[str],
        policy: PolicyGateResult,
        policy_event_ids: list[str],
    ) -> GrayBoxCasePipelineResult:
        execution = await self.execute_target(
            run_id=run_id,
            case=case,
            target=target,
            operation_id=operation_id,
            sequence=sequence,
            approval_id=approval_id,
            secret_values=secret_values,
        )
        observation = await self.normalize_observation(
            run_id=run_id,
            operation_id=operation_id,
            step_id=execution.step_id,
        )
        evaluation, evaluation_event_id = await self.evaluate(
            run_id=run_id,
            case=case,
            operation_id=operation_id,
            step_id=execution.step_id,
        )
        finding_id = await self.persist(
            run_id=run_id,
            case=case,
            operation_id=operation_id,
            policy=policy,
            policy_event_ids=policy_event_ids,
        )
        return GrayBoxCasePipelineResult(
            step_id=execution.step_id,
            target_called=execution.target_called,
            observation=observation,
            evaluation=evaluation,
            evaluation_event_id=evaluation_event_id,
            finding_id=finding_id,
        )
