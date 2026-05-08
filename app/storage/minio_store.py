import asyncio

from conf.storage_conf import get_minio_config


class MinIOStore:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or get_minio_config()

    async def upload_file(self, object_name: str, file_path: str) -> str:
        return await asyncio.to_thread(self._upload_file_sync, object_name, file_path)

    def _upload_file_sync(self, object_name: str, file_path: str) -> str:
        # Day 1 只定义边界。Day 3 再接入 minio SDK。
        return object_name