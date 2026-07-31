"""持久 Run Job 的请求、状态和租约视图；持久 payload 禁止携带明文 Secret。"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

SENSITIVE_JOB_KEY = re.compile(
    r"(authorization|credential|password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)",
    re.IGNORECASE,
)


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

        def walk(value: Any, path: str) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    current = f"{path}.{key}" if path else str(key)
                    if SENSITIVE_JOB_KEY.search(str(key)) and nested not in (None, "", {}):
                        raise ValueError(
                            f"durable job payload cannot persist secret field {current}; "
                            "use a Provider Instance secret reference"
                        )
                    walk(nested, current)
            elif isinstance(value, list):
                for index, nested in enumerate(value):
                    walk(nested, f"{path}[{index}]")

        walk(self.payload, "")
        return self


class RunJobView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
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
