from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.schemas.job_schema import JobStatus, RunJobCreate
from conf.settings import settings

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _require_queue_enabled() -> None:
    if not settings.worker.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="durable job queue is disabled",
        )


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_job(payload: RunJobCreate, request: Request) -> dict:
    _require_queue_enabled()
    try:
        return await request.app.state.job_repository.enqueue(
            payload,
            default_max_attempts=settings.worker.max_attempts,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("")
async def list_jobs(
    request: Request,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    _require_queue_enabled()
    return await request.app.state.job_repository.list(
        status=job_status.value if job_status else None,
        limit=limit,
    )


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request) -> dict:
    _require_queue_enabled()
    try:
        return await request.app.state.job_repository.get(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request) -> dict:
    _require_queue_enabled()
    try:
        return await request.app.state.job_repository.cancel(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{job_id}/retry")
async def retry_job(job_id: str, request: Request) -> dict:
    _require_queue_enabled()
    try:
        return await request.app.state.job_repository.retry(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
