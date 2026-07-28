from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# 定义应用名称、运行环境、调试开关和接口前缀配置。
class AppSettings(BaseSettings):
    app_name: str = "attacker"
    app_env: str = "local"
    debug: bool = True
    api_prefix: str = ""


# 定义日志级别、目录、轮转和保留策略配置。
class LogSettings(BaseSettings):
    log_level: str = "INFO"
    log_dir: str = "logs"
    log_rotation: str = "10 MB"
    log_retention: str = "14 days"


# 定义 DuckDB 数据库路径和只读模式配置。
class DatabaseSettings(BaseSettings):
    url: str = "sqlite+aiosqlite:///data/attacker.sqlite3"
    echo: bool = False


class CheckpointSettings(BaseSettings):
    database_path: str = "data/langgraph_checkpoints.sqlite3"


# 定义 MinIO 对象存储连接和桶配置。
# 汇总所有子配置，并支持从 .env 和环境变量读取覆盖值。
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app: AppSettings = Field(default_factory=AppSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    checkpoint: CheckpointSettings = Field(default_factory=CheckpointSettings)


# 返回缓存后的全局配置对象，避免重复解析配置来源。
@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
