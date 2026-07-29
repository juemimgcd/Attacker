from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.schemas.graybox_schema import GrayBoxRunRequest
from app.schemas.run_schema import DeterministicRunRequest
from app.schemas.stateful_schema import StatefulRunRequest

router = APIRouter(prefix="/runs", tags=["runs"])


@router.post("/stateful")
async def create_stateful_run(payload: StatefulRunRequest, request: Request) -> dict:
    try:
        return await request.app.state.stateful_run_service.run(payload)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/adaptive")
async def create_adaptive_run(payload: GrayBoxRunRequest, request: Request) -> dict:
    try:
        return await request.app.state.adaptive_run_service.start(payload)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/graybox/deterministic")
async def create_graybox_deterministic_run(
    payload: GrayBoxRunRequest,
    request: Request,
) -> dict:
    try:
        return await request.app.state.deterministic_graybox_service.run(payload)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/deterministic")
async def create_deterministic_run(
    payload: DeterministicRunRequest,
    request: Request,
) -> dict:
    try:
        return await request.app.state.run_service.run(payload)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{run_id}")
async def get_run(run_id: str, request: Request) -> dict:
    try:
        return await request.app.state.report_service.build_json(run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{run_id}/report.json")
async def get_json_report(run_id: str, request: Request) -> dict:
    return await get_run(run_id, request)


@router.get("/{run_id}/report.md", response_class=PlainTextResponse)
async def get_markdown_report(run_id: str, request: Request) -> PlainTextResponse:
    try:
        report = await request.app.state.report_service.build_markdown(run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PlainTextResponse(report, media_type="text/markdown; charset=utf-8")
