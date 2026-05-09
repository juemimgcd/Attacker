from fastapi import FastAPI
from fastapi import FastAPI
from app.api.tests import router as tests_router
from app.api.health import router as health_router
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

    app.include_router(tests_router, prefix=settings.app.api_prefix)
    app.include_router(health_router,prefix=settings.app.api_prefix)
    return app
