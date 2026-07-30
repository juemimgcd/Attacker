from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol

from app.schemas.equipment_schema import (
    ProviderContext,
    ProviderResult,
    ResourceLeaseDraft,
    SkillContext,
    SkillPreparation,
    SkillResult,
)

_PROVIDER_SECRETS: ContextVar[dict[str, str] | None] = ContextVar(
    "attacker_provider_secrets", default=None
)


def provider_secret(name: str) -> str:
    scoped = _PROVIDER_SECRETS.get() or {}
    if name in scoped:
        return scoped[name]
    environment_name = f"ATTACKER_SECRET_{name.upper()}"
    if environment_name in os.environ:
        return os.environ[environment_name]
    raise LookupError(f"secret {name} is not available in this Provider call")


@contextmanager
def provider_secret_scope(environment: Mapping[str, str]) -> Iterator[None]:
    prefix = "ATTACKER_SECRET_"
    values = {
        name.removeprefix(prefix).lower(): value
        for name, value in environment.items()
        if name.startswith(prefix)
    }
    token = _PROVIDER_SECRETS.set(values)
    try:
        yield
    finally:
        _PROVIDER_SECRETS.reset(token)


class ProviderAdapter(Protocol):
    async def describe(self) -> dict: ...

    async def validate_config(self, config: dict) -> dict: ...

    async def healthcheck(self, context: ProviderContext) -> dict: ...

    async def invoke(
        self,
        capability: str,
        payload: dict,
        context: ProviderContext,
    ) -> ProviderResult: ...

    async def cleanup(
        self,
        resource: ResourceLeaseDraft,
        context: ProviderContext,
    ) -> dict: ...


class Skill(Protocol):
    async def prepare(self, context: SkillContext) -> SkillPreparation: ...

    async def execute(self, payload: dict, context: SkillContext) -> SkillResult: ...


__all__ = [
    "ProviderAdapter",
    "ProviderContext",
    "ProviderResult",
    "Skill",
    "SkillContext",
    "SkillPreparation",
    "SkillResult",
    "provider_secret",
]
