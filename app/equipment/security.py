"""装备工作区、出站地址、Secret 租约、脱敏和摘要安全原语。"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

SENSITIVE_KEY = re.compile(
    r"(secret|token|password|authorization|credential|api[_-]?key)",
    re.IGNORECASE,
)
BLOCKED_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
}


def safe_workspace_path(workspace: Path, requested: str) -> Path:
    if Path(requested).is_absolute() or ".." in Path(requested).parts:
        raise ValueError("unsafe workspace path")
    root = workspace.resolve()
    target = (root / requested).resolve()
    if not target.is_relative_to(root):
        raise ValueError("workspace path escapes the current Run")
    if target.exists() and target.is_symlink():
        raise ValueError("symbolic workspace paths are not allowed")
    return target


def validate_outbound_url(url: str, allowed_hosts: list[str]) -> str:
    """同时校验主机 allowlist 与解析后的地址，阻断 metadata/本地地址 SSRF。"""

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Provider URL must be http(s) with a host")
    hostname = parsed.hostname.rstrip(".").lower()
    normalized_allowed = {host.rstrip(".").lower() for host in allowed_hosts}
    if hostname not in normalized_allowed:
        raise ValueError(f"host {hostname} is not in the Provider Instance allowlist")
    for result in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(result[4][0])
        if (
            address in BLOCKED_METADATA_IPS
            or address.is_link_local
            or address.is_loopback
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ValueError(f"resolved address for {hostname} is prohibited")
    return hostname


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if SENSITIVE_KEY.search(str(key)) else redact(item, secrets))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    return value


def sensitive_values(value: Any) -> tuple[str, ...]:
    collected: set[str] = set()

    def collect_strings(item: Any) -> None:
        if isinstance(item, dict):
            for nested in item.values():
                collect_strings(nested)
        elif isinstance(item, list):
            for nested in item:
                collect_strings(nested)
        elif isinstance(item, str) and item:
            collected.add(item)

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if SENSITIVE_KEY.search(str(key)):
                    collect_strings(nested)
                else:
                    walk(nested)
        elif isinstance(item, list):
            for nested in item:
                walk(nested)

    walk(value)
    return tuple(sorted(collected))


def summarize(value: Any, *, max_chars: int = 4096) -> dict[str, Any]:
    redacted = redact(value)
    encoded = json.dumps(redacted, sort_keys=True, default=str)
    if len(encoded) <= max_chars:
        return {"value": redacted, "truncated": False}
    return {"preview": encoded[:max_chars], "truncated": True, "original_chars": len(encoded)}


class SecretLease:
    """短生命周期明文视图；关闭后清空内存映射并禁止继续读取。"""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def provider_environment(self, names: list[str]) -> dict[str, str]:
        unknown = set(names) - self._values.keys()
        if unknown:
            raise ValueError(f"undeclared secret names: {sorted(unknown)}")
        return {f"ATTACKER_SECRET_{name.upper()}": self._values[name] for name in names}

    @property
    def redaction_values(self) -> tuple[str, ...]:
        return tuple(self._values.values())

    def close(self) -> None:
        for name in self._values:
            self._values[name] = ""
        self._values.clear()


class SecretResolver(Protocol):
    async def resolve(self, reference: str) -> str: ...


class SecretBroker:
    """按引用解析 Secret，并只在一次 Provider 调用作用域内出租。"""

    def __init__(
        self,
        backend: Mapping[str, str] | None = None,
        *,
        resolver: SecretResolver | None = None,
        allow_environment_references: bool = True,
    ) -> None:
        self._backend = dict(backend or {})
        self._resolver = resolver
        self._allow_environment_references = allow_environment_references

    @asynccontextmanager
    async def lease(self, secret_refs: Mapping[str, str]) -> AsyncIterator[SecretLease]:
        """解析引用并在 finally 中销毁租约，不把明文返回给 Skill 或持久层。"""

        resolved: dict[str, str] = {}
        lease: SecretLease | None = None
        try:
            for name, reference in secret_refs.items():
                if reference.startswith("env:"):
                    if not self._allow_environment_references:
                        raise PermissionError("environment secret references are disabled")
                    environment_name = reference.removeprefix("env:")
                    if environment_name not in os.environ:
                        raise LookupError(
                            f"secret environment reference {reference} is unavailable"
                        )
                    resolved[name] = os.environ[environment_name]
                elif reference in self._backend:
                    resolved[name] = self._backend[reference]
                elif self._resolver is not None:
                    resolved[name] = await self._resolver.resolve(reference)
                else:
                    raise LookupError(f"secret reference {reference} is unavailable")
            lease = SecretLease(resolved)
            resolved.clear()
            yield lease
        finally:
            for name in resolved:
                resolved[name] = ""
            resolved.clear()
            if lease is not None:
                lease.close()
