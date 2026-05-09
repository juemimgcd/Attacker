from uuid import uuid4

from app.schemas.evidence_schema import EvidenceEvent, EvidenceSaveResult
from app.schemas.judge_schema import AttackRunResult
from app.storage.duckdb_store import DuckDBStore
from app.storage.parquet_store import ParquetStore




# 负责把攻击运行结果转换为证据事件并写入持久化存储。
class EvidenceService:
    # 初始化证据服务并注入 DuckDB 和 Parquet 存储边界。
    def __init__(
            self,
            duckdb_store: DuckDBStore | None = None,
            parquet_store:ParquetStore | None = None,
    ) -> None:
        self.duck_db_store = duckdb_store or DuckDBStore()
        self.parquet_store = parquet_store or ParquetStore()

    # 根据攻击运行结果构建标准化证据事件。
    async def build_event(
            self,
            run_id:str,
            result:AttackRunResult

    ) -> EvidenceEvent:
        return EvidenceEvent(
            evidence_id=str(uuid4()),
            run_id=run_id,
            target_name=result.target_name,
            sample_id=result.sample_id,
            request_body=result.request_body,
            response_text=result.target_response.text,
            response_body=result.target_response.body,
            judge_result=result.judge_result.model_dump(mode="json"),
            latency_ms=result.target_response.latency_ms,
            error=result.target_response.error
        )

    # 保存一次攻击运行结果到 DuckDB 和 Parquet，并返回保存结果。
    async def save_attack_result(
            self,
            run_id:str,
            result:AttackRunResult
    )->EvidenceSaveResult:
        event = await self.build_event(run_id=run_id,result=result)
        await self.duck_db_store.initialize_schema()
        await self.duck_db_store.save_test_run(event)
        await self.duck_db_store.save_finding(event)
        parquet_path = await self.parquet_store.write_evidence_event(event)



        return EvidenceSaveResult(
            evidence_id=event.evidence_id,
            run_id=event.run_id,
            duckdb_saved=True,
            parquet_path=str(parquet_path),
        )


evidence_service = EvidenceService()
