from pathlib import Path

import pytest

from app.infrastructure.secrets import (
    FileSecretResolver,
    VaultKvV2SecretResolver,
    build_secret_broker,
)
from conf.settings import SecretSettings


@pytest.mark.asyncio
async def test_file_secret_rotation_is_visible_to_the_next_lease(tmp_path: Path) -> None:
    secret_dir = tmp_path / "enterprise"
    secret_dir.mkdir()
    secret_file = secret_dir / "api_token"
    secret_file.write_text("token-v1\n", encoding="utf-8")
    broker = build_secret_broker(
        SecretSettings(
            backend="file",
            allow_environment_references=False,
            file_root=str(tmp_path),
        )
    )

    async with broker.lease({"api_token": "file:enterprise/api_token"}) as first:
        assert first.provider_environment(["api_token"]) == {
            "ATTACKER_SECRET_API_TOKEN": "token-v1"
        }

    replacement = secret_dir / "api_token.next"
    replacement.write_text("token-v2\n", encoding="utf-8")
    replacement.replace(secret_file)

    async with broker.lease({"api_token": "file:enterprise/api_token"}) as second:
        assert second.provider_environment(["api_token"]) == {
            "ATTACKER_SECRET_API_TOKEN": "token-v2"
        }
        assert "token-v1" not in second.redaction_values


@pytest.mark.asyncio
async def test_file_secret_resolver_rejects_traversal_and_symlinks(tmp_path: Path) -> None:
    resolver = FileSecretResolver(tmp_path)
    outside = tmp_path.parent / "outside-secret"
    outside.write_text("not-allowed", encoding="utf-8")

    with pytest.raises(ValueError, match="safe relative path"):
        await resolver.resolve("file:../outside-secret")

    link = tmp_path / "linked-secret"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable on this host")
    with pytest.raises(ValueError, match="symbolic links"):
        await resolver.resolve("file:linked-secret")


@pytest.mark.asyncio
async def test_file_secret_resolver_rejects_empty_files(tmp_path: Path) -> None:
    (tmp_path / "empty-secret").write_text("\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mounted secret is empty"):
        await FileSecretResolver(tmp_path).resolve("file:empty-secret")


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("vault:teams/security/attacker#api_token", ("teams/security/attacker", "api_token")),
        ("vault:platform/alerting#webhook_url", ("platform/alerting", "webhook_url")),
    ],
)
def test_vault_reference_parser_accepts_safe_kv_v2_paths(
    reference: str,
    expected: tuple[str, str],
) -> None:
    assert VaultKvV2SecretResolver._parse_reference(reference) == expected


@pytest.mark.parametrize(
    "reference",
    [
        "vault:/absolute#token",
        "vault:../escape#token",
        "vault:path/./secret#token",
        "vault:path/no-field",
    ],
)
def test_vault_reference_parser_rejects_unsafe_paths(reference: str) -> None:
    with pytest.raises(ValueError, match="vault:path/to/secret#field"):
        VaultKvV2SecretResolver._parse_reference(reference)
