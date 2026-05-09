from pathlib import Path

from conf.settings import settings


# 返回 DuckDB 数据库文件路径。
def get_duckdb_path() -> Path:
    return Path(settings.duckdb.database_path)


# 返回 Parquet 证据归档目录路径。
def get_parquet_evidence_dir() -> Path:
    return Path(settings.parquet.evidence_dir)


# 返回 MinIO 对象存储连接配置。
def get_minio_config() -> dict:
    return {
        "endpoint": settings.minio.endpoint,
        "access_key": settings.minio.access_key,
        "secret_key": settings.minio.secret_key,
        "secure": settings.minio.secure,
        "bucket": settings.minio.bucket,
    }


# 返回 Qdrant 向量库连接和集合配置。
def get_qdrant_config() -> dict:
    return {
        "url": settings.qdrant.url,
        "api_key": settings.qdrant.api_key,
        "collection_attack_cases": settings.qdrant.collection_attack_cases,
        "collection_findings": settings.qdrant.collection_findings,
    }
