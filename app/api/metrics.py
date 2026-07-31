"""Prometheus 指标导出接口，并使用独立密钥保护运维数据。"""

from __future__ import annotations

from hmac import compare_digest
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status
from starlette.responses import Response

from app.observability import metrics_response
from conf.settings import settings

router = APIRouter(tags=["metrics"])


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(
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
    return metrics_response()
