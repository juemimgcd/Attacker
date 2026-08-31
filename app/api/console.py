"""同源、无构建步骤的最小操作台入口。"""

from html import escape
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app import __version__
from conf.settings import settings

router = APIRouter(tags=["console"])

_STATIC_ROOT = Path(__file__).resolve().parent.parent / "static"
_INDEX = (_STATIC_ROOT / "console.html").read_text(encoding="utf-8")
_HTML_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; connect-src 'self'; "
        "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "img-src 'self' data:; object-src 'none'; script-src 'self'; style-src 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_ASSET_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
}


@router.get("/console", response_class=HTMLResponse, include_in_schema=False)
async def console() -> HTMLResponse:
    """返回控制台外壳；业务数据仍由受保护的现有 API 提供。"""

    api_prefix = escape(settings.app.api_prefix.rstrip("/"), quote=True)
    return HTMLResponse(
        _INDEX.replace("__API_PREFIX__", api_prefix).replace(
            "__APP_VERSION__", escape(__version__, quote=True)
        ),
        headers=_HTML_HEADERS,
    )


@router.get("/", include_in_schema=False)
async def console_root() -> RedirectResponse:
    return RedirectResponse("/console")


@router.get("/console/assets/console.css", include_in_schema=False)
async def console_css() -> FileResponse:
    return FileResponse(
        _STATIC_ROOT / "console.css",
        media_type="text/css",
        headers=_ASSET_HEADERS,
    )


@router.get("/console/assets/console.js", include_in_schema=False)
async def console_javascript() -> FileResponse:
    return FileResponse(
        _STATIC_ROOT / "console.js",
        media_type="application/javascript",
        headers=_ASSET_HEADERS,
    )
