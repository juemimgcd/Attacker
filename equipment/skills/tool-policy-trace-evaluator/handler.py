from app.schemas.equipment_schema import EvidenceDraft, SkillPreparation, SkillResult
from app.schemas.graybox_schema import GrayBoxCase, TraceAdapterResult
from app.schemas.judge_schema import TargetResponse
from app.services.graybox_evaluator_service import GrayBoxEvaluatorService


class ToolPolicyTraceEvaluator:
    async def prepare(self, context: dict) -> SkillPreparation:
        return SkillPreparation()

    async def execute(self, payload: dict, context: dict) -> SkillResult:
        case = GrayBoxCase.model_validate(payload["case"])
        response = TargetResponse.model_validate(payload["response"])
        trace_result = TraceAdapterResult.model_validate(payload["trace_result"])
        evaluation = GrayBoxEvaluatorService().evaluate(
            case=case,
            response=response,
            trace_result=trace_result,
        )
        return SkillResult(
            status="success",
            output={"evaluation": evaluation.model_dump(mode="json")},
            evidence=[
                EvidenceDraft(
                    evidence_type="tool_policy_trace",
                    summary={
                        "case_id": case.id,
                        "event_count": len(trace_result.trace.tool_events),
                        "outcome": evaluation.outcome.value,
                    },
                )
            ],
        )
