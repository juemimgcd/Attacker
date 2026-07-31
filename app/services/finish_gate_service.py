"""判断自适应 Planner 是否具备提前结束 Run 的证据条件。"""

from app.schemas.adaptive_agent_schema import (
    FinishGateReason,
    FinishGateResult,
)
from app.schemas.attack_state_schema import CoverageStatus


class FinishGateService:
    """覆盖、control 与 Evidence gap 未满足时拒绝 finish 决策。"""

    @staticmethod
    def evaluate(
        *,
        required_coverage_tags: set[str],
        required_control_action_ids: set[str],
        coverage: dict[str, CoverageStatus],
        completed_action_ids: set[str],
        evidence_gaps: set[str],
        pending_approval_ids: set[str],
        allow_early_finish: bool,
        has_candidates: bool,
    ) -> FinishGateResult:
        if pending_approval_ids:
            return FinishGateResult(
                allowed=False,
                reason_code=FinishGateReason.pending_approval,
                detail="pending approvals must be resolved before finish",
            )
        if evidence_gaps:
            return FinishGateResult(
                allowed=False,
                reason_code=FinishGateReason.evidence_gap,
                detail="persisted evidence gaps remain unresolved",
            )
        missing_controls = required_control_action_ids - completed_action_ids
        if missing_controls:
            return FinishGateResult(
                allowed=False,
                reason_code=FinishGateReason.control_missing,
                detail=f"required controls are incomplete: {', '.join(sorted(missing_controls))}",
            )
        missing_coverage = {
            tag for tag in required_coverage_tags if coverage.get(tag) != CoverageStatus.covered
        }
        if missing_coverage and not allow_early_finish:
            if not has_candidates:
                return FinishGateResult(
                    allowed=False,
                    reason_code=FinishGateReason.no_candidates,
                    detail="no executable candidates remain for incomplete coverage",
                )
            return FinishGateResult(
                allowed=False,
                reason_code=FinishGateReason.coverage_incomplete,
                detail=f"required coverage is incomplete: {', '.join(sorted(missing_coverage))}",
            )
        return FinishGateResult(
            allowed=True,
            reason_code=FinishGateReason.accepted,
            detail="finish requirements are satisfied",
        )
