"""业务 API 的服务级密钥依赖；它是部署门禁，不等同于用户身份或 RBAC。"""

from secrets import compare_digest

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from conf.settings import settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(api_key: str | None = Security(api_key_header)) -> None:
    """在配置密钥时执行常量时间比较；本地未配置密钥时保持开发入口开放。"""

    configured = settings.security.api_key
    if configured is None:
        return
    expected = configured.get_secret_value()
    if api_key is None or not compare_digest(api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
        )
