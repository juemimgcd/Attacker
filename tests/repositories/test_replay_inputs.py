from app.repositories.adaptive_repository import AdaptiveRepository
from app.repositories.run_repository import RunRepository
from app.repositories.stateful_repository import StatefulRepository
from app.schemas.graybox_schema import AttackPolicy
from app.schemas.run_schema import RunBudget
from app.services.sample_loader import BlackBoxDatasetLoader, GrayBoxDatasetLoader


async def test_replay_inputs_are_reconstructed_from_persisted_snapshots(
    session_factory,
) -> None:
    blackbox_dataset = await BlackBoxDatasetLoader().load(
        "samples/blackbox/phase1.yaml",
        ["bb_direct_override_attack"],
    )
    blackbox_budget = RunBudget(max_cases=1, max_target_calls=1)
    blackbox_run_id = await RunRepository(session_factory).create_run(
        target_snapshot={"name": "sandbox", "endpoint": "http://localhost"},
        dataset=blackbox_dataset,
        budget=blackbox_budget,
    )
    graybox_dataset = await GrayBoxDatasetLoader().load(
        "samples/graybox/phase2.yaml",
        ["gb_unauthorized_tool_attack"],
    )
    graybox_run_id, _, _, effective_policy = await AdaptiveRepository(session_factory).create_run(
        target_snapshot={"name": "sandbox", "endpoint": "http://localhost"},
        dataset=graybox_dataset,
        policy=AttackPolicy(),
        mode="deterministic_graybox",
        baseline_run_id=None,
    )
    replay_repository = StatefulRepository(session_factory)

    loaded_blackbox, loaded_budget = await replay_repository.load_blackbox_replay_inputs(
        blackbox_run_id
    )
    loaded_graybox, loaded_policy = await replay_repository.load_graybox_replay_inputs(
        graybox_run_id
    )

    assert loaded_blackbox.sha256 == blackbox_dataset.sha256
    assert loaded_blackbox.cases[0].id == "bb_direct_override_attack"
    assert loaded_budget == blackbox_budget
    assert loaded_graybox.sha256 == graybox_dataset.sha256
    assert loaded_graybox.cases[0].id == "gb_unauthorized_tool_attack"
    assert loaded_policy == effective_policy
