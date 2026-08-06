from asyncio import CancelledError
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.runs import create_stateful_run
from app.models import EvaluationRunRecord, StateFixtureRecord
from app.repositories.run_repository import RunRepository
from app.repositories.stateful_repository import StatefulRepository
from app.schemas.stateful_schema import StatefulRunRequest
from app.services.sample_loader import (
    BlackBoxDatasetLoader,
    GrayBoxDatasetLoader,
    StatefulDatasetLoader,
)
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
    ("loader", "dataset_path", "public_error"),
    [
        (
            BlackBoxDatasetLoader(),
            Path("samples/blackbox/missing.yaml"),
            "black-box dataset file not found",
        ),
        (
            GrayBoxDatasetLoader(),
            Path("samples/graybox/missing.yaml"),
            "gray-box dataset file not found",
        ),
        (
            StatefulDatasetLoader(),
            Path("samples/stateful/missing.yaml"),
            "stateful dataset file not found",
        ),
    ],
)
async def test_dataset_loaders_hide_resolved_paths_for_missing_files(
    loader: Any,
    dataset_path: Path,
    public_error: str,
) -> None:
    with pytest.raises(FileNotFoundError) as raised:
        await loader.load(dataset_path)

    assert str(raised.value) == public_error
    assert str(Path.cwd()) not in str(raised.value)


async def test_stateful_api_returns_stable_missing_dataset_error(session_factory) -> None:
    service = StatefulRunService(StatefulRepository(session_factory))
    request = cast(
        Any,
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(stateful_run_service=service))),
    )

    with pytest.raises(HTTPException) as raised:
        await create_stateful_run(
            StatefulRunRequest(dataset_path="samples/stateful/missing.yaml"),
            request,
        )

    assert raised.value.status_code == 422
    assert raised.value.detail == "stateful dataset file not found"


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


async def test_stateful_interruption_retries_transient_cleanup_failure(
    session_factory,
    monkeypatch,
) -> None:
    repository = StatefulRepository(session_factory)
    original_cleanup = repository.cleanup
    cleanup_attempts = 0

    async def flaky_cleanup(*, run_id: str, scope_id: str, operation_id: str):
        nonlocal cleanup_attempts
        if scope_id.endswith(":st_memory_poisoning_attack"):
            cleanup_attempts += 1
            if cleanup_attempts == 1:
                raise RuntimeError("cleanup credential=do-not-persist")
        return await original_cleanup(
            run_id=run_id,
            scope_id=scope_id,
            operation_id=operation_id,
        )

    monkeypatch.setattr(repository, "cleanup", flaky_cleanup)
    service = StatefulRunService(repository)
    service.memory = cast(
        Any,
        InterruptingMemoryAdapter(repository, RuntimeError("execution interrupted")),
    )

    with pytest.raises(RuntimeError, match="execution interrupted"):
        await service.run(StatefulRunRequest(case_ids=["st_memory_poisoning_attack"]))

    async with session_factory() as session:
        run = await session.scalar(select(EvaluationRunRecord))
        fixtures = list(await session.scalars(select(StateFixtureRecord)))

    assert run is not None
    assert cleanup_attempts == 2
    assert all(fixture.active is False for fixture in fixtures)
    rows = await RunRepository(session_factory).get_report_rows(run.id)
    assert not any(event["event_type"] == "state_cleanup_failed" for event in rows["events"])
    assert "do-not-persist" not in repr(rows)


async def test_stateful_interruption_persists_unrecovered_cleanup_failure(
    session_factory,
    monkeypatch,
) -> None:
    repository = StatefulRepository(session_factory)
    original_cleanup = repository.cleanup
    cleanup_attempts = 0

    async def failing_cleanup(*, run_id: str, scope_id: str, operation_id: str):
        nonlocal cleanup_attempts
        if scope_id.endswith(":st_memory_poisoning_attack"):
            cleanup_attempts += 1
            raise RuntimeError("cleanup credential=do-not-persist")
        return await original_cleanup(
            run_id=run_id,
            scope_id=scope_id,
            operation_id=operation_id,
        )

    monkeypatch.setattr(repository, "cleanup", failing_cleanup)
    service = StatefulRunService(repository)
    service.memory = cast(
        Any,
        InterruptingMemoryAdapter(repository, RuntimeError("execution interrupted")),
    )

    with pytest.raises(RuntimeError, match="execution interrupted"):
        await service.run(StatefulRunRequest(case_ids=["st_memory_poisoning_attack"]))

    async with session_factory() as session:
        run = await session.scalar(select(EvaluationRunRecord))
        fixtures = list(await session.scalars(select(StateFixtureRecord)))

    assert run is not None
    assert cleanup_attempts == 2
    assert any(
        fixture.active and fixture.scope_id.endswith(":st_memory_poisoning_attack")
        for fixture in fixtures
    )
    assert all(
        fixture.active is False
        for fixture in fixtures
        if fixture.scope_id.endswith(":protected-control")
    )
    rows = await RunRepository(session_factory).get_report_rows(run.id)
    cleanup_failure = next(
        event for event in rows["events"] if event["event_type"] == "state_cleanup_failed"
    )
    assert cleanup_failure["evidence"]["cleanup_failure"]["attempts"] == 2
    assert cleanup_failure["evidence"]["cleanup_failure"]["error_type"] == "RuntimeError"
    terminal = next(event for event in rows["events"] if event["event_type"] == "run_failed")
    assert terminal["evidence"]["cleanup_incomplete"] is True
    assert terminal["evidence"]["cleanup_failures"] == [
        cleanup_failure["evidence"]["cleanup_failure"]
    ]
    assert "do-not-persist" not in repr(rows)
