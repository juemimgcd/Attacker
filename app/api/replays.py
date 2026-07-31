"""基于历史 Run 快照重新评测并生成 Finding 差异的 Replay 接口。"""

from fastapi import APIRouter, HTTPException, Request

from app.schemas.replay_schema import ReplayRunRequest

router = APIRouter(prefix="/runs", tags=["replays"])


@router.post("/{source_run_id}/replay")
async def replay_run(
    source_run_id: str,
    payload: ReplayRunRequest,
    request: Request,
) -> dict:
    try:
        return await request.app.state.replay_service.replay(source_run_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{run_id}/replay")
async def get_replay(run_id: str, request: Request) -> dict:
    replays = await request.app.state.stateful_repository.list_replays(run_id)
    if not replays:
        raise HTTPException(status_code=404, detail=f"replay for run {run_id} not found")
    return {"run_id": run_id, "replays": replays}
