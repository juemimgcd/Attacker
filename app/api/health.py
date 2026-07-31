"""存活与就绪探针；就绪状态反映数据库、checkpoint 和运行时依赖是否可用。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.observability import set_readiness
from conf.settings import settings

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {
        "status": "alive",
        "service": settings.app.app_name,
        "environment": settings.app.app_env,
    }


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    payload, ready = await _readiness_payload(request)
    return JSONResponse(
        payload,
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
    )


@router.get("/health")
async def health_check(request: Request) -> JSONResponse:
    payload, ready = await _readiness_payload(request)
    payload["status"] = "ok" if ready else "unavailable"
    return JSONResponse(
        payload,
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
    )


async def _readiness_payload(request: Request) -> tuple[dict[str, Any], bool]:
    database = getattr(request.app.state, "database", None)
    database_status = (
        await database.readiness()
        if database is not None
        else {"status": "unavailable", "error": "runtime_not_initialized"}
    )
    catalog_ready = bool(getattr(request.app.state, "catalog_ready", False))
    checkpoint_ready = getattr(request.app.state, "checkpointer", None) is not None
    dependencies = {
        "database": database_status,
        "checkpoint": {"status": "ready" if checkpoint_ready else "unavailable"},
        "equipment_catalog": {"status": "ready" if catalog_ready else "unavailable"},
    }
    set_readiness("database", database_status["status"] == "ready")
    set_readiness("checkpoint", checkpoint_ready)
    set_readiness("equipment_catalog", catalog_ready)
    ready = all(item["status"] == "ready" for item in dependencies.values())
    return (
        {
            "status": "ready" if ready else "unavailable",
            "service": settings.app.app_name,
            "environment": settings.app.app_env,
            "dependencies": dependencies,
        },
        ready,
    )
