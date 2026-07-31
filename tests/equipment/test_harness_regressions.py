from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.equipment.catalog import EquipmentCatalog
from app.equipment.runner import EquipmentRunner
from app.equipment.security import SecretBroker, redact, summarize
from app.repositories.equipment_repository import EquipmentRepository
from app.schemas.equipment_schema import (
    EvidenceDraft,
    ExecutionBudget,
    HarnessPolicy,
    PackageType,
    ProviderContext,
    ProviderInstanceCreate,
    ProviderResult,
)
from app.services.equipment_service import EquipmentService
from app.services.harness_service import HarnessService
from conf.settings import EquipmentSettings

ROOT = Path(__file__).parents[2]


class _StaticSecretResolver:
    def __init__(self, value: str) -> None:
        self.value = value

    async def resolve(self, reference: str) -> str:
        return self.value


class _StaticProviderRunner:
    def __init__(self, result: ProviderResult) -> None:
        self.result = result
        self.calls = 0

    async def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return self.result.model_dump(mode="json")


def _execution_values() -> dict:
    return {
        "run_id": None,
        "step_id": None,
        "operation_id": "enterprise-operation-001",
        "package_id": "enterprise-ops-provider",
        "package_version": "1.0.0",
        "package_checksum": "a" * 64,
        "provider_instance_id": "enterprise-ops-production",
        "config_revision": "b" * 64,
        "secret_binding_revision": "c" * 64,
        "capability": "enterprise.change.execute.v1",
        "contract_checksum": "d" * 64,
        "test_principal_ref": "principal:test:tenant:enterprise:session:001",
        "request_fingerprint": "e" * 64,
        "input_summary": {"value": {"change_id": "change-001"}, "truncated": False},
    }


async def _persist_provider_instance(repository: EquipmentRepository) -> None:
    await repository.create_provider_instance(
        ProviderInstanceCreate(
            instance_id="enterprise-ops-production",
            provider_package_id="enterprise-ops-provider",
            provider_version="1.0.0",
            display_name="Enterprise Operations Production",
            environment="production",
            config={"base_url": "https://enterprise-ops.internal.example"},
            secret_refs={"api_token": "file:enterprise-ops/api_token"},
            allowed_hosts=["enterprise-ops.internal.example"],
            enabled=True,
        ),
        {"checksum": "a" * 64},
    )


@pytest.mark.asyncio
async def test_completed_execution_replays_structured_result_and_leases(
    session_factory,
) -> None:
    repository = EquipmentRepository(session_factory)
    await _persist_provider_instance(repository)
    values = _execution_values()
    _, created = await repository.begin_execution(values)
    assert created is True
    completed = await repository.complete_execution(
        values["operation_id"],
        status="success",
        result={"status": "success", "output": {"change": {"id": "change-001"}}},
        output_summary={"value": {"change": {"id": "change-001"}}, "truncated": False},
        evidence=[{"event_type": "provider_call_completed"}],
        physical_attempts=1,
        error_code=None,
        leases=[
            {
                "resource_type": "enterprise_change",
                "external_resource_id": "change-001",
                "cleanup_contract": "enterprise.change.execute.v1",
                "cleanup_payload": {"change_id": "change-001"},
            }
        ],
    )
    assert completed["resource_leases"][0]["external_resource_id"] == "change-001"

    replayed, created = await repository.begin_execution(values)
    assert created is False
    assert replayed["status"] == "success"
    assert replayed["result"]["output"]["change"]["id"] == "change-001"
    assert replayed["resource_leases"][0]["external_resource_id"] == "change-001"


@pytest.mark.asyncio
async def test_harness_replays_once_and_persists_only_redacted_provider_result(
    tmp_path: Path,
    session_factory,
) -> None:
    secret = "enterprise-call-scoped-token"
    repository = EquipmentRepository(session_factory)
    settings = EquipmentSettings(
        root=str(ROOT / "equipment"),
        contracts_root=str(ROOT / "contracts"),
        workspace_root=str(tmp_path / "workspaces"),
        archive_root=str(tmp_path / "archive"),
        require_signature=False,
        trust_roots_file=str(ROOT / "deploy/security/equipment_trust_roots.json"),
        revocations_file=str(ROOT / "deploy/security/equipment_revocations.json"),
    )
    service = EquipmentService(repository, EquipmentCatalog(settings))
    await service.reload()
    await repository.set_instance_enabled("enterprise-ops-production", True)
    package = await repository.get_package(
        PackageType.provider,
        "enterprise-ops-provider",
        "1.0.0",
    )
    contract = await repository.get_package(
        PackageType.contract,
        "enterprise.alert.read.v1",
        "1.0.0",
    )
    instance = await repository.get_provider_instance("enterprise-ops-production")
    runner = _StaticProviderRunner(
        ProviderResult(
            status="success",
            output={
                "alert": {
                    "id": "alert-001",
                    "status": "firing",
                    "severity": "critical",
                    "trigger_count": 1,
                    "first_triggered_at": "2026-07-31T10:00:00Z",
                    "last_triggered_at": "2026-07-31T10:00:00Z",
                    "resource": {"id": "vm-001", "name": secret},
                }
            },
            evidence=[
                EvidenceDraft(
                    evidence_type="enterprise_alert_snapshot",
                    summary={"alert_id": "alert-001", "upstream_note": secret},
                )
            ],
            external_operation_id=secret,
        )
    )
    harness = HarnessService(
        repository,
        service,
        cast(EquipmentRunner, runner),
        settings,
        secret_broker=SecretBroker(resolver=_StaticSecretResolver(secret)),
    )
    context = ProviderContext(
        run_id="dry-run",
        step_id="step-001",
        operation_id="provider-redaction-replay-001",
        target_id="target-001",
        case_id="case-001",
        provider_package_id=package["package_id"],
        provider_version=package["version"],
        provider_checksum=package["checksum"],
        provider_instance_id=instance["instance_id"],
        config_revision=instance["config_revision"],
        secret_binding_revision=instance["secret_binding_revision"],
        capability_contract_checksum=contract["checksum"],
        test_principal_ref="principal:test",
        approved_host_set=instance["allowed_hosts"],
        budget=ExecutionBudget(max_provider_calls=1, timeout_seconds=30),
    )
    policy = HarnessPolicy(
        allowed_capabilities=["enterprise.alert.read.v1"],
        allowed_targets=["target-001"],
        allowed_cases=["case-001"],
        allowed_principal_refs=["principal:test"],
        max_provider_calls=1,
    )

    first = await harness.invoke_provider(
        capability="enterprise.alert.read.v1",
        payload={"alert_id": "alert-001"},
        context=context,
        policy=policy,
    )
    replayed = await harness.invoke_provider(
        capability="enterprise.alert.read.v1",
        payload={"alert_id": "alert-001"},
        context=context,
        policy=policy,
    )

    assert runner.calls == 1
    assert replayed["id"] == first["id"]
    assert first["result"]["output"]["alert"]["resource"]["name"] == "[REDACTED]"
    assert first["result"]["external_operation_id"] == "[REDACTED]"
    assert secret not in str(first)


@pytest.mark.asyncio
async def test_operation_id_reuse_with_changed_fingerprint_is_rejected(
    session_factory,
) -> None:
    repository = EquipmentRepository(session_factory)
    await _persist_provider_instance(repository)
    values = _execution_values()
    await repository.begin_execution(values)

    with pytest.raises(ValueError, match="different execution facts"):
        await repository.begin_execution({**values, "request_fingerprint": "9" * 64})


@pytest.mark.asyncio
async def test_execution_completion_and_resource_leases_are_atomic(session_factory) -> None:
    repository = EquipmentRepository(session_factory)
    await _persist_provider_instance(repository)
    values = _execution_values()
    await repository.begin_execution(values)

    with pytest.raises(KeyError):
        await repository.complete_execution(
            values["operation_id"],
            status="success",
            result={"status": "success"},
            output_summary={},
            evidence=[],
            physical_attempts=1,
            error_code=None,
            leases=[{"resource_type": "broken"}],
        )

    persisted = await repository.get_execution(values["operation_id"])
    assert persisted["status"] == "running"
    assert "resource_leases" not in persisted


def test_nested_provider_secrets_are_redacted_from_results_and_summaries() -> None:
    secret = "enterprise-token-value"
    raw = {
        "output": {
            "authorization": f"Bearer {secret}",
            "nested": [{"cookie": secret}, {"safe": "retained"}],
        },
        "error": f"upstream rejected {secret}",
    }

    safe = redact(raw, (secret,))
    summary = summarize(safe)

    assert secret not in str(safe)
    assert secret not in str(summary)
    assert safe["output"]["nested"][1]["safe"] == "retained"


@pytest.mark.asyncio
async def test_cleanup_recovery_reuses_the_persisted_execution_principal() -> None:
    service = HarnessService.__new__(HarnessService)
    service.repository = AsyncMock()
    service.repository.list_pending_leases.return_value = [
        {
            "id": "lease-001",
            "run_id": "run-001",
            "created_by_operation_id": "enterprise-operation-001",
        }
    ]
    service.repository.get_execution.return_value = {
        "test_principal_ref": "principal:test:tenant:enterprise:session:001"
    }
    service.cleanup_leases = AsyncMock(return_value=[])

    await service.recover_pending_cleanups()

    service.cleanup_leases.assert_awaited_once_with(
        run_id="run-001",
        test_principal_ref="principal:test:tenant:enterprise:session:001",
        lease_ids={"lease-001"},
    )
