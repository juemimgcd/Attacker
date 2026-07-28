from fastapi import APIRouter, HTTPException, Request

from app.schemas.graybox_schema import ApprovalResolveRequest

router = APIRouter(prefix="/runs", tags=["approvals"])


@router.get("/{run_id}/approvals")
async def list_approvals(run_id: str, request: Request) -> list[dict]:
    try:
        await request.app.state.adaptive_repository.get_run(run_id)
        return await request.app.state.adaptive_repository.list_approvals(run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{run_id}/approvals/{approval_id}")
async def resolve_approval(
    run_id: str,
    approval_id: str,
    payload: ApprovalResolveRequest,
    request: Request,
) -> dict:
    try:
        return await request.app.state.adaptive_run_service.resume(
            run_id=run_id,
            approval_id=approval_id,
            approved=payload.approved,
            resolved_by=payload.resolved_by,
            reason=payload.reason,
            target=payload.target,
            planner=payload.planner,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
