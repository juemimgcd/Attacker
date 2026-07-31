"""内置黑盒 Prompt Injection Evaluator Skill。"""

from app.schemas.attack_sample_schema import BlackBoxCase
from app.schemas.equipment_schema import EvidenceDraft, SkillPreparation, SkillResult
from app.schemas.judge_schema import TargetResponse
from app.services.evaluator_service import EvaluatorService


class PromptInjectionEvaluator:
    """复用 Core 黑盒规则并返回 Evaluation 草稿，不直接创建 Finding。"""

    async def prepare(self, context: dict) -> SkillPreparation:
        return SkillPreparation()

    async def execute(self, payload: dict, context: dict) -> SkillResult:
        case = BlackBoxCase.model_validate(payload["case"])
        responses = [TargetResponse.model_validate(item) for item in payload["responses"]]
        evaluation = EvaluatorService().evaluate(
            case,
            responses,
            max_response_bytes=int(payload["max_response_bytes"]),
        )
        return SkillResult(
            status="success",
            output={"evaluation": evaluation.model_dump(mode="json")},
            evidence=[
                EvidenceDraft(
                    evidence_type="evaluator_result",
                    summary={
                        "case_id": case.id,
                        "response_count": len(responses),
                        "outcome": evaluation.outcome.value,
                    },
                )
            ],
        )
