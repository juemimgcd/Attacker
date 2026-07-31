from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import RunJobRecord, WorkerHeartbeatRecord
from app.schemas.job_schema import JobStatus, RunJobCreate

TERMINAL_STATUSES = {
    JobStatus.succeeded.value,
    JobStatus.failed.value,
    JobStatus.cancelled.value,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _fingerprint(kind: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"kind": kind, "payload": payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class JobRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def enqueue(self, request: RunJobCreate, *, default_max_attempts: int) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        payload_json = dict(payload["payload"])
        fingerprint = _fingerprint(request.kind.value, payload_json)
        async with self.session_factory.begin() as session:
            existing = await session.scalar(
                select(RunJobRecord).where(RunJobRecord.request_id == request.request_id)
            )
            if existing is not None:
                self._assert_same_request(existing, request.kind.value, fingerprint)
                return self.as_dict(existing)

            record = RunJobRecord(
                id=str(uuid4()),
                request_id=request.request_id,
                kind=request.kind.value,
                payload_fingerprint=fingerprint,
                payload_json=payload_json,
                status=JobStatus.queued.value,
                priority=request.priority,
                available_at=_now(),
                max_attempts=request.max_attempts or default_max_attempts,
            )
            try:
                async with session.begin_nested():
                    session.add(record)
                    await session.flush()
            except IntegrityError:
                existing = await session.scalar(
                    select(RunJobRecord).where(RunJobRecord.request_id == request.request_id)
                )
                if existing is None:
                    raise
                self._assert_same_request(existing, request.kind.value, fingerprint)
                return self.as_dict(existing)
            return self.as_dict(record)

    async def get(self, job_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            record = await session.get(RunJobRecord, job_id)
            if record is None:
                raise LookupError(f"job {job_id} not found")
            return self.as_dict(record)

    async def list(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            statement = select(RunJobRecord)
            if status is not None:
                statement = statement.where(RunJobRecord.status == status)
            records = (
                await session.scalars(
                    statement.order_by(RunJobRecord.created_at.desc()).limit(limit)
                )
            ).all()
            return [self.as_dict(record) for record in records]

    async def recover_expired(self) -> dict[str, int]:
        now = _now()
        retried = 0
        failed = 0
        async with self.session_factory.begin() as session:
            records = (
                await session.scalars(
                    select(RunJobRecord)
                    .where(
                        RunJobRecord.status.in_([JobStatus.leased.value, JobStatus.running.value]),
                        RunJobRecord.lease_expires_at < now,
                    )
                    .with_for_update(skip_locked=session.get_bind().dialect.name == "postgresql")
                )
            ).all()
            for record in records:
                record.lease_owner = None
                record.lease_token = None
                record.lease_expires_at = None
                record.updated_at = now
                if record.cancel_requested:
                    record.status = JobStatus.cancelled.value
                    record.completed_at = now
                elif record.attempts >= record.max_attempts:
                    record.status = JobStatus.failed.value
                    record.error_code = "job_lease_exhausted"
                    record.error_summary = "worker lease expired and retry budget was exhausted"
                    record.completed_at = now
                    failed += 1
                else:
                    record.status = JobStatus.retry_wait.value
                    record.available_at = now
                    retried += 1
        return {"retried": retried, "failed": failed}

    async def claim(self, *, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        await self.recover_expired()
        now = _now()
        eligible_statuses = [JobStatus.queued.value, JobStatus.retry_wait.value]
        async with self.session_factory.begin() as session:
            statement = (
                select(RunJobRecord)
                .where(
                    RunJobRecord.status.in_(eligible_statuses),
                    RunJobRecord.available_at <= now,
                    RunJobRecord.cancel_requested.is_(False),
                    RunJobRecord.attempts < RunJobRecord.max_attempts,
                )
                .order_by(
                    RunJobRecord.priority.desc(),
                    RunJobRecord.available_at,
                    RunJobRecord.created_at,
                )
                .limit(1)
            )
            if session.get_bind().dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            record = await session.scalar(statement)
            if record is None:
                return None

            previous_status = record.status
            lease_token = secrets.token_hex(32)
            claimed = cast(
                CursorResult[Any],
                await session.execute(
                    update(RunJobRecord)
                    .where(
                        RunJobRecord.id == record.id,
                        RunJobRecord.status == previous_status,
                        RunJobRecord.cancel_requested.is_(False),
                    )
                    .values(
                        status=JobStatus.leased.value,
                        lease_owner=worker_id,
                        lease_token=lease_token,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        attempts=RunJobRecord.attempts + 1,
                        updated_at=now,
                    )
                ),
            )
            if claimed.rowcount != 1:
                return None
            await session.flush()
            await session.refresh(record)
            return self.as_dict(record, include_lease_token=True)

    async def mark_running(self, job_id: str, *, worker_id: str, lease_token: str) -> None:
        await self._lease_update(
            job_id,
            worker_id=worker_id,
            lease_token=lease_token,
            values={"status": JobStatus.running.value, "updated_at": _now()},
            allowed_statuses={JobStatus.leased.value},
        )

    async def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        lease_seconds: int,
    ) -> None:
        now = _now()
        await self._lease_update(
            job_id,
            worker_id=worker_id,
            lease_token=lease_token,
            values={
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "updated_at": now,
            },
            allowed_statuses={JobStatus.leased.value, JobStatus.running.value},
        )

    async def complete(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        now = _now()
        await self._lease_update(
            job_id,
            worker_id=worker_id,
            lease_token=lease_token,
            values={
                "status": JobStatus.succeeded.value,
                "result_json": result,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "error_code": None,
                "error_summary": None,
                "updated_at": now,
                "completed_at": now,
            },
            allowed_statuses={JobStatus.leased.value, JobStatus.running.value},
        )
        return await self.get(job_id)

    async def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        error_code: str,
        error_summary: str,
    ) -> dict[str, Any]:
        async with self.session_factory.begin() as session:
            record = await session.get(RunJobRecord, job_id, with_for_update=True)
            record = self._assert_lease(record, job_id, worker_id, lease_token)
            now = _now()
            record.lease_owner = None
            record.lease_token = None
            record.lease_expires_at = None
            record.error_code = error_code[:100]
            record.error_summary = error_summary[:2_000]
            record.updated_at = now
            if record.cancel_requested:
                record.status = JobStatus.cancelled.value
                record.completed_at = now
            elif record.attempts < record.max_attempts:
                record.status = JobStatus.retry_wait.value
                delay_seconds = min(300, 2 ** max(record.attempts - 1, 0))
                record.available_at = now + timedelta(seconds=delay_seconds)
            else:
                record.status = JobStatus.failed.value
                record.completed_at = now
            await session.flush()
            return self.as_dict(record)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        async with self.session_factory.begin() as session:
            record = await session.get(RunJobRecord, job_id, with_for_update=True)
            if record is None:
                raise LookupError(f"job {job_id} not found")
            if record.status in TERMINAL_STATUSES:
                return self.as_dict(record)
            now = _now()
            record.cancel_requested = True
            record.updated_at = now
            if record.status in {JobStatus.queued.value, JobStatus.retry_wait.value}:
                record.status = JobStatus.cancelled.value
                record.completed_at = now
            return self.as_dict(record)

    async def retry(self, job_id: str) -> dict[str, Any]:
        async with self.session_factory.begin() as session:
            record = await session.get(RunJobRecord, job_id, with_for_update=True)
            if record is None:
                raise LookupError(f"job {job_id} not found")
            if record.status not in {JobStatus.failed.value, JobStatus.cancelled.value}:
                raise ValueError("only failed or cancelled jobs can be retried")
            now = _now()
            record.status = JobStatus.queued.value
            record.available_at = now
            record.lease_owner = None
            record.lease_token = None
            record.lease_expires_at = None
            record.cancel_requested = False
            record.error_code = None
            record.error_summary = None
            record.completed_at = None
            record.attempts = 0
            record.updated_at = now
            return self.as_dict(record)

    async def is_cancel_requested(self, job_id: str) -> bool:
        async with self.session_factory() as session:
            value = await session.scalar(
                select(RunJobRecord.cancel_requested).where(RunJobRecord.id == job_id)
            )
            if value is None:
                raise LookupError(f"job {job_id} not found")
            return bool(value)

    async def worker_heartbeat(
        self,
        *,
        worker_id: str,
        active_jobs: int,
        draining: bool,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = _now()
        async with self.session_factory.begin() as session:
            record = await session.get(WorkerHeartbeatRecord, worker_id)
            if record is None:
                session.add(
                    WorkerHeartbeatRecord(
                        worker_id=worker_id,
                        started_at=now,
                        heartbeat_at=now,
                        active_jobs=active_jobs,
                        draining=draining,
                        metadata_json=metadata or {},
                    )
                )
            else:
                record.heartbeat_at = now
                record.active_jobs = active_jobs
                record.draining = draining
                record.metadata_json = metadata or record.metadata_json

    async def metrics_snapshot(self, *, stale_after_seconds: int) -> dict[str, Any]:
        now = _now()
        stale_before = now - timedelta(seconds=stale_after_seconds)
        ready_statuses = [JobStatus.queued.value, JobStatus.retry_wait.value]
        active_statuses = [JobStatus.leased.value, JobStatus.running.value]
        async with self.session_factory() as session:
            status_rows = (
                await session.execute(
                    select(RunJobRecord.status, func.count(RunJobRecord.id)).group_by(
                        RunJobRecord.status
                    )
                )
            ).all()
            oldest_ready_at = await session.scalar(
                select(func.min(RunJobRecord.available_at)).where(
                    RunJobRecord.status.in_(ready_statuses),
                    RunJobRecord.available_at <= now,
                )
            )
            expired_leases = (
                await session.scalar(
                    select(func.count(RunJobRecord.id)).where(
                        RunJobRecord.status.in_(active_statuses),
                        RunJobRecord.lease_expires_at < now,
                    )
                )
                or 0
            )
            stale_workers = (
                await session.scalar(
                    select(func.count(WorkerHeartbeatRecord.worker_id)).where(
                        WorkerHeartbeatRecord.draining.is_(False),
                        WorkerHeartbeatRecord.heartbeat_at < stale_before,
                    )
                )
                or 0
            )
        if oldest_ready_at is not None and oldest_ready_at.tzinfo is None:
            oldest_ready_at = oldest_ready_at.replace(tzinfo=UTC)
        oldest_age = (
            max((now - oldest_ready_at).total_seconds(), 0.0)
            if oldest_ready_at is not None
            else 0.0
        )
        return {
            "status_counts": {
                str(status): int(count)
                for status, count in sorted(status_rows, key=lambda item: str(item[0]))
            },
            "oldest_ready_age_seconds": oldest_age,
            "expired_leases": int(expired_leases),
            "stale_workers": int(stale_workers),
        }

    async def _lease_update(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        values: dict[str, Any],
        allowed_statuses: set[str],
    ) -> None:
        async with self.session_factory.begin() as session:
            updated = cast(
                CursorResult[Any],
                await session.execute(
                    update(RunJobRecord)
                    .where(
                        RunJobRecord.id == job_id,
                        RunJobRecord.lease_owner == worker_id,
                        RunJobRecord.lease_token == lease_token,
                        RunJobRecord.status.in_(allowed_statuses),
                        or_(
                            RunJobRecord.lease_expires_at.is_(None),
                            RunJobRecord.lease_expires_at > _now(),
                        ),
                    )
                    .values(**values)
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"job {job_id} lease is missing, expired, or owned elsewhere")

    @staticmethod
    def _assert_same_request(record: RunJobRecord, kind: str, fingerprint: str) -> None:
        if record.kind != kind or record.payload_fingerprint != fingerprint:
            raise ValueError("request_id was reused with a different job payload")

    @staticmethod
    def _assert_lease(
        record: RunJobRecord | None,
        job_id: str,
        worker_id: str,
        lease_token: str,
    ) -> RunJobRecord:
        if record is None:
            raise LookupError(f"job {job_id} not found")
        lease_expires_at = record.lease_expires_at
        if lease_expires_at is not None and lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
        if (
            record.lease_owner != worker_id
            or record.lease_token != lease_token
            or record.status not in {JobStatus.leased.value, JobStatus.running.value}
            or lease_expires_at is None
            or lease_expires_at <= _now()
        ):
            raise RuntimeError(f"job {job_id} lease is missing, expired, or owned elsewhere")
        return record

    @staticmethod
    def as_dict(
        record: RunJobRecord,
        *,
        include_lease_token: bool = False,
    ) -> dict[str, Any]:
        result = {
            "id": record.id,
            "request_id": record.request_id,
            "kind": record.kind,
            "status": record.status,
            "priority": record.priority,
            "attempts": record.attempts,
            "max_attempts": record.max_attempts,
            "available_at": record.available_at,
            "lease_owner": record.lease_owner,
            "lease_expires_at": record.lease_expires_at,
            "result": record.result_json,
            "error_code": record.error_code,
            "error_summary": record.error_summary,
            "cancel_requested": record.cancel_requested,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "completed_at": record.completed_at,
        }
        if include_lease_token:
            result["lease_token"] = record.lease_token
            result["payload"] = record.payload_json
        return result
