from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class IndexSourceType(str, Enum):
    attack_sample = "attack_sample"
    finding = "finding"


class IndexCollection(str, Enum):
    attack_cases = "attack_cases"
    findings = "findings"


class IndexDocument(BaseModel):
    document_id: str
    source_type: IndexSourceType
    source_id: str
    collection: IndexCollection
    embedding_text: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class IndexWriteResult(BaseModel):
    document_id: str
    source_type: IndexSourceType
    source_id: str
    collection: IndexCollection
    indexed: bool
    backend: str


class SearchSimilarRequest(BaseModel):
    query: str
    collection: IndexCollection = IndexCollection.attack_cases
    limit: int = 5
    filters: dict[str, Any] = Field(default_factory=dict)


class SimilarCaseResult(BaseModel):
    document_id: str
    source_type: IndexSourceType
    source_id: str
    score: float
    title: str
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)