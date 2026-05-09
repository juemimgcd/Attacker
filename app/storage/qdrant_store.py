import asyncio
import json
import math
from pathlib import Path
from typing import Any

from conf.storage_conf import get_qdrant_config


class QdrantStore:
    def __init__(self, fallback_path: Path | None = None) -> None:
        self.config = get_qdrant_config()
        self.fallback_path = fallback_path or Path("data/qdrant_fallback.json")
        self.backend = "local_fallback"

    async def ensure_collection(self, collection: str, vector_size: int) -> None:
        await asyncio.to_thread(
            self._ensure_collection_sync,
            collection,
            vector_size,
        )

    def _ensure_collection_sync(self, collection: str, vector_size: int) -> None:
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        data = self._load()
        collection_data = data.setdefault(collection, {})
        collection_data.setdefault("_meta", {"vector_size": vector_size})
        self._save(data)

    async def upsert_point(
        self,
        collection: str,
        point_id: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> str:
        return await asyncio.to_thread(
            self._upsert_point_sync,
            collection,
            point_id,
            vector,
            payload,
        )

    def _upsert_point_sync(
        self,
        collection: str,
        point_id: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> str:
        data = self._load()
        data.setdefault(collection, {})
        data[collection][point_id] = {
            "id": point_id,
            "vector": vector,
            "payload": payload,
        }
        self._save(data)
        return self.backend

    async def search(
        self,
        collection: str,
        query_vector: list[float],
        limit: int,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._search_sync,
            collection,
            query_vector,
            limit,
            filters or {},
        )

    def _search_sync(
        self,
        collection: str,
        query_vector: list[float],
        limit: int,
        filters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        data = self._load()
        points = data.get(collection, {})
        scored: list[dict[str, Any]] = []

        for point_id, point in points.items():
            if point_id == "_meta":
                continue

            payload = point.get("payload", {})
            if not self._match_filters(payload, filters):
                continue

            scored.append(
                {
                    "id": point["id"],
                    "score": self._cosine_similarity(
                        query_vector,
                        point.get("vector", []),
                    ),
                    "payload": payload,
                }
            )

        return sorted(scored, key=lambda item: item["score"], reverse=True)[:limit]

    def _load(self) -> dict[str, Any]:
        if not self.fallback_path.exists():
            return {}

        return json.loads(self.fallback_path.read_text(encoding="utf-8"))

    def _save(self, data: dict[str, Any]) -> None:
        self.fallback_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _match_filters(
        self,
        payload: dict[str, Any],
        filters: dict[str, Any],
    ) -> bool:
        return all(payload.get(key) == value for key, value in filters.items())

    def _cosine_similarity(self, left: list[float], right: list[float]) -> float:
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm == 0.0 or right_norm == 0.0:
            return 0.0
        return dot / (left_norm * right_norm)
