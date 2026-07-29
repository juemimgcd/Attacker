import hashlib
import json
from collections.abc import Iterable
from decimal import Decimal
from typing import ClassVar

from app.schemas.adaptive_agent_schema import (
    Candidate,
    CandidateAction,
    CandidateFilterReason,
    CandidateRejection,
    CandidateSnapshot,
    HypothesisFact,
    HypothesisStatus,
    InformationGain,
)
from app.schemas.attack_sample_schema import CaseKind, RiskLevel
from app.schemas.attack_state_schema import CoverageStatus, RunBudgetSnapshot
from app.schemas.graybox_schema import AttackPolicy, GrayBoxCase


class CandidateBuilder:
    _gain_priority: ClassVar[dict[InformationGain, int]] = {
        InformationGain.high: 3,
        InformationGain.medium: 2,
        InformationGain.low: 1,
    }
    _risk_priority: ClassVar[dict[RiskLevel, int]] = {
        RiskLevel.low: 0,
        RiskLevel.medium: 1,
        RiskLevel.high: 2,
        RiskLevel.critical: 3,
    }

    @staticmethod
    def actions_from_cases(
        *,
        cases: Iterable[GrayBoxCase],
        target_id: str,
        test_principal_ref: str,
    ) -> list[CandidateAction]:
        return [
            CandidateAction(
                action_id=case.id,
                target_id=target_id,
                test_principal_ref=test_principal_ref,
                provider_instance_ref=case.provider_instance_ref,
                capability_contract=case.capability_contract,
                enabled=case.enabled,
                compatible=case.compatible,
                risk_level=case.severity,
                requires_approval=case.requires_approval,
                repeatable=case.repeatable,
                max_repeats=case.max_action_repeats,
                prerequisite_action_ids=tuple(case.prerequisite_case_ids),
                hypothesis_template_id=(case.hypothesis_template_id or f"{case.category}.v1"),
                coverage_tags=tuple(case.coverage_tags or [case.category]),
                expected_information_gain=case.expected_information_gain,
                is_control=case.kind == CaseKind.control,
                similarity_key=case.category,
            )
            for case in cases
        ]

    def build(
        self,
        *,
        run_id: str,
        actions: Iterable[CandidateAction],
        policy: AttackPolicy,
        budget: RunBudgetSnapshot,
        completed_action_ids: set[str],
        denied_action_ids: set[str],
        action_repeat_counts: dict[str, int],
        coverage: dict[str, CoverageStatus],
        hypotheses: dict[str, HypothesisFact],
        evidence_gaps: set[str],
        valid_test_principal_refs: set[str],
        recent_similarity_keys: tuple[str, ...] = (),
    ) -> CandidateSnapshot:
        candidates: list[Candidate] = []
        rejected: list[CandidateRejection] = []
        for action in sorted(actions, key=lambda item: item.action_id):
            rejection = self._filter(
                action=action,
                policy=policy,
                budget=budget,
                completed_action_ids=completed_action_ids,
                denied_action_ids=denied_action_ids,
                action_repeat_counts=action_repeat_counts,
                valid_test_principal_refs=valid_test_principal_refs,
            )
            if rejection is not None:
                rejected.append(rejection)
                continue

            uncovered_count = sum(
                coverage.get(tag, CoverageStatus.not_started) != CoverageStatus.covered
                for tag in action.coverage_tags
            )
            hypothesis = hypotheses.get(action.action_id)
            hypothesis_value = int(
                hypothesis is None
                or hypothesis.status in {HypothesisStatus.pending, HypothesisStatus.inconclusive}
            )
            evidence_gap_count = sum(
                tag in evidence_gaps or action.action_id in evidence_gaps
                for tag in action.coverage_tags
            )
            control_value = int(action.is_control and action.action_id not in completed_action_ids)
            if uncovered_count + hypothesis_value + evidence_gap_count + control_value == 0:
                rejected.append(
                    CandidateRejection(
                        action_id=action.action_id,
                        reason=CandidateFilterReason.no_expected_gain,
                        detail="action has no uncovered tag, hypothesis value, evidence gap, or control value",
                    )
                )
                continue

            similarity_penalty = int(action.similarity_key in recent_similarity_keys)
            priority = (
                -uncovered_count,
                -hypothesis_value,
                -self._gain_priority[action.expected_information_gain],
                -evidence_gap_count,
                -control_value,
                action.estimated_target_calls,
                action.estimated_provider_calls,
                int(action.estimated_cost * Decimal(1_000_000)),
                int(
                    action.requires_approval
                    or action.risk_level in policy.approval_required_severities
                ),
                self._risk_priority[action.risk_level],
                similarity_penalty,
            )
            hypothesis_refs = (hypothesis.hypothesis_ref,) if hypothesis else ()
            candidates.append(
                Candidate(
                    candidate_id=self._candidate_id(action),
                    action_id=action.action_id,
                    target_id=action.target_id,
                    test_principal_ref=action.test_principal_ref,
                    provider_instance_ref=action.provider_instance_ref,
                    capability_contract=action.capability_contract,
                    risk_level=action.risk_level,
                    requires_approval=(
                        action.requires_approval
                        or action.risk_level in policy.approval_required_severities
                    ),
                    hypothesis_refs=hypothesis_refs,
                    coverage_tags=action.coverage_tags,
                    expected_information_gain=action.expected_information_gain,
                    estimated_target_calls=action.estimated_target_calls,
                    estimated_provider_calls=action.estimated_provider_calls,
                    estimated_cost=action.estimated_cost,
                    is_control=action.is_control,
                    priority=priority,
                )
            )

        ordered = tuple(
            sorted(
                candidates,
                key=lambda item: (*item.priority, item.action_id, item.candidate_id),
            )
        )
        rejected_tuple = tuple(
            sorted(rejected, key=lambda item: (item.action_id, item.reason.value))
        )
        snapshot_payload = {
            "run_id": run_id,
            "candidates": [item.model_dump(mode="json") for item in ordered],
            "rejected": [item.model_dump(mode="json") for item in rejected_tuple],
        }
        snapshot_id = hashlib.sha256(
            json.dumps(
                snapshot_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return CandidateSnapshot(
            snapshot_id=snapshot_id,
            run_id=run_id,
            candidates=ordered,
            rejected=rejected_tuple,
        )

    @staticmethod
    def _candidate_id(action: CandidateAction) -> str:
        binding = {
            "action_id": action.action_id,
            "target_id": action.target_id,
            "test_principal_ref": action.test_principal_ref,
            "provider_instance_ref": action.provider_instance_ref,
            "capability_contract": action.capability_contract,
            "hypothesis_template_id": action.hypothesis_template_id,
        }
        return hashlib.sha256(
            json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _filter(
        *,
        action: CandidateAction,
        policy: AttackPolicy,
        budget: RunBudgetSnapshot,
        completed_action_ids: set[str],
        denied_action_ids: set[str],
        action_repeat_counts: dict[str, int],
        valid_test_principal_refs: set[str],
    ) -> CandidateRejection | None:
        def reject(reason: CandidateFilterReason, detail: str) -> CandidateRejection:
            return CandidateRejection(action_id=action.action_id, reason=reason, detail=detail)

        if not action.enabled:
            return reject(CandidateFilterReason.disabled, "action is disabled")
        if not action.compatible:
            return reject(CandidateFilterReason.incompatible, "action is incompatible")
        if action.target_id not in policy.allowed_target_ids:
            return reject(
                CandidateFilterReason.target_not_allowed,
                "target is outside the policy allowlist",
            )
        if action.action_id not in policy.allowed_case_ids:
            return reject(
                CandidateFilterReason.case_not_allowed,
                "case is outside the policy allowlist",
            )
        if action.capability_contract not in policy.allowed_capability_contracts:
            return reject(
                CandidateFilterReason.capability_not_allowed,
                "capability contract is outside the policy allowlist",
            )
        if action.provider_instance_ref not in policy.allowed_provider_instance_refs:
            return reject(
                CandidateFilterReason.invalid_binding,
                "provider instance binding is not valid for this run",
            )
        if action.test_principal_ref not in valid_test_principal_refs:
            return reject(
                CandidateFilterReason.invalid_binding,
                "test principal binding is not valid for this run",
            )
        if (
            budget.steps_used >= budget.max_steps
            or budget.elapsed_seconds >= budget.max_duration_seconds
            or budget.target_calls_used + action.estimated_target_calls > budget.max_target_calls
            or budget.provider_calls_used + action.estimated_provider_calls
            > budget.max_provider_calls
            or (
                budget.max_cost is not None
                and budget.cost_used + action.estimated_cost > budget.max_cost
            )
        ):
            return reject(
                CandidateFilterReason.budget_insufficient,
                "remaining budget cannot cover the action upper bound",
            )
        repeats = action_repeat_counts.get(action.action_id, 0)
        repeat_limit = min(action.max_repeats, policy.max_action_repeats)
        if action.action_id in denied_action_ids:
            return reject(
                CandidateFilterReason.policy_denied,
                "action was denied by policy or approval",
            )
        if action.action_id in completed_action_ids and not action.repeatable:
            return reject(
                CandidateFilterReason.already_completed,
                "completed or denied action is not repeatable",
            )
        if repeats >= repeat_limit:
            return reject(
                CandidateFilterReason.repeat_limit,
                "action repeat limit reached",
            )
        missing = sorted(set(action.prerequisite_action_ids) - completed_action_ids)
        if missing:
            return reject(
                CandidateFilterReason.prerequisite_missing,
                f"missing prerequisite actions: {', '.join(missing)}",
            )
        return None
