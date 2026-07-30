from __future__ import annotations

from copy import deepcopy
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from app.schemas.target_schema import TargetConfig
from app.services.prompt_governance import redact_sensitive_text

_REDACTED_KEYS = {
    "authorization",
    "token",
    "api_key",
    "secret",
    "secret_key",
    "password",
}


def canonical_target_binding(
    target: TargetConfig | dict[str, Any] | None,
) -> dict[str, Any] | None:
    if target is None:
        return None
    snapshot = (
        target.model_dump(mode="json") if isinstance(target, TargetConfig) else deepcopy(target)
    )
    if "endpoint" not in snapshot:
        snapshot.pop("name", None)
        return _redact(snapshot, set())

    secret_values = {
        value for value in snapshot.get("headers", {}).values() if isinstance(value, str) and value
    }
    auth = snapshot.get("auth")
    if isinstance(auth, dict):
        token = auth.get("token")
        if isinstance(token, str) and token:
            secret_values.add(token)

    snapshot["name"] = redact_sensitive_text(str(snapshot["name"]), secret_values)
    snapshot["endpoint"] = _redact_endpoint(str(snapshot["endpoint"]), secret_values)
    snapshot["headers"] = _redact(snapshot.get("headers", {}), secret_values)
    snapshot["auth"] = _redact(snapshot.get("auth", {}), secret_values)
    snapshot["request_template"] = _redact(
        snapshot.get("request_template", {}),
        secret_values,
    )
    return snapshot


def canonical_target_ref(target: TargetConfig) -> str:
    snapshot = canonical_target_binding(target)
    if snapshot is None or "endpoint" not in snapshot:
        raise ValueError("HTTP target endpoint is required")
    return str(snapshot["endpoint"])


def _redact(value: Any, secret_values: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                None
                if item is None
                else "[REDACTED]"
                if _is_redacted_key(str(key))
                else _redact(item, secret_values)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, secret_values) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value, secret_values)
    return value


def _redact_endpoint(endpoint: str, secret_values: set[str]) -> str:
    parsed = urlsplit(endpoint)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    query = urlencode(
        [
            (
                key,
                (
                    "[REDACTED]"
                    if _is_redacted_key(key)
                    else redact_sensitive_text(value, secret_values)
                ),
            )
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    return urlunsplit(
        (
            parsed.scheme,
            netloc,
            quote(
                redact_sensitive_text(unquote(parsed.path), secret_values),
                safe="/:@-._~",
            ),
            query,
            quote(
                redact_sensitive_text(unquote(parsed.fragment), secret_values),
                safe="-._~",
            ),
        )
    )


def _is_redacted_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(
        normalized == candidate or normalized.endswith(f"_{candidate}")
        for candidate in _REDACTED_KEYS
    )
