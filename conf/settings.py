from functools import lru_cache

from pydantic import Field, SecretStr
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


# 定义 SQLAlchemy Async 与 SQLite 连接配置。
class DatabaseSettings(BaseSettings):
    url: str = "sqlite+aiosqlite:///data/attacker.sqlite3"
    echo: bool = False


# 定义 LangGraph checkpoint 的独立 SQLite 路径。
class CheckpointSettings(BaseSettings):
    database_path: str = "data/langgraph_checkpoints.sqlite3"


# 定义 API 控制面的可选访问密钥。
class SecuritySettings(BaseSettings):
    api_key: SecretStr | None = None


class EquipmentSettings(BaseSettings):
    root: str = "equipment"
    contracts_root: str = "contracts"
    workspace_root: str = "data/equipment-workspaces"
    archive_root: str = "data/equipment-archive"
    allow_executable_packages: bool = True
    require_checksum: bool = True
    allow_untrusted: bool = False
    require_signature: bool = False
    trust_roots_file: str = "conf/equipment_trust_roots.json"
    max_package_files: int = Field(default=500, gt=0)
    max_package_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    max_archive_depth: int = Field(default=8, gt=0)
    max_stdout_bytes: int = Field(default=1_048_576, gt=0)
    max_stderr_bytes: int = Field(default=262_144, gt=0)
    sandbox_image: str | None = None


# 定义单个模型职责的独立配置、预算和输入限制。
class ModelRoleSettings(BaseSettings):
    model_id: str = "unconfigured"
    provider_id: str = "unconfigured"
    max_calls: int = Field(default=0, ge=0)
    max_input_tokens: int = Field(default=4096, gt=0)
    max_output_tokens: int = Field(default=512, gt=0)
    max_cost: float | None = Field(default=None, ge=0)
    timeout_seconds: float = Field(default=30, gt=0)
    temperature: float = Field(default=0, ge=0)


# Planner 与 Model Judge 不共享模型配置或预算。
class AgentModelSettings(BaseSettings):
    planner: ModelRoleSettings = Field(default_factory=ModelRoleSettings)
    model_judge: ModelRoleSettings = Field(default_factory=ModelRoleSettings)


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
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    equipment: EquipmentSettings = Field(default_factory=EquipmentSettings)
    agent_models: AgentModelSettings = Field(default_factory=AgentModelSettings)


# 返回缓存后的全局配置对象，避免重复解析配置来源。
@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
