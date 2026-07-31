from __future__ import annotations

from hmac import compare_digest
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, status
from loguru import logger
from starlette.responses import Response

from app.observability import (
    metrics_response,
    record_metrics_refresh_failure,
    refresh_job_metrics,
)
from conf.settings import settings

router = APIRouter(tags=["metrics"])


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(
    request: Request,
    metrics_key: Annotated[str | None, Header(alias="X-Metrics-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    if not settings.observability.metrics_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    configured = settings.security.metrics_api_key
    if configured is not None:
        expected = configured.get_secret_value()
        bearer = (
            authorization.removeprefix("Bearer ")
            if authorization and authorization.startswith("Bearer ")
            else None
        )
        supplied = metrics_key or bearer
        if supplied is None or not compare_digest(supplied, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid metrics API key",
            )
    repository = getattr(request.app.state, "job_repository", None)
    if repository is not None:
        try:
            snapshot = await repository.metrics_snapshot(
                stale_after_seconds=settings.worker.heartbeat_seconds * 3
            )
            refresh_job_metrics(snapshot)
        except Exception as exc:  # noqa: BLE001 - metrics must expose refresh failure
            record_metrics_refresh_failure()
            logger.warning(
                "operational metrics refresh failed",
                error_type=type(exc).__name__,
            )
    return metrics_response()
