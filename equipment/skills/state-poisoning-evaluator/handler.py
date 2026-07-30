from app.schemas.equipment_schema import EvidenceDraft, SkillPreparation, SkillResult
from app.schemas.stateful_schema import (
    MemoryEvidence,
    RecoveryEvidence,
    RetrievalTrace,
    StatefulCase,
)
from app.services.stateful_evaluator_service import StatefulEvaluatorService


class StatePoisoningEvaluator:
    async def prepare(self, context: dict) -> SkillPreparation:
        return SkillPreparation()

    async def execute(self, payload: dict, context: dict) -> SkillResult:
        case = StatefulCase.model_validate(payload["case"])
        observed_memory = (
            [MemoryEvidence.model_validate(item) for item in payload["observed_memory"]]
            if payload.get("observed_memory") is not None
            else None
        )
        retrieval = (
            RetrievalTrace.model_validate(payload["retrieval"])
            if payload.get("retrieval") is not None
            else None
        )
        recovery = (
            RecoveryEvidence.model_validate(payload["recovery"])
            if payload.get("recovery") is not None
            else None
        )
        evaluation = StatefulEvaluatorService().evaluate(
            case=case,
            observed_memory=observed_memory,
            retrieval=retrieval,
            recovery=recovery,
        )
        return SkillResult(
            status="success",
            output={"evaluation": evaluation.model_dump(mode="json")},
            evidence=[
                EvidenceDraft(
                    evidence_type="retrieval_evaluation",
                    summary={
                        "case_id": case.id,
                        "memory_evidence_count": len(observed_memory or []),
                        "retrieval_document_count": len(retrieval.documents if retrieval else []),
                        "outcome": evaluation.outcome,
                    },
                )
            ],
        )
