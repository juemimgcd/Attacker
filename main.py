"""Attacker FastAPI 应用入口；路由和资源生命周期在这里统一装配。"""

from fastapi import Depends, FastAPI

from app.api.approvals import router as approvals_router
from app.api.equipment import router as equipment_router
from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.metrics import router as metrics_router
from app.api.replays import router as replays_router
from app.api.runs import router as runs_router
from app.api.security import require_api_key
from app.api.tests import router as tests_router
from app.core.lifespan import create_lifespan
from app.observability import RequestObservabilityMiddleware
from conf.settings import settings


def create_app() -> FastAPI:
    """创建应用并把健康探针与受 API Key 保护的业务路由分开注册。"""

    app = FastAPI(
        title=settings.app.app_name,
        debug=settings.app.debug,
        lifespan=create_lifespan(),
        version="v0.1.0",
    )
    app.add_middleware(RequestObservabilityMiddleware, config=settings.observability)

    protected = [Depends(require_api_key)]
    app.include_router(
        tests_router,
        prefix=settings.app.api_prefix,
        dependencies=protected,
    )
    app.include_router(
        runs_router,
        prefix=settings.app.api_prefix,
        dependencies=protected,
    )
    app.include_router(
        approvals_router,
        prefix=settings.app.api_prefix,
        dependencies=protected,
    )
    app.include_router(
        replays_router,
        prefix=settings.app.api_prefix,
        dependencies=protected,
    )
    app.include_router(
        equipment_router,
        prefix=settings.app.api_prefix,
        dependencies=protected,
    )
    app.include_router(
        jobs_router,
        prefix=settings.app.api_prefix,
        dependencies=protected,
    )
    app.include_router(health_router, prefix=settings.app.api_prefix)
    app.include_router(metrics_router, prefix=settings.app.api_prefix)
    return app


app = create_app()
