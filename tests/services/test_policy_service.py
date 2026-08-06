from app.schemas.graybox_schema import AttackPolicy, ToolPolicyDecision
from app.services.policy_service import PolicyService
from app.services.sample_loader import GrayBoxDatasetLoader


def _policy_for_case(case, *, target_id: str = "target-1") -> AttackPolicy:
    return AttackPolicy(
        allowed_target_ids={target_id},
        allowed_case_ids={case.id},
        allowed_capability_contracts={case.capability_contract},
        allowed_provider_instance_refs={case.provider_instance_ref},
    )


async def test_policy_denies_targets_and_cases_outside_allowlists() -> None:
    case = (await GrayBoxDatasetLoader().load("samples/graybox/phase2.yaml")).cases[0]
    service = PolicyService()
    policy = _policy_for_case(case)

    target_denied = service.evaluate(
        policy=policy,
        target_id="target-2",
        case=case,
        remaining_steps=1,
        target_call_count=0,
        elapsed_seconds=0,
        approval_status=None,
    )
    case_denied = service.evaluate(
        policy=policy.model_copy(update={"allowed_case_ids": set()}),
        target_id="target-1",
        case=case,
        remaining_steps=1,
        target_call_count=0,
        elapsed_seconds=0,
        approval_status=None,
    )

    assert target_denied.decision == ToolPolicyDecision.deny
    assert case_denied.decision == ToolPolicyDecision.deny


async def test_policy_requires_current_run_approval_and_enforces_call_budget() -> None:
    dataset = await GrayBoxDatasetLoader().load("samples/graybox/phase2.yaml")
    case = next(item for item in dataset.cases if item.requires_approval)
    service = PolicyService()
    policy = _policy_for_case(case).model_copy(update={"max_target_calls": 1})

    pending = service.evaluate(
        policy=policy,
        target_id="target-1",
        case=case,
        remaining_steps=1,
        target_call_count=0,
        elapsed_seconds=0,
        approval_status=None,
    )
    approved = service.evaluate(
        policy=policy,
        target_id="target-1",
        case=case,
        remaining_steps=1,
        target_call_count=0,
        elapsed_seconds=0,
        approval_status="approved",
    )
    exhausted = service.evaluate(
        policy=policy,
        target_id="target-1",
        case=case,
        remaining_steps=1,
        target_call_count=1,
        elapsed_seconds=0,
        approval_status="approved",
    )

    assert pending.decision == ToolPolicyDecision.approval_required
    assert approved.decision == ToolPolicyDecision.allow
    assert exhausted.decision == ToolPolicyDecision.deny


async def test_policy_denies_disabled_incompatible_and_unapproved_equipment() -> None:
    case = (await GrayBoxDatasetLoader().load("samples/graybox/phase2.yaml")).cases[0]
    service = PolicyService()
    policy = _policy_for_case(case)

    disabled = service.evaluate(
        policy=policy,
        target_id="target-1",
        case=case.model_copy(update={"enabled": False}),
        remaining_steps=1,
        target_call_count=0,
        elapsed_seconds=0,
        approval_status="approved",
    )
    incompatible = service.evaluate(
        policy=policy,
        target_id="target-1",
        case=case.model_copy(update={"compatible": False}),
        remaining_steps=1,
        target_call_count=0,
        elapsed_seconds=0,
        approval_status="approved",
    )
    capability_denied = service.evaluate(
        policy=policy.model_copy(update={"allowed_capability_contracts": {"other.v1"}}),
        target_id="target-1",
        case=case,
        remaining_steps=1,
        target_call_count=0,
        elapsed_seconds=0,
        approval_status="approved",
    )
    provider_denied = service.evaluate(
        policy=policy.model_copy(update={"allowed_provider_instance_refs": {"other"}}),
        target_id="target-1",
        case=case,
        remaining_steps=1,
        target_call_count=0,
        elapsed_seconds=0,
        approval_status="approved",
    )

    assert disabled.reason == "case is disabled"
    assert incompatible.reason == "case is incompatible with the current runtime"
    assert capability_denied.reason == "capability contract is outside the run allowlist"
    assert provider_denied.reason == "provider instance is outside the run allowlist"
