import json
from asyncio import CancelledError
from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlalchemy import select

from app.models import EvaluationRunRecord
from app.repositories.adaptive_repository import AdaptiveRepository
from app.repositories.run_repository import RunRepository
from app.schemas.graybox_schema import (
    AttackPolicy,
    DeterministicGrayBoxRunRequest,
    GrayBoxCase,
    GrayBoxRunRequest,
)
from app.schemas.judge_schema import TargetResponse
from app.schemas.target_schema import TargetConfig
from app.services.adaptive_run_service import AdaptiveRunService, DeterministicGrayBoxRunService
from app.services.graybox_connector import GrayBoxConnector
from app.services.sample_loader import GrayBoxDatasetLoader


class CountingGrayBoxConnector(GrayBoxConnector):
    def __init__(self, exc: BaseException | None = None) -> None:
        self.calls = 0
        self.exc = exc

    async def execute(
        self,
        *,
        target: TargetConfig,
        case: GrayBoxCase,
        operation_id: str,
        approval_id: str | None,
    ) -> tuple[dict[str, Any], TargetResponse]:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        trace = {
            "tool_events": [
                {
                    "tool_call_id": "call-1",
                    "tool_name": case.allowed_tools[0],
                    "caller_identity": "test-principal",
                    "policy_decision": "allow",
                    "executed": True,
                    "sequence": 2,
                }
            ],
            "policy_events": [
                {
                    "tool_call_id": "call-1",
                    "policy_name": "test-policy",
                    "decision": "allow",
                    "reason": "explicit deterministic authorization",
                    "evaluated_identity": "test-principal",
                    "sequence": 1,
                }
            ],
            "approval_events": [
                {
                    "approval_id": approval_id,
                    "case_id": case.id,
                    "status": "approved",
                    "resolved_by": "deterministic-baseline",
                    "reason": "explicit test authorization",
                }
            ],
        }
        body = {"trace": trace}
        return {"case_id": case.id}, TargetResponse(
            status_code=200,
            body=body,
            text=json.dumps(body),
            response_bytes=len(json.dumps(body).encode()),
        )


class FailingEquipmentService:
    async def freeze_run_bindings(self, **_: Any) -> None:
        raise RuntimeError("equipment credential=do-not-persist")


class CapturingGraph:
    def __init__(self) -> None:
        self.state: dict[str, Any] | None = None

    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        self.state = state
        return {}


def _target() -> TargetConfig:
    return TargetConfig.model_validate(
        {
            "name": "graybox-sandbox",
            "endpoint": "http://127.0.0.1:9000/agent",
        }
    )


async def test_graybox_loader_rejects_paths_outside_the_graybox_samples_root() -> None:
    with pytest.raises(ValueError, match="inside samples/graybox"):
        await GrayBoxDatasetLoader().load(Path("pyproject.toml"))


async def test_repository_preserves_explicit_policy_allowlists(session_factory) -> None:
    dataset = await GrayBoxDatasetLoader().load("samples/graybox/phase2.yaml")
    selected = dataset.cases[0]
    repository = AdaptiveRepository(session_factory)
    policy = AttackPolicy(
        allowed_case_ids={selected.id},
        allowed_capability_contracts={selected.capability_contract},
        allowed_provider_instance_refs={selected.provider_instance_ref},
    )

    _, _, _, effective = await repository.create_run(
        target_snapshot={"name": "sandbox", "endpoint": "http://127.0.0.1:9000/agent"},
        dataset=dataset,
        policy=policy,
        mode="deterministic_graybox",
        baseline_run_id=None,
    )

    assert effective.allowed_case_ids == {selected.id}
    assert effective.allowed_capability_contracts == {selected.capability_contract}
    assert effective.allowed_provider_instance_refs == {selected.provider_instance_ref}

    with pytest.raises(ValueError, match="unknown case IDs"):
        await repository.create_run(
            target_snapshot={"name": "sandbox", "endpoint": "http://127.0.0.1:9000/agent"},
            dataset=dataset,
            policy=AttackPolicy(allowed_case_ids={"unknown-case"}),
            mode="deterministic_graybox",
            baseline_run_id=None,
        )


async def test_adaptive_start_freezes_effective_case_allowlist_in_graph_state(
    session_factory,
    tmp_path,
) -> None:
    dataset = await GrayBoxDatasetLoader().load("samples/graybox/phase2.yaml")
    selected = dataset.cases[0]
    policy = AttackPolicy(
        allowed_case_ids={selected.id},
        allowed_capability_contracts={selected.capability_contract},
        allowed_provider_instance_refs={selected.provider_instance_ref},
    )
    checkpoint_path = tmp_path / "adaptive-policy-checkpoints.sqlite3"
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
        await checkpointer.setup()
        service = AdaptiveRunService(
            repository=AdaptiveRepository(session_factory),
            checkpointer=checkpointer,
        )
        graph = CapturingGraph()
        service.graph.graph = cast(Any, graph)

        result = await service.start(GrayBoxRunRequest(target=_target(), policy=policy))

    assert result["status"] == "running"
    assert graph.state is not None
    assert graph.state["allowed_case_ids"] == [selected.id]


async def test_deterministic_approval_requires_explicit_preauthorization(session_factory) -> None:
    repository = AdaptiveRepository(session_factory)
    connector = CountingGrayBoxConnector()
    service = DeterministicGrayBoxRunService(repository, connector=connector)
    base_request = {
        "target": _target(),
        "case_ids": ["gb_approval_control"],
    }

    denied = await service.run(DeterministicGrayBoxRunRequest.model_validate(base_request))
    denied_rows = await RunRepository(session_factory).get_report_rows(denied["run_id"])

    assert connector.calls == 0
    assert denied_rows["steps"][0]["outcome"] == "policy_denied"
    assert denied_rows["approvals"] == []

    allowed = await service.run(
        DeterministicGrayBoxRunRequest.model_validate(
            {**base_request, "preauthorize_approvals": True}
        )
    )
    allowed_rows = await RunRepository(session_factory).get_report_rows(allowed["run_id"])

    assert connector.calls == 1
    assert allowed_rows["steps"][0]["outcome"] == "safe"
    assert allowed_rows["approvals"][0]["status"] == "approved"


@pytest.mark.parametrize(
    ("exc", "expected_status", "event_type"),
    [
        (RuntimeError("target credential=do-not-persist"), "failed", "run_failed"),
        (CancelledError("client credential=do-not-persist"), "cancelled", "run_cancelled"),
    ],
)
async def test_deterministic_graybox_interruptions_are_terminalized(
    session_factory,
    exc: BaseException,
    expected_status: str,
    event_type: str,
) -> None:
    repository = AdaptiveRepository(session_factory)
    connector = CountingGrayBoxConnector(exc)
    service = DeterministicGrayBoxRunService(repository, connector=connector)

    with pytest.raises(type(exc)):
        await service.run(
            DeterministicGrayBoxRunRequest(
                target=_target(),
                case_ids=["gb_unauthorized_tool_attack"],
            )
        )

    async with session_factory() as session:
        run = await session.scalar(select(EvaluationRunRecord))
        assert run is not None
        run_id = run.id
        assert run.status == expected_status
        assert run.terminal_reason == type(exc).__name__

    rows = await RunRepository(session_factory).get_report_rows(run_id)
    assert any(event["event_type"] == event_type for event in rows["events"])
    assert "do-not-persist" not in repr(rows)


async def test_adaptive_start_failure_is_terminalized(session_factory, tmp_path) -> None:
    repository = AdaptiveRepository(session_factory)
    checkpoint_path = tmp_path / "adaptive-checkpoints.sqlite3"
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
        await checkpointer.setup()
        service = AdaptiveRunService(
            repository=repository,
            checkpointer=checkpointer,
            equipment_service=cast(Any, FailingEquipmentService()),
        )

        with pytest.raises(RuntimeError, match="do-not-persist"):
            await service.start(
                DeterministicGrayBoxRunRequest(
                    target=_target(),
                    case_ids=["gb_unauthorized_tool_attack"],
                )
            )

    async with session_factory() as session:
        run = await session.scalar(select(EvaluationRunRecord))
        assert run is not None
        run_id = run.id
        assert run.status == "failed"
        assert run.terminal_reason == "RuntimeError"

    rows = await RunRepository(session_factory).get_report_rows(run_id)
    assert "do-not-persist" not in repr(rows)
