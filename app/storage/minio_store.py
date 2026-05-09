import asyncio

from app.schemas.evidence_schema import ArtifactRef
from conf.storage_conf import get_minio_config


# 封装 MinIO 对象存储的上传边界。
class MinIOStore:
    # 初始化 MinIO 存储对象并读取对象存储配置。
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or get_minio_config()

    # 异步上传证据制品并返回对象存储引用。
    async def upload_artifact(
        self,
        object_name: str,
        file_path: str,
        artifact_type: str,
    ) -> ArtifactRef:
        object_key = await asyncio.to_thread(
            self._upload_artifact_sync,
            object_name,
            file_path,
        )
        return ArtifactRef(
            object_key=object_key,
            artifact_type=artifact_type,
            storage_backend="minio",
        )

    # 在线程中同步执行证据制品上传占位逻辑。
    def _upload_artifact_sync(self, object_name: str, file_path: str) -> str:
        # Day 3 只保留边界。真实上传可以在 MinIO 服务接入后替换这里。
        return object_name
