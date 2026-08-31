"""装备工作区、出站地址、Secret 租约、脱敏和摘要安全原语。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import ParseResult, parse_qsl, urlparse

import httpx

_EXACT_SENSITIVE_KEYS = {
    "api_key",
    "auth",
    "authorization",
    "client_key",
    "cookie",
    "credential",
    "password",
    "passphrase",
    "private_key",
    "secret",
    "set_cookie",
    "sig",
    "signature",
    "ssh_key",
    "token",
}
_SENSITIVE_SEGMENTS = {
    "auth",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passphrase",
    "secret",
    "sig",
    "signature",
}
_PRIVATE_KEY_PEM_MARKER = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----",
    re.IGNORECASE,
)
_PRIVATE_KEY_PEM_BLOCK = re.compile(
    r"-----BEGIN (?P<label>(?:[A-Z0-9]+ )*PRIVATE KEY)-----.*?"
    r"(?:-----END (?P=label)-----|\Z)",
    re.IGNORECASE | re.DOTALL,
)
BLOCKED_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("fd00:ec2::254"),
}

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def normalize_sensitive_key(key: object) -> str:
    """Normalize snake, kebab, camel and acronym CamelCase names to snake_case."""

    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", str(key))
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).replace("-", "_").casefold()


def is_sensitive_key(
    key: object,
    extra_keys: Iterable[str] = (),
    *,
    url_query: bool = False,
) -> bool:
    """Recognize credential-bearing field names without treating plural token counts as secrets."""

    normalized = normalize_sensitive_key(key)
    compact = normalized.replace("_", "")
    segments = set(normalized.split("_"))
    normalized_extras = {normalize_sensitive_key(candidate) for candidate in extra_keys}
    if url_query and (
        normalized
        in {
            "auth",
            "key",
            "session",
            "session_id",
            "sig",
            "signature",
            "token_prefix",
        }
        or normalized.endswith("_session_id")
    ):
        return True
    if normalized == "token_prefix":
        return False
    if (
        normalized in _EXACT_SENSITIVE_KEYS
        or bool(segments & _SENSITIVE_SEGMENTS)
        or "token" in segments
        or compact.endswith(("token", "tokenvalue"))
        or normalized.endswith(
            ("_api_key", "_private_key", "_client_key", "_ssh_key", "_set_cookie")
        )
    ):
        return True
    return any(
        normalized == candidate or normalized.endswith(f"_{candidate}")
        for candidate in normalized_extras
    )


def is_sensitive_field(
    key: object,
    value: object,
    extra_keys: Iterable[str] = (),
) -> bool:
    """Apply structured-payload semantics, recursing through an ``auth`` container."""

    if normalize_sensitive_key(key) == "auth" and isinstance(value, Mapping):
        return False
    return is_sensitive_key(key, extra_keys)


def is_sensitive_header_key(key: object) -> bool:
    """Recognize credential-like HTTP headers, including session identifiers."""

    normalized = normalize_sensitive_key(key)
    return (
        normalized in {"session", "session_id"}
        or normalized.endswith("_session_id")
        or is_sensitive_key(key)
    )


def contains_private_key_pem(value: str) -> bool:
    """Return whether a string contains a private-key PEM marker (not public certs)."""

    return _PRIVATE_KEY_PEM_MARKER.search(value) is not None


def redact_private_key_pem(value: str) -> str:
    """Remove complete or unterminated private-key PEM blocks from persisted text."""

    return _PRIVATE_KEY_PEM_BLOCK.sub("[REDACTED PRIVATE KEY]", value)


class HTTPResponsePolicyError(ValueError):
    """The upstream response cannot be consumed within the configured boundary."""


class HTTPResponseTooLargeError(HTTPResponsePolicyError):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"HTTP response exceeded {limit} bytes")


def validate_url_has_no_secrets(url: str, *, label: str) -> None:
    """拒绝会把明文凭据嵌入配置、日志或请求目标的 URL 形式。"""

    _reject_embedded_url_secrets(urlparse(url), label=label)


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


def validate_outbound_url(url: str, allowed_hosts: list[str]) -> tuple[str, ...]:
    """同步校验 allowlist；地址由发送路径在线程中解析并固定。"""

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Provider URL must be http(s) with a host")
    _reject_embedded_url_secrets(parsed, label="Provider")
    hostname = parsed.hostname.rstrip(".").lower()
    normalized_allowed = {host.rstrip(".").lower() for host in allowed_hosts}
    if hostname not in normalized_allowed:
        raise ValueError(f"host {hostname} is not in the Provider Instance allowlist")
    return ()


def validate_target_url(
    url: str,
    *,
    allow_public_target: bool,
    allowed_hosts: list[str],
) -> str:
    """允许本地开发 Target；公网 Target 必须同时通过服务端 Instance allowlist。"""

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("target endpoint must be http(s) with a host")
    _reject_embedded_url_secrets(parsed, label="target")
    hostname = parsed.hostname.rstrip(".").lower()
    if allowed_hosts:
        validate_outbound_url(url, allowed_hosts)
    if hostname == "localhost":
        return hostname
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None:
        _validate_address(
            address,
            allow_loopback=True,
            error=f"target address {hostname} is prohibited",
        )
        if _is_private_or_loopback(address):
            return hostname
    if not allow_public_target:
        raise ValueError("public targets require allow_public_target=true")
    if not allowed_hosts:
        raise ValueError("public targets require an enabled Provider Instance allowlist")
    return hostname


def pin_http_request(
    request: httpx.Request,
    *,
    address: str,
) -> httpx.Request:
    """保留原 Host/SNI，并让连接使用刚通过策略校验的数字地址。"""

    host = request.url.host
    if not host:
        raise ValueError("outbound URL does not include a host")
    extensions = dict(request.extensions)
    if request.url.scheme == "https":
        extensions["sni_hostname"] = host
    headers = request.headers.copy()
    headers["host"] = httpx.Request("GET", request.url).headers["host"]
    headers["accept-encoding"] = "identity"
    return httpx.Request(
        method=request.method,
        url=request.url.copy_with(host=address),
        headers=headers,
        stream=request.stream,
        extensions=extensions,
    )


async def send_pinned_request(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    local_only: bool = False,
    public_only: bool = False,
    addresses: str | Sequence[str] | None = None,
    stream: bool = False,
) -> httpx.Response:
    """依次尝试已验证地址；每次连接都不再触发目标域名解析。"""

    if local_only and public_only:
        raise ValueError("outbound request cannot be both local-only and public-only")
    loop = asyncio.get_running_loop()
    timeout = request.extensions.get("timeout", {})
    timeout_seconds = timeout.get("connect", 5.0) if isinstance(timeout, dict) else 5.0
    deadline = loop.time() + float(5.0 if timeout_seconds is None else timeout_seconds)
    try:
        supplied = (addresses,) if isinstance(addresses, str) else tuple(addresses or ())
        if supplied:
            candidates = tuple(ipaddress.ip_address(address) for address in supplied)
            for address in candidates:
                _validate_address(
                    address,
                    allow_loopback=local_only,
                    error=f"pinned address {address} is prohibited",
                )
                if local_only and not _is_private_or_loopback(address):
                    raise ValueError(f"pinned address {address} is not local")
                if public_only and not _is_global(address):
                    raise ValueError(f"pinned address {address} is not public")
        else:
            candidates = await asyncio.wait_for(
                asyncio.to_thread(
                    _resolve_http_addresses,
                    str(request.url),
                    local_only=local_only,
                    public_only=public_only,
                ),
                timeout=max(0.0, deadline - loop.time()),
            )
    except TimeoutError as exc:
        raise httpx.ConnectTimeout("outbound request deadline exceeded", request=request) from exc
    except ValueError as exc:
        raise httpx.ConnectError(str(exc), request=request) from exc

    last_error: httpx.RequestError | None = None
    for address in candidates:
        try:
            return await asyncio.wait_for(
                client.send(
                    pin_http_request(request, address=str(address)),
                    stream=stream,
                    follow_redirects=False,
                ),
                timeout=max(0.0, deadline - loop.time()),
            )
        except TimeoutError as exc:
            raise httpx.ConnectTimeout(
                "outbound request deadline exceeded", request=request
            ) from exc
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise httpx.ConnectError("outbound host did not resolve to an address", request=request)


async def read_bounded_response(
    response: httpx.Response,
    max_response_bytes: int,
) -> bytes:
    """Reject encoded bodies and enforce the byte limit before buffering raw content."""

    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")
    body = bytearray()
    chunk_size = min(65_536, max_response_bytes + 1)
    try:
        content_encoding = response.headers.get("content-encoding")
        if content_encoding is not None and content_encoding.strip().lower() != "identity":
            raise HTTPResponsePolicyError("encoded HTTP responses are not accepted")
        async for chunk in response.aiter_raw(chunk_size=chunk_size):
            if len(body) + len(chunk) > max_response_bytes:
                raise HTTPResponseTooLargeError(max_response_bytes)
            body.extend(chunk)
    finally:
        await response.aclose()
    return bytes(body)


def buffered_identity_response(response: httpx.Response, content: bytes) -> httpx.Response:
    """Rebuild an identity response without stale wire-length metadata."""

    headers = response.headers.copy()
    headers.pop("content-encoding", None)
    headers.pop("content-length", None)
    return httpx.Response(
        status_code=response.status_code,
        headers=headers,
        content=content,
        request=response.request,
    )


def _resolve_http_addresses(
    url: str,
    *,
    local_only: bool,
    public_only: bool,
) -> tuple[IPAddress, ...]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("outbound URL must be http(s) with a host")
    hostname = parsed.hostname.rstrip(".").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        resolved = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"host {hostname} could not be resolved") from exc

    addresses: list[IPAddress] = []
    for result in resolved:
        address = ipaddress.ip_address(result[4][0])
        _validate_address(
            address,
            allow_loopback=local_only,
            error=f"resolved address for {hostname} is prohibited",
        )
        if local_only and not _is_private_or_loopback(address):
            raise ValueError(f"resolved address for {hostname} is not local")
        if public_only and not _is_global(address):
            raise ValueError(f"resolved address for {hostname} is not public")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValueError(f"host {hostname} did not resolve to an address")
    return tuple(addresses)


def _reject_embedded_url_secrets(parsed: ParseResult, *, label: str) -> None:
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label} URL credentials are not allowed; use a Secret reference")
    for component in (parsed.query, parsed.fragment):
        if any(
            value and (is_sensitive_key(key, url_query=True)) for key, value in parse_qsl(component)
        ):
            raise ValueError(
                f"{label} URL secret parameters are not allowed; use a Secret reference"
            )


def _validate_address(address: IPAddress, *, allow_loopback: bool, error: str) -> None:
    for candidate in _address_variants(address):
        if (
            candidate in BLOCKED_METADATA_IPS
            or candidate.is_link_local
            or candidate.is_multicast
            or candidate.is_unspecified
            or (candidate.is_loopback and not allow_loopback)
        ):
            raise ValueError(error)


def _address_variants(address: IPAddress) -> tuple[IPAddress, ...]:
    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    return (address, mapped) if mapped is not None else (address,)


def _is_private_or_loopback(address: IPAddress) -> bool:
    return any(
        candidate.is_private or candidate.is_loopback for candidate in _address_variants(address)
    )


def _is_global(address: IPAddress) -> bool:
    return all(candidate.is_global for candidate in _address_variants(address))


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if is_sensitive_field(key, item) else redact(item, secrets))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        result = redact_private_key_pem(value)
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
                if is_sensitive_field(key, nested):
                    collect_strings(nested)
                else:
                    walk(nested)
        elif isinstance(item, list):
            for nested in item:
                walk(nested)
        elif isinstance(item, str) and contains_private_key_pem(item):
            collected.add(item)

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
        if any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None for name in values):
            raise ValueError("secret names must be ASCII environment-safe identifiers")
        normalized_names = [name.casefold() for name in values]
        if len(normalized_names) != len(set(normalized_names)):
            raise ValueError("secret names must be unique ignoring case")
        self._values = {name.casefold(): value for name, value in values.items()}

    def provider_environment(self, names: list[str]) -> dict[str, str]:
        unknown = {name for name in names if name.casefold() not in self._values}
        if unknown:
            raise ValueError(f"undeclared secret names: {sorted(unknown)}")
        return {f"ATTACKER_SECRET_{name.upper()}": self._values[name.casefold()] for name in names}

    def value(self, name: str) -> str:
        """让 Core 在租约作用域内读取一个已声明 Secret。"""

        normalized_name = name.casefold()
        if normalized_name not in self._values:
            raise ValueError(f"undeclared secret name: {name}")
        return self._values[normalized_name]

    @property
    def redaction_values(self) -> tuple[str, ...]:
        return tuple(self._values.values())

    def close(self) -> None:
        for name in self._values:
            self._values[name] = ""
        self._values.clear()


class SecretResolver(Protocol):
    async def resolve(self, reference: str) -> str: ...


def validate_secret_reference(reference: str, *, allow_environment: bool = True) -> None:
    """Validate built-in reference syntax without resolving or exposing its value."""

    if not reference or any(character.isspace() for character in reference):
        raise ValueError("Secret reference must be a non-empty reference identifier")
    scheme, separator, payload = reference.partition(":")
    if separator != ":" or not payload:
        raise ValueError("Secret values must use a configured reference, not plaintext")
    if scheme == "env":
        if not allow_environment:
            raise PermissionError("environment secret references are disabled")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", payload) is None:
            raise ValueError("environment Secret reference name is invalid")
        return
    if scheme == "file":
        relative = Path(payload)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("mounted Secret reference must be a safe relative path")
        return
    if scheme == "vault":
        path, field_separator, field = payload.partition("#")
        parts = path.split("/")
        if (
            field_separator != "#"
            or not field
            or not path
            or path.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("Vault reference must use vault:path/to/secret#field")
        return
    raise ValueError(f"unsupported Secret reference scheme: {scheme}")


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

    def validate_reference(self, reference: str, *, require_configured: bool = False) -> None:
        """Prove that an Instance value names a configured reference before persistence."""

        if reference in self._backend:
            return
        if reference.startswith("env:"):
            validate_secret_reference(
                reference,
                allow_environment=self._allow_environment_references,
            )
            return
        scheme = reference.partition(":")[0]
        if scheme in {"file", "vault"}:
            validate_secret_reference(reference, allow_environment=False)
            supports_reference = getattr(self._resolver, "supports_reference", None)
            if callable(supports_reference) and supports_reference(reference):
                return
            if not require_configured:
                return
            raise ValueError(f"Secret reference scheme {scheme} is not configured")
        supports_reference = getattr(self._resolver, "supports_reference", None)
        if callable(supports_reference) and supports_reference(reference):
            return
        raise ValueError("Secret values must use a configured reference, not plaintext")

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
