import asyncio
from pathlib import Path

from conf.storage_conf import get_parquet_evidence_dir


class ParquetStore:
    def __init__(self, evidence_dir: Path | None = None) -> None:
        self.evidence_dir = evidence_dir or get_parquet_evidence_dir()

    async def ensure_dirs(self) -> None:
        await asyncio.to_thread(self._ensure_dirs_sync)

    def _ensure_dirs_sync(self) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    async def write_events(self, dataset: str, rows: list[dict]) -> Path:
        return await asyncio.to_thread(self._write_events_sync, dataset, rows)

    def _write_events_sync(self, dataset: str, rows: list[dict]) -> Path:
        # Day 1 只保留边界。Day 3 再接入 pyarrow 写 Parquet。
        dataset_dir = self.evidence_dir / dataset
        dataset_dir.mkdir(parents=True, exist_ok=True)
        return dataset_dir