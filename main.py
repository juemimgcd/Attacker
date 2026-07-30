from fastapi import Depends, FastAPI

from app.api.approvals import router as approvals_router
from app.api.equipment import router as equipment_router
from app.api.health import router as health_router
from app.api.replays import router as replays_router
from app.api.runs import router as runs_router
from app.api.security import require_api_key
from app.api.tests import router as tests_router
from app.core.lifespan import create_lifespan
from conf.settings import settings


# 创建并配置 FastAPI 应用，注册生命周期和业务路由。
def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app.app_name,
        debug=settings.app.debug,
        lifespan=create_lifespan(),
        version="v0.1.0",
    )

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
    app.include_router(health_router, prefix=settings.app.api_prefix)
    return app


app = create_app()
