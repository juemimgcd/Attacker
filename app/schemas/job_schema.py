"""持久 Run Job 的请求、状态和租约视图；持久 payload 禁止携带明文 Secret。"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.equipment.security import (
    contains_private_key_pem,
    is_sensitive_field,
    is_sensitive_header_key,
    is_sensitive_key,
    normalize_sensitive_key,
)

NON_SECRET_JOB_CHECKSUM_KEYS = {"provider_secret_binding_revision"}
NON_SECRET_JOB_METADATA_KEYS = {"token_prefix"}


class JobKind(str, Enum):
    deterministic = "deterministic"
    adaptive = "adaptive"
    deterministic_graybox = "deterministic_graybox"
    stateful = "stateful"


class JobStatus(str, Enum):
    queued = "queued"
    leased = "leased"
    running = "running"
    retry_wait = "retry_wait"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class RunJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str = Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    kind: JobKind
    payload: dict[str, Any]
    priority: int = Field(default=100, ge=0, le=1_000)
    max_attempts: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def reject_persisted_secrets(self) -> RunJobCreate:
        """递归拒绝常见 Secret 字段，避免凭据随重试任务长期落库。"""

        def reject_url_secrets(value: str, path: str) -> None:
            try:
                parsed = urlsplit(value)
            except ValueError:
                return
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return
            if parsed.username is not None or parsed.password is not None:
                raise ValueError(
                    f"durable job payload cannot persist URL credentials at {path}; "
                    "use a Provider Instance secret reference"
                )
            for component_name, component in (
                ("query", parsed.query),
                ("fragment", parsed.fragment),
            ):
                sensitive_key = next(
                    (
                        key
                        for key, nested in parse_qsl(component, keep_blank_values=True)
                        if nested and is_sensitive_key(key, url_query=True)
                    ),
                    None,
                )
                if sensitive_key is not None:
                    raise ValueError(
                        f"durable job payload cannot persist secret URL {component_name} "
                        f"parameter {sensitive_key} at {path}; use a Provider Instance "
                        "secret reference"
                    )

        def walk(value: Any, path: str, *, in_headers: bool = False) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    current = f"{path}.{key}" if path else str(key)
                    normalized_key = normalize_sensitive_key(key)
                    checksum_metadata = (
                        normalized_key in NON_SECRET_JOB_CHECKSUM_KEYS
                        and isinstance(nested, str)
                        and re.fullmatch(r"[0-9a-f]{64}", nested) is not None
                    )
                    ordinary_metadata = normalized_key in NON_SECRET_JOB_METADATA_KEYS
                    sensitive_field = (
                        is_sensitive_header_key(key)
                        if in_headers
                        else is_sensitive_field(key, nested)
                    )
                    if (
                        not checksum_metadata
                        and not ordinary_metadata
                        and sensitive_field
                        and nested not in (None, "", {})
                    ):
                        raise ValueError(
                            f"durable job payload cannot persist secret field {current}; "
                            "use a Provider Instance secret reference"
                        )
                    walk(
                        nested,
                        current,
                        in_headers=normalized_key == "headers",
                    )
            elif isinstance(value, list):
                for index, nested in enumerate(value):
                    walk(nested, f"{path}[{index}]", in_headers=in_headers)
            elif isinstance(value, str):
                if contains_private_key_pem(value):
                    raise ValueError(
                        f"durable job payload cannot persist private-key PEM material at {path}; "
                        "use a Provider Instance secret reference"
                    )
                reject_url_secrets(value, path)

        walk(self.payload, "")
        return self


class RunJobView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str | None = None
    request_id: str
    kind: JobKind
    status: JobStatus
    priority: int
    attempts: int
    max_attempts: int
    available_at: datetime
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_summary: str | None = None
    cancel_requested: bool
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
