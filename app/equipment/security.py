from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
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


def summarize(value: Any, *, max_chars: int = 4096) -> dict[str, Any]:
    redacted = redact(value)
    encoded = json.dumps(redacted, sort_keys=True, default=str)
    if len(encoded) <= max_chars:
        return {"value": redacted, "truncated": False}
    return {"preview": encoded[:max_chars], "truncated": True, "original_chars": len(encoded)}


class SecretLease:
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
        self._values.clear()


class SecretBroker:
    def __init__(self, backend: Mapping[str, str] | None = None) -> None:
        self._backend = dict(backend or {})

    @contextmanager
    def lease(self, secret_refs: Mapping[str, str]) -> Iterator[SecretLease]:
        resolved: dict[str, str] = {}
        for name, reference in secret_refs.items():
            if reference.startswith("env:"):
                environment_name = reference.removeprefix("env:")
                if environment_name not in os.environ:
                    raise LookupError(f"secret environment reference {reference} is unavailable")
                resolved[name] = os.environ[environment_name]
            elif reference not in self._backend:
                raise LookupError(f"secret reference {reference} is unavailable")
            else:
                resolved[name] = self._backend[reference]
        lease = SecretLease(resolved)
        try:
            yield lease
        finally:
            lease.close()
