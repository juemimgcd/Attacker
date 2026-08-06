from asyncio import CancelledError
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select

from app.models import EvaluationRunRecord, StateFixtureRecord
from app.repositories.run_repository import RunRepository
from app.repositories.stateful_repository import StatefulRepository
from app.schemas.stateful_schema import StatefulRunRequest
from app.services.sample_loader import StatefulDatasetLoader
from app.services.stateful_adapters import MemoryAdapter
from app.services.stateful_run_service import StatefulRunService


class InterruptingMemoryAdapter(MemoryAdapter):
    def __init__(self, repository: StatefulRepository, exc: BaseException) -> None:
        super().__init__(repository)
        self.exc = exc

    async def read(self, **_: Any):
        raise self.exc


@pytest.mark.parametrize(
    "dataset_path",
    [Path("pyproject.toml"), Path("samples/stateful/../../pyproject.toml")],
)
async def test_stateful_loader_rejects_paths_outside_the_stateful_samples_root(
    dataset_path: Path,
) -> None:
    with pytest.raises(ValueError, match="inside samples/stateful"):
        await StatefulDatasetLoader().load(dataset_path)


@pytest.mark.parametrize(
    ("exc", "expected_status", "event_type"),
    [
        (RuntimeError("state credential=do-not-persist"), "failed", "run_failed"),
        (CancelledError("cancel credential=do-not-persist"), "cancelled", "run_cancelled"),
    ],
)
async def test_stateful_interruptions_cleanup_fixtures_and_terminalize_run(
    session_factory,
    exc: BaseException,
    expected_status: str,
    event_type: str,
) -> None:
    repository = StatefulRepository(session_factory)
    service = StatefulRunService(repository)
    service.memory = cast(Any, InterruptingMemoryAdapter(repository, exc))

    with pytest.raises(type(exc)):
        await service.run(StatefulRunRequest(case_ids=["st_memory_poisoning_attack"]))

    async with session_factory() as session:
        run = await session.scalar(select(EvaluationRunRecord))
        fixtures = list(await session.scalars(select(StateFixtureRecord)))

    assert run is not None
    assert run.status == expected_status
    assert run.terminal_reason == type(exc).__name__
    assert run.completed_cases == 0
    assert fixtures
    assert all(fixture.active is False for fixture in fixtures)
    assert all(fixture.cleaned_at is not None for fixture in fixtures)

    rows = await RunRepository(session_factory).get_report_rows(run.id)
    assert any(event["event_type"] == event_type for event in rows["events"])
    assert "do-not-persist" not in repr(rows)
