from fastapi import APIRouter

from conf.settings import settings

router = APIRouter(tags=["health"])


# 返回服务基础健康状态和当前运行环境。
@router.get("/health")
async def health_check() -> dict:
    return {
        "status": "ok",
        "service": settings.app.app_name,
        "environment": settings.app.app_env,
    }
