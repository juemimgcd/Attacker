from uuid import uuid4

from pydantic import BaseModel
from fastapi import APIRouter

from app.schemas.attack_sample_schema import AttackSample
from app.schemas.target_schema import TargetConfig
from app.services.attack_executor import attack_executor
from app.services.evidence_service import evidence_service

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
async def dry_run_and_save(payload: DryRunRequest) -> dict:
    attack_result = await attack_executor.run_once(
        target=payload.target,
        sample=payload.sample,
    )
    save_result = await evidence_service.save_attack_result(
        run_id=str(uuid4()),
        result=attack_result,
    )
    return {
        "attack_result": attack_result.model_dump(mode="json"),
        "save_result": save_result.model_dump(mode="json"),
    }



from app.schemas.index_schema import SearchSimilarRequest
from app.services.semantic_index_service import semantic_index_service


@router.post("/tests/index-sample")
async def index_sample(payload: AttackSample) -> dict:
    result = await semantic_index_service.index_sample(payload)
    return result.model_dump(mode="json")


@router.post("/tests/search-similar")
async def search_similar(payload: SearchSimilarRequest) -> dict:
    results = await semantic_index_service.search_similar(payload)
    return {
        "results": [result.model_dump(mode="json") for result in results],
    }















