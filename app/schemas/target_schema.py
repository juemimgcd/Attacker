"""外部 Target 的 HTTP、鉴权和运行时凭据配置。"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, Field, HttpUrl, model_validator

from app.equipment.security import (
    contains_private_key_pem,
    is_sensitive_field,
    is_sensitive_header_key,
    validate_url_has_no_secrets,
)

DEFAULT_REFUSAL_STATUS_CODES = (403,)


# 定义目标 Agent 调用支持的 HTTP 方法。
class HTTPMethod(str, Enum):
    post = "POST"


# 定义目标 Agent 的鉴权方式和鉴权头配置。
class TargetAuth(BaseModel):
    type: str = "none"
    token: str | None = None
    header_name: str = "Authorization"
    token_prefix: str = "Bearer"


# 定义发送给目标 Agent 的请求体模板。
class TargetRequestTemplate(BaseModel):
    body_template: dict = Field(
        default_factory=lambda: {
            "messages": [
                {
                    "role": "user",
                    "content": "{prompt}",
                }
            ]
        }
    )


# 定义目标 Agent 的完整 HTTP 接入配置。
class TargetConfig(BaseModel):
    name: str
    endpoint: HttpUrl
    method: HTTPMethod = HTTPMethod.post
    headers: dict[str, str] = Field(default_factory=dict)
    auth: TargetAuth = Field(default_factory=TargetAuth)
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    allow_public_target: bool = False
    provider_instance_id: str | None = Field(
        default=None,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$",
    )
    provider_package_checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider_config_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider_secret_binding_revision: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    refusal_status_codes: tuple[
        Annotated[int, Field(ge=400, le=499)],
        ...,
    ] = DEFAULT_REFUSAL_STATUS_CODES
    request_template: TargetRequestTemplate = Field(default_factory=TargetRequestTemplate)

    @model_validator(mode="after")
    def reject_unstructured_credentials(self) -> TargetConfig:
        """Keep ad-hoc credentials in ``auth`` and out of persisted headers/templates."""

        for name, value in self.headers.items():
            if value and is_sensitive_header_key(name):
                raise ValueError(f"Target header {name} cannot carry credentials; use target.auth")
        self._reject_secret_template_value(
            self.request_template.body_template,
            "request_template.body_template",
        )
        return self

    @classmethod
    def _reject_secret_template_value(cls, value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                current = f"{path}.{key}"
                if is_sensitive_field(key, nested) and nested not in (None, "", {}, []):
                    raise ValueError(
                        f"Target request template cannot persist credentials at {current}; "
                        "use target.auth"
                    )
                cls._reject_secret_template_value(nested, current)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                cls._reject_secret_template_value(nested, f"{path}[{index}]")
        elif isinstance(value, str):
            if contains_private_key_pem(value):
                raise ValueError(
                    f"Target request template cannot persist private-key PEM material at {path}"
                )
            validate_url_has_no_secrets(value, label=f"Target {path}")
