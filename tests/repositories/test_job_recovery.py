from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.models import RunJobRecord, WorkerHeartbeatRecord
from app.repositories.job_repository import JobRepository
from app.schemas.job_schema import JobKind, RunJobCreate


def _request(request_id: str) -> RunJobCreate:
    return RunJobCreate(
        request_id=request_id,
        kind=JobKind.stateful,
        payload={"profile": "hardened"},
        max_attempts=2,
    )


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_rejects_changed_payload(session_factory) -> None:
    repository = JobRepository(session_factory)
    first = await repository.enqueue(_request("job-idempotent-001"), default_max_attempts=3)
    repeated = await repository.enqueue(_request("job-idempotent-001"), default_max_attempts=3)

    assert repeated["id"] == first["id"]

    changed = _request("job-idempotent-001").model_copy(update={"payload": {"profile": "baseline"}})
    with pytest.raises(ValueError, match="different job payload"):
        await repository.enqueue(changed, default_max_attempts=3)


@pytest.mark.asyncio
async def test_expired_leases_retry_then_exhaust_the_budget(session_factory) -> None:
    repository = JobRepository(session_factory)
    queued = await repository.enqueue(_request("job-expiry-001"), default_max_attempts=3)
    first = await repository.claim(worker_id="worker-a", lease_seconds=30)
    assert first is not None and first["id"] == queued["id"]

    async with session_factory.begin() as session:
        await session.execute(
            update(RunJobRecord)
            .where(RunJobRecord.id == queued["id"])
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await repository.recover_expired() == {"retried": 1, "failed": 0}

    second = await repository.claim(worker_id="worker-b", lease_seconds=30)
    assert second is not None and second["attempts"] == 2
    async with session_factory.begin() as session:
        await session.execute(
            update(RunJobRecord)
            .where(RunJobRecord.id == queued["id"])
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await repository.recover_expired() == {"retried": 0, "failed": 1}
    exhausted = await repository.get(queued["id"])
    assert exhausted["status"] == "failed"
    assert exhausted["error_code"] == "job_lease_exhausted"


@pytest.mark.asyncio
async def test_queued_cancellation_is_terminal_and_not_claimed(session_factory) -> None:
    repository = JobRepository(session_factory)
    queued = await repository.enqueue(_request("job-cancel-001"), default_max_attempts=3)

    cancelled = await repository.cancel(queued["id"])

    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested"] is True
    assert await repository.claim(worker_id="worker-a", lease_seconds=30) is None


@pytest.mark.asyncio
async def test_metrics_snapshot_contains_only_bounded_aggregates(session_factory) -> None:
    repository = JobRepository(session_factory)
    await repository.enqueue(_request("job-metrics-queued"), default_max_attempts=3)
    await repository.enqueue(_request("job-metrics-leased"), default_max_attempts=3)
    claimed = await repository.claim(worker_id="worker-current", lease_seconds=30)
    assert claimed is not None

    now = datetime.now(UTC)
    async with session_factory.begin() as session:
        await session.execute(
            update(RunJobRecord)
            .where(RunJobRecord.id == claimed["id"])
            .values(lease_expires_at=now - timedelta(seconds=1))
        )
        session.add(
            WorkerHeartbeatRecord(
                worker_id="worker-stale",
                started_at=now - timedelta(minutes=10),
                heartbeat_at=now - timedelta(minutes=5),
                active_jobs=1,
                draining=False,
                metadata_json={"hostname": "must-not-leak"},
            )
        )

    snapshot = await repository.metrics_snapshot(stale_after_seconds=60)

    assert snapshot["status_counts"] == {"leased": 1, "queued": 1}
    assert snapshot["expired_leases"] == 1
    assert snapshot["stale_workers"] == 1
    assert snapshot["oldest_ready_age_seconds"] >= 0
    assert "worker-stale" not in str(snapshot)
    assert "job-metrics" not in str(snapshot)
