from fastapi import APIRouter

from conf.settings import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict:
    return {
        "status": "ok",
        "service": settings.app.app_name,
        "environment": settings.app.app_env,
    }