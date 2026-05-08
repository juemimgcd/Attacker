from conf.storage_conf import get_qdrant_config


class QdrantStore:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or get_qdrant_config()

    async def upsert_attack_case(
        self,
        case_id: str,
        vector: list[float],
        payload: dict,
    ) -> None:
        # Day 1 只定义边界。Day 4 再接入 qdrant async client。
        return None

    async def search_similar_cases(
        self,
        vector: list[float],
        limit: int = 5,
    ) -> list[dict]:
        # Day 1 只定义边界。Day 4 再接入 qdrant async client。
        return []