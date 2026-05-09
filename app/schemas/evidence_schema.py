from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


# 定义证据关联的外部对象存储引用信息。
class ArtifactRef(BaseModel):
    object_key: str
    artifact_type: str
    storage_backend: str = "minio"


# 定义一次攻击运行产生的标准化证据事件。
class EvidenceEvent(BaseModel):
    evidence_id: str
    run_id: str
    target_name: str
    sample_id: str
    event_type: str = "attack_run"
    request_body: dict[str, Any]
    response_text: str
    response_body: dict[str, Any] | list[Any] | None = None
    judge_result: dict[str, Any]
    latency_ms: int
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# 定义证据保存后的落库、归档和对象引用结果。
class EvidenceSaveResult(BaseModel):
    evidence_id: str
    run_id: str
    duckdb_saved: bool
    parquet_path: str | None = None
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
