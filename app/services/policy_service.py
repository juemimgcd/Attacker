from app.schemas.graybox_schema import (
    AttackPolicy,
    GrayBoxCase,
    PolicyGateResult,
    ToolPolicyDecision,
)


class PolicyService:
    def evaluate(
        self,
        *,
        policy: AttackPolicy,
        target_id: str,
        case: GrayBoxCase,
        remaining_steps: int,
        target_call_count: int,
        elapsed_seconds: float,
        approval_status: str | None,
        approval_id: str | None = None,
    ) -> PolicyGateResult:
        if target_id not in policy.allowed_target_ids:
            return PolicyGateResult(
                decision=ToolPolicyDecision.deny,
                reason="target is outside the run allowlist",
            )
        if case.id not in policy.allowed_case_ids:
            return PolicyGateResult(
                decision=ToolPolicyDecision.deny,
                reason="case is outside the run allowlist",
            )
        if remaining_steps <= 0:
            return PolicyGateResult(
                decision=ToolPolicyDecision.deny,
                reason="graph step budget exhausted",
            )
        if target_call_count >= policy.max_target_calls:
            return PolicyGateResult(
                decision=ToolPolicyDecision.deny,
                reason="target call budget exhausted",
            )
        if elapsed_seconds >= policy.max_duration_seconds:
            return PolicyGateResult(
                decision=ToolPolicyDecision.deny,
                reason="run duration budget exhausted",
            )

        requires_approval = (
            case.requires_approval or case.severity in policy.approval_required_severities
        )
        if requires_approval and approval_status == "rejected":
            return PolicyGateResult(
                decision=ToolPolicyDecision.deny,
                reason="human approval was rejected",
                approval_id=approval_id,
            )
        if requires_approval and approval_status != "approved":
            return PolicyGateResult(
                decision=ToolPolicyDecision.approval_required,
                reason="case requires current-run human approval",
                approval_id=approval_id,
            )
        return PolicyGateResult(
            decision=ToolPolicyDecision.allow,
            reason="target, case, approval, and budgets are allowed",
            approval_id=approval_id,
        )
