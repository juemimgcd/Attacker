from pathlib import Path

from conf.settings import settings


def get_duckdb_path() -> Path:
    return Path(settings.duckdb.database_path)


def get_parquet_evidence_dir() -> Path:
    return Path(settings.parquet.evidence_dir)


def get_minio_config() -> dict:
    return {
        "endpoint": settings.minio.endpoint,
        "access_key": settings.minio.access_key,
        "secret_key": settings.minio.secret_key,
        "secure": settings.minio.secure,
        "bucket": settings.minio.bucket,
    }


def get_qdrant_config() -> dict:
    return {
        "url": settings.qdrant.url,
        "api_key": settings.qdrant.api_key,
        "collection_attack_cases": settings.qdrant.collection_attack_cases,
        "collection_findings": settings.qdrant.collection_findings,
    }