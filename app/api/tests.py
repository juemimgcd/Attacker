"""本地 dry-run 调试接口；只执行单条合成攻击样本，不创建正式 Run。"""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.schemas.attack_sample_schema import AttackSample
from app.schemas.target_schema import TargetConfig
from app.services.attack_executor import attack_executor

router = APIRouter(tags=["tests"])


# 定义 dry-run 接口需要的目标配置和攻击样本请求体。
class DryRunRequest(BaseModel):
    target: TargetConfig
    sample: AttackSample


# 执行一次最小攻击 dry-run，并返回完整运行结果。
@router.post("/tests/dry-run")
async def dry_run(payload: DryRunRequest) -> dict:
    result = await attack_executor.run_once(
        target=payload.target,
        sample=payload.sample,
    )
    return result.model_dump()


# 执行一次攻击 dry-run，并把结果保存为证据记录。
@router.post("/tests/dry-run-and-save")
async def dry_run_and_save(payload: DryRunRequest, request: Request) -> dict:
    report = await request.app.state.run_service.run_layered_single(
        target=payload.target,
        sample=payload.sample,
    )
    step_result = report["steps"][0]["result"]
    return {
        "attack_result": step_result,
        "run_id": report["run"]["id"],
        "report_urls": {
            "json": f"/runs/{report['run']['id']}/report.json",
            "markdown": f"/runs/{report['run']['id']}/report.md",
        },
    }
