"""分层应用配置与生产安全门禁；生产配置不安全时在启动阶段直接失败。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
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
    structured_json: bool = False


# 定义 SQLAlchemy Async 连接池与模式初始化配置。
class DatabaseSettings(BaseSettings):
    url: str = "sqlite+aiosqlite:///data/attacker.sqlite3"
    echo: bool = False
    auto_create_schema: bool = True
    pool_size: int = Field(default=10, ge=1, le=100)
    max_overflow: int = Field(default=20, ge=0, le=200)
    pool_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    pool_recycle_seconds: int = Field(default=1_800, ge=60, le=86_400)
    pool_pre_ping: bool = True
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=60)


# 定义 LangGraph checkpoint；本地默认 SQLite，生产使用 PostgreSQL URL。
class CheckpointSettings(BaseSettings):
    database_path: str = "data/langgraph_checkpoints.sqlite3"
    url: str | None = None

    @property
    def connection_string(self) -> str:
        return self.url or self.database_path


# 定义 API 控制面的可选访问密钥。
class SecuritySettings(BaseSettings):
    api_key: SecretStr | None = None
    metrics_api_key: SecretStr | None = None


class WorkerSettings(BaseSettings):
    enabled: bool = True
    concurrency: int = Field(default=2, ge=1, le=32)
    poll_seconds: float = Field(default=1.0, ge=0.1, le=60)
    lease_seconds: int = Field(default=120, ge=30, le=3_600)
    heartbeat_seconds: int = Field(default=20, ge=5, le=300)
    max_attempts: int = Field(default=3, ge=1, le=20)
    shutdown_grace_seconds: int = Field(default=60, ge=5, le=600)

    @model_validator(mode="after")
    def validate_heartbeat(self) -> "WorkerSettings":
        if self.heartbeat_seconds * 2 >= self.lease_seconds:
            raise ValueError("worker heartbeat must be less than half of the lease duration")
        return self


class SecretSettings(BaseSettings):
    backend: Literal["environment", "file", "vault"] = "environment"
    allow_environment_references: bool = True
    file_root: str = "run/secrets"
    vault_address: str | None = None
    vault_mount: str = "secret"
    vault_namespace: str | None = None
    vault_token_env: str = "VAULT_TOKEN"
    vault_token_file: str | None = None
    vault_timeout_seconds: float = Field(default=5.0, gt=0, le=30)


class ObservabilitySettings(BaseSettings):
    metrics_enabled: bool = True
    tracing_enabled: bool = False
    otlp_endpoint: str | None = None
    otlp_headers: str | None = None
    service_name: str = "attacker"
    deployment_environment: str = "local"
    request_id_header: str = "X-Request-ID"


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
    revocations_file: str = "conf/equipment_revocations.json"
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
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    secrets: SecretSettings = Field(default_factory=SecretSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    equipment: EquipmentSettings = Field(default_factory=EquipmentSettings)
    agent_models: AgentModelSettings = Field(default_factory=AgentModelSettings)

    @model_validator(mode="after")
    def validate_selected_profile(self) -> "Settings":
        """仅对生产环境启用不可降级的配置组合校验。"""

        if self.app.app_env.lower() in {"production", "prod"}:
            self.validate_production()
        return self

    def validate_production(self) -> None:
        """聚合全部生产配置问题后一次性拒绝启动，便于部署阶段修正。"""

        failures: list[str] = []
        database_url = self.database.url.lower()
        checkpoint_url = self.checkpoint.connection_string.lower()
        api_key = self.security.api_key
        metrics_key = self.security.metrics_api_key

        if self.app.debug:
            failures.append("APP__DEBUG must be false")
        if not database_url.startswith("postgresql+asyncpg://"):
            failures.append("DATABASE__URL must use PostgreSQL with the asyncpg driver")
        if self.database.auto_create_schema:
            failures.append("DATABASE__AUTO_CREATE_SCHEMA must be false; use Alembic")
        if not checkpoint_url.startswith(("postgres://", "postgresql://")):
            failures.append("CHECKPOINT__URL must use PostgreSQL")
        if api_key is None or len(api_key.get_secret_value()) < 32:
            failures.append("SECURITY__API_KEY must contain at least 32 characters")
        if self.observability.metrics_enabled and (
            metrics_key is None or len(metrics_key.get_secret_value()) < 32
        ):
            failures.append("SECURITY__METRICS_API_KEY must contain at least 32 characters")
        if not self.log.structured_json:
            failures.append("LOG__STRUCTURED_JSON must be true")
        if not self.equipment.require_checksum:
            failures.append("EQUIPMENT__REQUIRE_CHECKSUM must be true")
        if not self.equipment.require_signature:
            failures.append("EQUIPMENT__REQUIRE_SIGNATURE must be true")
        if self.equipment.allow_untrusted and not self.equipment.sandbox_image:
            failures.append("EQUIPMENT__SANDBOX_IMAGE is required for untrusted packages")
        if self.secrets.backend == "environment":
            failures.append("SECRETS__BACKEND must be file or vault")
        if self.secrets.allow_environment_references:
            failures.append("SECRETS__ALLOW_ENVIRONMENT_REFERENCES must be false")
        if self.secrets.backend == "vault":
            address = (self.secrets.vault_address or "").lower()
            if not address.startswith("https://"):
                failures.append("SECRETS__VAULT_ADDRESS must use HTTPS")
        if self.secrets.backend == "file" and not self.secrets.file_root.strip():
            failures.append("SECRETS__FILE_ROOT must be configured")

        for name, value in (
            ("EQUIPMENT__TRUST_ROOTS_FILE", self.equipment.trust_roots_file),
            ("EQUIPMENT__REVOCATIONS_FILE", self.equipment.revocations_file),
        ):
            path = Path(value)
            if not path.is_file():
                failures.append(f"{name} must reference a mounted regular file")

        if failures:
            raise ValueError("unsafe production configuration: " + "; ".join(failures))


# 返回缓存后的全局配置对象，避免重复解析配置来源。
@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
