"""受限文件与 Vault KV v2 Secret Resolver，并组装短生命周期 SecretBroker。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.equipment.security import SecretBroker, SecretResolver
from conf.settings import SecretSettings

MAX_SECRET_BYTES = 64 * 1024


class FileSecretResolver:
    """只允许挂载根目录内的相对普通文件，拒绝链接、逃逸和超大 Secret。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    async def resolve(self, reference: str) -> str:
        if not reference.startswith("file:"):
            raise LookupError("secret reference is not supported by the mounted-file backend")
        relative = Path(reference.removeprefix("file:"))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("mounted secret reference must be a safe relative path")
        root = self.root.resolve()
        candidate = self.root / relative
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("mounted secret reference cannot contain symbolic links")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise LookupError("mounted secret reference is unavailable")
        if resolved.stat().st_size > MAX_SECRET_BYTES:
            raise ValueError("mounted secret exceeds the maximum allowed size")
        value = await asyncio.to_thread(resolved.read_text, encoding="utf-8")
        value = value.rstrip("\r\n")
        if not value:
            raise ValueError("mounted secret is empty")
        return value


class VaultKvV2SecretResolver:
    """通过 HTTPS 读取 Vault KV v2；Token 自身不会进入业务快照。"""

    def __init__(
        self,
        *,
        address: str,
        mount: str,
        namespace: str | None,
        token_env: str,
        token_file: str | None,
        timeout_seconds: float,
        file_resolver: FileSecretResolver,
    ) -> None:
        if not address.lower().startswith("https://"):
            raise ValueError("Vault address must use HTTPS")
        self.address = address.rstrip("/")
        self.mount = self._safe_segment(mount)
        self.namespace = namespace
        self.token_env = token_env
        self.token_file = token_file
        self.timeout_seconds = timeout_seconds
        self.file_resolver = file_resolver

    async def resolve(self, reference: str) -> str:
        path, field = self._parse_reference(reference)
        token = await self._token()
        headers = {"X-Vault-Token": token}
        if self.namespace:
            headers["X-Vault-Namespace"] = self.namespace
        encoded_path = "/".join(quote(segment, safe="") for segment in path.split("/"))
        url = f"{self.address}/v1/{quote(self.mount, safe='')}/data/{encoded_path}"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.get(url, headers=headers)
            if response.status_code != 200:
                raise LookupError(f"Vault secret is unavailable (HTTP {response.status_code})")
            body: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LookupError(f"Vault secret resolution failed ({type(exc).__name__})") from None
        data = body.get("data", {}).get("data", {}) if isinstance(body, dict) else {}
        value = data.get(field) if isinstance(data, dict) else None
        if not isinstance(value, str) or not value:
            raise LookupError("Vault secret field is unavailable")
        if len(value.encode()) > MAX_SECRET_BYTES:
            raise ValueError("Vault secret exceeds the maximum allowed size")
        return value

    async def _token(self) -> str:
        value = os.environ.get(self.token_env, "")
        if not value and self.token_file:
            token_reference = (
                self.token_file
                if self.token_file.startswith("file:")
                else f"file:{self.token_file}"
            )
            value = await self.file_resolver.resolve(token_reference)
        if not value:
            raise LookupError("Vault token is unavailable")
        return value

    @staticmethod
    def _parse_reference(reference: str) -> tuple[str, str]:
        if not reference.startswith("vault:"):
            raise LookupError("secret reference is not supported by the Vault backend")
        raw = reference.removeprefix("vault:")
        path, separator, field = raw.partition("#")
        parts = path.split("/")
        if (
            separator != "#"
            or not field
            or not path
            or path.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("Vault reference must use vault:path/to/secret#field")
        return path, field

    @staticmethod
    def _safe_segment(value: str) -> str:
        if not value or "/" in value or value in {".", ".."}:
            raise ValueError("Vault mount must be one safe path segment")
        return value


class CompositeSecretResolver:
    """按显式 scheme 路由引用，不猜测或回退到其他 Secret 来源。"""

    def __init__(self, resolvers: dict[str, SecretResolver]) -> None:
        self.resolvers = resolvers

    async def resolve(self, reference: str) -> str:
        scheme, separator, _ = reference.partition(":")
        if separator != ":" or scheme not in self.resolvers:
            raise LookupError("secret reference scheme is unavailable")
        return await self.resolvers[scheme].resolve(reference)


def build_secret_broker(config: SecretSettings) -> SecretBroker:
    file_resolver = FileSecretResolver(config.file_root)
    resolvers: dict[str, SecretResolver] = {}
    if config.backend in {"file", "vault"}:
        resolvers["file"] = file_resolver
    if config.backend == "vault":
        if not config.vault_address:
            raise ValueError("Vault backend requires SECRETS__VAULT_ADDRESS")
        resolvers["vault"] = VaultKvV2SecretResolver(
            address=config.vault_address,
            mount=config.vault_mount,
            namespace=config.vault_namespace,
            token_env=config.vault_token_env,
            token_file=config.vault_token_file,
            timeout_seconds=config.vault_timeout_seconds,
            file_resolver=file_resolver,
        )
    resolver = CompositeSecretResolver(resolvers) if resolvers else None
    return SecretBroker(
        resolver=resolver,
        allow_environment_references=config.allow_environment_references,
    )
