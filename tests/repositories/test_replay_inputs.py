from sqlalchemy import select

from app.models import EventRecord
from app.repositories.adaptive_repository import AdaptiveRepository
from app.repositories.run_repository import RunRepository
from app.repositories.stateful_repository import StatefulRepository
from app.schemas.graybox_schema import AttackPolicy
from app.schemas.run_schema import RunBudget
from app.schemas.stateful_schema import StatefulProfile
from app.services.sample_loader import (
    BlackBoxDatasetLoader,
    GrayBoxDatasetLoader,
    StatefulDatasetLoader,
)


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
    stateful_dataset = await StatefulDatasetLoader().load(
        "samples/stateful/phase3.yaml",
        ["st_memory_poisoning_attack"],
    )
    stateful_run_id = await replay_repository.create_run(
        dataset=stateful_dataset,
        profile=StatefulProfile.hardened,
        target_name="sandbox",
        mode="deterministic_stateful",
    )

    loaded_blackbox, loaded_budget = await replay_repository.load_blackbox_replay_inputs(
        blackbox_run_id
    )
    loaded_graybox, loaded_policy = await replay_repository.load_graybox_replay_inputs(
        graybox_run_id
    )
    loaded_stateful = await replay_repository.load_dataset(stateful_run_id)

    assert loaded_blackbox.sha256 == blackbox_dataset.sha256
    assert [case.id for case in loaded_blackbox.cases] == ["bb_direct_override_attack"]
    assert loaded_blackbox.cases[0].id == "bb_direct_override_attack"
    assert loaded_budget == blackbox_budget
    assert loaded_graybox.sha256 == graybox_dataset.sha256
    assert [case.id for case in loaded_graybox.cases] == ["gb_unauthorized_tool_attack"]
    assert loaded_graybox.cases[0].id == "gb_unauthorized_tool_attack"
    assert loaded_policy == effective_policy
    assert loaded_stateful.sha256 == stateful_dataset.sha256
    assert [case.id for case in loaded_stateful.cases] == ["st_memory_poisoning_attack"]


async def test_legacy_replay_without_case_order_uses_the_full_snapshot(session_factory) -> None:
    dataset = await BlackBoxDatasetLoader().load(
        "samples/blackbox/phase1.yaml",
        ["bb_direct_override_attack"],
    )
    run_id = await RunRepository(session_factory).create_run(
        target_snapshot={"name": "sandbox", "endpoint": "http://localhost"},
        dataset=dataset,
        budget=RunBudget(),
    )

    async with session_factory.begin() as session:
        event = await session.scalar(
            select(EventRecord).where(
                EventRecord.run_id == run_id,
                EventRecord.event_type == "run_started",
            )
        )
        assert event is not None
        legacy_evidence = dict(event.evidence_json)
        legacy_evidence.pop("case_order")
        event.evidence_json = legacy_evidence

    loaded, _ = await StatefulRepository(session_factory).load_blackbox_replay_inputs(run_id)

    assert len(dataset.snapshot["cases"]) > len(dataset.cases)
    assert len(loaded.cases) == len(dataset.snapshot["cases"])
