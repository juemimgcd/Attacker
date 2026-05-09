import asyncio
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
from app.schemas.evidence_schema import EvidenceEvent
from conf.storage_conf import get_parquet_evidence_dir


# 封装 Parquet 证据归档目录的异步文件操作边界。
class ParquetStore:
    # 初始化 Parquet 存储对象并确定证据归档根目录。
    def __init__(self, evidence_dir: Path | None = None) -> None:
        self.evidence_dir = evidence_dir or get_parquet_evidence_dir()

    # 异步确保 Parquet 证据归档目录存在。
    async def ensure_dirs(self) -> None:
        await asyncio.to_thread(self._ensure_dirs_sync)

    # 在线程中同步创建 Parquet 证据归档目录。
    def _ensure_dirs_sync(self) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    # 异步把证据事件写入按 run_id 分区的 Parquet 文件。
    async def write_evidence_event(self, event: EvidenceEvent) -> Path:
        return await asyncio.to_thread(self._write_evidence_event_sync, event)

    # 在线程中同步序列化证据事件并写入 Parquet 文件。
    def _write_evidence_event_sync(self, event: EvidenceEvent) -> Path:
        dataset_dir = self.evidence_dir / "evidence_events" / f"run_id={event.run_id}"
        dataset_dir.mkdir(parents=True, exist_ok=True)
        file_path = dataset_dir / f"{event.evidence_id}.parquet"

        row = event.model_dump(mode="json")
        table = pa.Table.from_pylist([row])
        pq.write_table(table, file_path)
        return file_path
