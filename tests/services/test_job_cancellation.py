import asyncio
from pathlib import Path
from typing import Any

from app.repositories.job_repository import JobRepository
from app.repositories.run_repository import RunRepository
from app.schemas.job_schema import JobKind, RunJobCreate
from app.schemas.run_schema import LoadedDataset, RunBudget
from app.services.job_service import JobWorker
from app.services.run_lifecycle import RunCreatedHook
from conf.settings import WorkerSettings


class _BlockingDispatcher:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.started = asyncio.Event()
        self.cancelled = False
        self.iterations = 0

    async def dispatch(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        on_run_created: RunCreatedHook | None = None,
    ) -> dict[str, Any]:
        del kind, payload
        try:
            if on_run_created is not None:
                await on_run_created(self.run_id)
            self.started.set()
            while True:
                self.iterations += 1
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _SlowBindingJobRepository(JobRepository):
    def __init__(self, session_factory) -> None:
        super().__init__(session_factory)
        self.binding_started = asyncio.Event()
        self.release_binding = asyncio.Event()

    async def bind_run(
        self,
        job_id: str,
        *,
        run_id: str,
        worker_id: str,
        lease_token: str,
    ) -> None:
        self.binding_started.set()
        await self.release_binding.wait()
        await super().bind_run(
            job_id,
            run_id=run_id,
            worker_id=worker_id,
            lease_token=lease_token,
        )


async def test_running_job_cancel_stops_dispatch_and_retains_run_binding(
    session_factory,
) -> None:
    run_id = await RunRepository(session_factory).create_run(
        target_snapshot={"name": "cancel-target", "endpoint": "http://localhost"},
        dataset=LoadedDataset(
            name="empty",
            version="1",
            source_path=Path("empty"),
            sha256="c" * 64,
            cases=[],
            snapshot={"cases": []},
        ),
        budget=RunBudget(),
    )
    repository = JobRepository(session_factory)
    queued = await repository.enqueue(
        RunJobCreate(
            request_id="job-running-cancel-001",
            kind=JobKind.stateful,
            payload={"profile": "hardened"},
        ),
        default_max_attempts=3,
    )
    claimed = await repository.claim(worker_id="worker-cancel", lease_seconds=30)
    assert claimed is not None and claimed["id"] == queued["id"]

    dispatcher = _BlockingDispatcher(run_id)
    worker = JobWorker(
        worker_id="worker-cancel",
        repository=repository,
        dispatcher=dispatcher,  # type: ignore[arg-type]
        settings=WorkerSettings(
            concurrency=1,
            poll_seconds=0.1,
            lease_seconds=30,
            heartbeat_seconds=5,
            shutdown_grace_seconds=5,
        ),
    )
    execution = asyncio.create_task(worker._execute(claimed))
    await asyncio.wait_for(dispatcher.started.wait(), timeout=1)

    running = await repository.get(queued["id"])
    assert running["status"] == "running"
    assert running["run_id"] == run_id
    await repository.cancel(queued["id"])
    await asyncio.wait_for(execution, timeout=2)

    cancelled = await repository.get(queued["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["run_id"] == run_id
    assert dispatcher.cancelled is True
    stopped_at = dispatcher.iterations
    await asyncio.sleep(0.05)
    assert dispatcher.iterations == stopped_at


async def test_cancel_during_run_binding_persists_binding_before_terminalizing_job(
    session_factory,
) -> None:
    run_id = await RunRepository(session_factory).create_run(
        target_snapshot={"name": "binding-race", "endpoint": "http://localhost"},
        dataset=LoadedDataset(
            name="empty-binding-race",
            version="1",
            source_path=Path("empty"),
            sha256="b" * 64,
            cases=[],
            snapshot={"cases": []},
        ),
        budget=RunBudget(),
    )
    repository = _SlowBindingJobRepository(session_factory)
    queued = await repository.enqueue(
        RunJobCreate(
            request_id="job-binding-cancel-001",
            kind=JobKind.stateful,
            payload={"profile": "hardened"},
        ),
        default_max_attempts=3,
    )
    claimed = await repository.claim(worker_id="worker-binding", lease_seconds=30)
    assert claimed is not None
    dispatcher = _BlockingDispatcher(run_id)
    worker = JobWorker(
        worker_id="worker-binding",
        repository=repository,
        dispatcher=dispatcher,  # type: ignore[arg-type]
        settings=WorkerSettings(
            concurrency=1,
            poll_seconds=0.1,
            lease_seconds=30,
            heartbeat_seconds=5,
            shutdown_grace_seconds=5,
        ),
    )
    execution = asyncio.create_task(worker._execute(claimed))
    await asyncio.wait_for(repository.binding_started.wait(), timeout=1)

    await repository.cancel(queued["id"])
    await asyncio.sleep(0.15)
    repository.release_binding.set()
    await asyncio.wait_for(execution, timeout=2)

    cancelled = await repository.get(queued["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["run_id"] == run_id
    assert dispatcher.cancelled is True
