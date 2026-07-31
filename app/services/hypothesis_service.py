"""维护安全假设、覆盖事实和每步实际信息增益。"""

from app.schemas.adaptive_agent_schema import (
    CoverageFact,
    HypothesisFact,
    HypothesisStatus,
    InformationGainMetrics,
)
from app.schemas.attack_state_schema import CoverageStatus
from app.schemas.graybox_schema import GrayBoxEvaluationResult


class HypothesisService:
    """使用确定性状态转换更新假设，不让 Planner 直接改写事实。"""

    @staticmethod
    def initial_fact(
        *,
        hypothesis_ref: str,
        template_id: str,
        action_id: str,
    ) -> HypothesisFact:
        return HypothesisFact(
            hypothesis_ref=hypothesis_ref,
            template_id=template_id,
            action_id=action_id,
            status=HypothesisStatus.pending,
        )

    @staticmethod
    def transition(
        *,
        previous: HypothesisFact,
        hypothesis_ref: str,
        evaluation: GrayBoxEvaluationResult,
        evidence_refs: tuple[str, ...],
    ) -> HypothesisFact:
        if not evaluation.evidence_complete or evaluation.outcome.value == "inconclusive":
            status = HypothesisStatus.inconclusive
        elif evaluation.violated:
            status = HypothesisStatus.supported
        else:
            status = HypothesisStatus.rejected
        return HypothesisFact(
            hypothesis_ref=hypothesis_ref,
            template_id=previous.template_id,
            action_id=previous.action_id,
            status=status,
            evidence_refs=evidence_refs,
        )

    @staticmethod
    def coverage_facts(
        *,
        tags: tuple[str, ...],
        evaluation: GrayBoxEvaluationResult,
        evidence_refs: tuple[str, ...],
    ) -> tuple[CoverageFact, ...]:
        status = (
            CoverageStatus.covered if evaluation.evidence_complete else CoverageStatus.inconclusive
        )
        return tuple(
            CoverageFact(tag=tag, status=status, evidence_refs=evidence_refs)
            for tag in sorted(set(tags))
        )

    @staticmethod
    def actual_gain(
        *,
        previous_coverage: dict[str, CoverageStatus],
        previous_hypothesis: HypothesisFact,
        current_coverage: tuple[CoverageFact, ...],
        evaluation: GrayBoxEvaluationResult,
        target_call_cost: int,
        planner_cost: int,
        duration_delta: float,
    ) -> InformationGainMetrics:
        coverage_delta = sum(
            fact.status == CoverageStatus.covered
            and previous_coverage.get(fact.tag) != CoverageStatus.covered
            for fact in current_coverage
        )
        return InformationGainMetrics(
            coverage_delta=coverage_delta,
            evidence_completeness_delta=int(
                evaluation.evidence_complete
                and previous_hypothesis.status
                in {HypothesisStatus.pending, HypothesisStatus.inconclusive}
            ),
            confirmed_finding_delta=int(
                evaluation.violated
                and evaluation.evidence_complete
                and previous_hypothesis.status != HypothesisStatus.supported
            ),
            target_call_cost=target_call_cost,
            planner_cost=planner_cost,
            duration_delta=duration_delta,
        )
