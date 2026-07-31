import importlib.util
import json
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).parents[2]


def _manifest(relative: str) -> dict:
    return yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))


def _load_handler(relative: str, symbol: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(symbol, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, symbol)()


def _provider_context() -> dict:
    return {
        "run_id": "run-001",
        "step_id": "step-001",
        "operation_id": "operation-001",
        "target_id": "target-001",
        "case_id": "case-001",
        "provider_package_id": "enterprise-ops-provider",
        "provider_version": "1.0.0",
        "provider_checksum": "a" * 64,
        "provider_instance_id": "enterprise-ops-production",
        "config_revision": "b" * 64,
        "secret_binding_revision": "c" * 64,
        "capability_contract_checksum": "d" * 64,
        "test_principal_ref": "principal:test",
        "approved_host_set": ["enterprise-ops.internal.example"],
        "budget": {"timeout_seconds": 10},
        "config": {
            "base_url": "https://enterprise-ops.internal.example",
            "auth_secret_name": "api_token",
        },
    }


def test_enterprise_contracts_keep_reads_separate_from_high_risk_changes() -> None:
    resource = _manifest("contracts/enterprise.resource.read.v1/contract.yaml")
    alert = _manifest("contracts/enterprise.alert.read.v1/contract.yaml")
    change = _manifest("contracts/enterprise.change.execute.v1/contract.yaml")

    assert (resource["side_effect"], resource["risk_level"]) == ("read", "low")
    assert (alert["side_effect"], alert["risk_level"]) == ("read", "low")
    assert (change["side_effect"], change["risk_level"]) == ("external_call", "high")
    assert change["idempotency"]["operation_id_required"] is True
    assert change["idempotency"]["provider_retry"] == "forbidden"


@pytest.mark.asyncio
async def test_resource_compliance_skill_flags_public_unencrypted_resource() -> None:
    handler = _load_handler(
        "equipment/skills/enterprise-resource-compliance-evaluator/handler.py",
        "EnterpriseResourceComplianceEvaluator",
    )
    first = await handler.execute({"resource_id": "vm-001"}, {})
    assert first.capability_requests[0].binding == "resource"

    completed = await handler.execute(
        {
            "resource_id": "vm-001",
            "capability_results": [
                {
                    "request_id": "resource-vm-001",
                    "binding": "resource",
                    "capability": "enterprise.resource.read.v1",
                    "status": "success",
                    "operation_id": "op-resource",
                    "output": {
                        "resource": {
                            "id": "vm-001",
                            "name": "public-vm",
                            "type": "virtual_machine",
                            "environment": "production",
                            "properties": {
                                "public_access": True,
                                "encryption_enabled": False,
                                "backup_enabled": True,
                                "monitoring_enabled": True,
                                "patch_age_days": 10,
                            },
                        }
                    },
                }
            ],
        },
        {},
    )
    assert completed.output["overall_compliance"] == "non_compliant"
    assert {item["control"] for item in completed.output["findings"]} >= {
        "public_access",
        "encryption",
    }


@pytest.mark.asyncio
async def test_resource_compliance_skill_does_not_treat_missing_access_fact_as_compliant() -> None:
    handler = _load_handler(
        "equipment/skills/enterprise-resource-compliance-evaluator/handler.py",
        "EnterpriseResourceComplianceEvaluator",
    )
    completed = await handler.execute(
        {
            "resource_id": "vm-002",
            "capability_results": [
                {
                    "request_id": "resource-vm-002",
                    "binding": "resource",
                    "capability": "enterprise.resource.read.v1",
                    "status": "success",
                    "operation_id": "op-resource-missing-access",
                    "output": {
                        "resource": {
                            "id": "vm-002",
                            "name": "unknown-access-vm",
                            "type": "virtual_machine",
                            "environment": "production",
                            "properties": {
                                "encryption_enabled": True,
                                "backup_enabled": True,
                                "monitoring_enabled": True,
                                "patch_age_days": 10,
                            },
                        }
                    },
                }
            ],
        },
        {},
    )
    finding = next(
        item for item in completed.output["findings"] if item["control"] == "public_access"
    )
    assert finding["status"] == "non_compliant"


@pytest.mark.asyncio
async def test_alert_triage_skill_classifies_short_window_repeats_as_noisy() -> None:
    handler = _load_handler(
        "equipment/skills/enterprise-alert-triage-evaluator/handler.py",
        "EnterpriseAlertTriageEvaluator",
    )
    completed = await handler.execute(
        {
            "alert_id": "alert-001",
            "capability_results": [
                {
                    "request_id": "alert-alert-001",
                    "binding": "alert",
                    "capability": "enterprise.alert.read.v1",
                    "status": "success",
                    "operation_id": "op-alert",
                    "output": {
                        "alert": {
                            "id": "alert-001",
                            "status": "firing",
                            "severity": "warning",
                            "trigger_count": 8,
                            "first_triggered_at": "2026-07-31T10:00:00Z",
                            "last_triggered_at": "2026-07-31T10:30:00Z",
                            "resource": {"id": "vm-001", "name": "api-vm"},
                        }
                    },
                }
            ],
        },
        {},
    )
    assert completed.output["pattern"] == "noisy"
    assert completed.output["recommended_action"] == "investigate_then_mute"


@pytest.mark.asyncio
async def test_change_risk_skill_blocks_uncontrolled_high_blast_radius_change() -> None:
    handler = _load_handler(
        "equipment/skills/enterprise-change-risk-evaluator/handler.py",
        "EnterpriseChangeRiskEvaluator",
    )
    payload = {
        "change": {
            "id": "change-001",
            "action": "delete",
            "environment": "production",
            "resource_criticality": "critical",
            "blast_radius": 50,
            "owner": "",
            "approval_reference": "",
            "rollback_plan": "",
            "maintenance_window": "",
        }
    }
    result = await handler.execute(payload, {})
    repeated = await handler.execute(payload, {})
    assert result.output["decision"] == "blocked"
    assert repeated == result
    assert set(result.output["missing_controls"]) >= {
        "owner",
        "approval_reference",
        "rollback_plan",
        "maintenance_window",
        "bounded_blast_radius",
    }


@pytest.mark.asyncio
async def test_enterprise_provider_rejects_oversized_upstream_response(monkeypatch) -> None:
    handler = _load_handler(
        "equipment/providers/enterprise-ops-provider/adapter.py",
        "EnterpriseOpsProvider",
    )
    oversized = json.dumps(
        {
            "id": "vm-oversized",
            "properties": {"inventory": "x" * 524_288},
        }
    ).encode()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=oversized, request=request)
    )
    real_client = httpx.AsyncClient
    method_globals = handler._request.__func__.__globals__

    def client_factory(**kwargs):
        return real_client(transport=transport, **kwargs)

    monkeypatch.setitem(method_globals, "validate_outbound_url", lambda url, hosts: hosts[0])
    monkeypatch.setitem(method_globals, "provider_secret", lambda name: "scoped-token")
    monkeypatch.setattr(method_globals["httpx"], "AsyncClient", client_factory)

    result = await handler.invoke(
        "enterprise.resource.read.v1",
        {"resource_id": "vm-oversized"},
        _provider_context(),
    )

    assert result.status == "error"
    assert result.error_code == "enterprise_response_too_large"


@pytest.mark.asyncio
async def test_enterprise_change_passes_core_operation_id_upstream(monkeypatch) -> None:
    handler = _load_handler(
        "equipment/providers/enterprise-ops-provider/adapter.py",
        "EnterpriseOpsProvider",
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"status": "submitted"},
            headers={"x-operation-id": "upstream-operation-001"},
            request=request,
        )

    transport = httpx.MockTransport(respond)
    real_client = httpx.AsyncClient
    method_globals = handler._request.__func__.__globals__

    def client_factory(**kwargs):
        return real_client(transport=transport, **kwargs)

    monkeypatch.setitem(method_globals, "validate_outbound_url", lambda url, hosts: hosts[0])
    monkeypatch.setitem(method_globals, "provider_secret", lambda name: "scoped-token")
    monkeypatch.setattr(method_globals["httpx"], "AsyncClient", client_factory)

    result = await handler.invoke(
        "enterprise.change.execute.v1",
        {
            "change_id": "change-001",
            "approval_reference": "CAB-001",
            "action": "restart",
        },
        _provider_context(),
    )

    assert result.status == "success"
    assert requests[0].headers["idempotency-key"] == "operation-001"


def test_enterprise_resource_normalization_drops_uncontracted_properties() -> None:
    handler = _load_handler(
        "equipment/providers/enterprise-ops-provider/adapter.py",
        "EnterpriseOpsProvider",
    )

    resource = handler._normalize_resource(
        {
            "id": "vm-001",
            "properties": {
                "public_access": False,
                "encryption_enabled": True,
                "backup_enabled": True,
                "monitoring_enabled": True,
                "patch_age_days": 4,
                "api_token": "must-not-cross-the-contract",
                "inventory": ["unbounded", "shape"],
            },
        },
        "vm-001",
    )

    assert resource["properties"] == {
        "public_access": False,
        "encryption_enabled": True,
        "backup_enabled": True,
        "monitoring_enabled": True,
        "patch_age_days": 4,
    }
