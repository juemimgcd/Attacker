from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    app_name: str = "attacker"
    app_env: str = "local"
    debug: bool = True
    api_prefix: str = ""


class LogSettings(BaseSettings):
    log_level: str = "INFO"
    log_dir: str = "logs"
    log_rotation: str = "10 MB"
    log_retention: str = "14 days"


class DuckDBSettings(BaseSettings):
    database_path: str = "data/attacker.duckdb"
    read_only: bool = False


class ParquetSettings(BaseSettings):
    evidence_dir: str = "data/evidence"


class MinIOSettings(BaseSettings):
    endpoint: str = "localhost:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    bucket: str = "attacker"
    secure: bool = False


class QdrantSettings(BaseSettings):
    url: str = "http://localhost:6333"
    api_key: str | None = None
    collection_attack_cases: str = "attack_cases"
    collection_findings: str = "findings"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app: AppSettings = Field(default_factory=AppSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    duckdb: DuckDBSettings = Field(default_factory=DuckDBSettings)
    parquet: ParquetSettings = Field(default_factory=ParquetSettings)
    minio: MinIOSettings = Field(default_factory=MinIOSettings)
    qdrant: QdrantSettings = Field(default_factory=QdrantSettings)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()