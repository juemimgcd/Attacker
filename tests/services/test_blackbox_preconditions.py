import hashlib
from pathlib import Path
from typing import Any, Literal

import pytest

from app.repositories.run_repository import RunRepository
from app.schemas.attack_sample_schema import (
    BlackBoxCase,
    CaseKind,
    EvaluatorRules,
    EvaluatorType,
    RiskLevel,
)
from app.schemas.judge_schema import TargetResponse
from app.schemas.run_schema import LoadedDataset, RunBudget
from app.schemas.target_schema import TargetConfig, TargetRequestTemplate
from app.services.report_service import ReportService
from app.services.run_service import DeterministicRunService
from app.services.target_connector.http_connector import HTTPTargetConnector


class RecordingConnector(HTTPTargetConnector):
    def __init__(self) -> None:
        self.calls = 0

    async def call(
        self,
        target: TargetConfig,
        prompt: str,
        messages: list[dict[str, str]] | None = None,
    ) -> tuple[dict[str, Any], TargetResponse]:
        self.calls += 1
        return self.build_request_body(target, prompt, messages), TargetResponse(
            status_code=200,
            text="ordinary safe response",
            latency_ms=1,
            response_bytes=22,
        )


def _case(
    *,
    delivery_mode: Literal["direct", "multi_turn", "target_fixture"],
) -> BlackBoxCase:
    is_multiturn = delivery_mode == "multi_turn"
    return BlackBoxCase(
        id=f"case-{delivery_mode}",
        name=f"Case {delivery_mode}",
        category="test",
        severity=RiskLevel.high,
        kind=CaseKind.attack,
        prompts=["first", "second"] if is_multiturn else ["test"],
        expected_violation="the protected boundary is crossed",
        expected_outcome="refused",
        evaluator=EvaluatorRules(
            type=EvaluatorType.direct_prompt_injection,
            exact_match_patterns=["VIOLATION"],
        ),
        delivery_mode=delivery_mode,
        setup_requirements=["prepare the synthetic fixture"]
        if delivery_mode == "target_fixture"
        else [],
    )


def _dataset(case: BlackBoxCase) -> LoadedDataset:
    snapshot = {
        "name": "precondition-tests",
        "cases": [{"name": case.id, "inputs": case.model_dump(mode="json")}],
    }
    digest = hashlib.sha256(repr(snapshot).encode()).hexdigest()
    return LoadedDataset(
        name="precondition-tests",
        version="1",
        source_path=Path("<test>"),
        sha256=digest,
        cases=[case],
        snapshot=snapshot,
    )


def _target(*, messages: bool = True) -> TargetConfig:
    body: dict[str, Any] = {"messages": [{"role": "user", "content": "{prompt}"}]}
    if not messages:
        body = {"input": "{prompt}"}
    return TargetConfig(
        name="local-test",
        endpoint="http://127.0.0.1:9000/chat",
        request_template=TargetRequestTemplate(body_template=body),
    )


@pytest.mark.parametrize(
    ("case", "target", "reason"),
    [
        (
            _case(delivery_mode="target_fixture"),
            _target(),
            "target fixture preparation evidence was not provided",
        ),
        (
            _case(delivery_mode="multi_turn"),
            _target(messages=False),
            "target request template does not support multi-turn message history",
        ),
    ],
)
async def test_unmet_case_preconditions_are_reported_without_target_calls(
    session_factory,
    case: BlackBoxCase,
    target: TargetConfig,
    reason: str,
) -> None:
    repository = RunRepository(session_factory)
    connector = RecordingConnector()
    service = DeterministicRunService(repository, connector=connector)

    rows = await service.run_dataset(
        target=target,
        dataset=_dataset(case),
        budget=RunBudget(max_cases=1, max_target_calls=2),
        mode="deterministic",
    )
    report = await ReportService(repository).build_json(rows["run"]["id"])

    assert connector.calls == 0
    assert rows["steps"][0]["outcome"] == "not_evaluable"
    assert rows["steps"][0]["result"]["evaluation"]["reason"] == reason
    assert rows["steps"][0]["result"]["evaluation"]["evidence_complete"] is False
    assert any(event["event_type"] == "evaluation_skipped" for event in rows["events"])
    assert report["summary"]["completed_cases"] == 0
    assert report["summary"]["outcomes"]["not_evaluable"] == 1


async def test_fixture_preparation_evidence_allows_execution_and_is_persisted(
    session_factory,
) -> None:
    repository = RunRepository(session_factory)
    connector = RecordingConnector()
    service = DeterministicRunService(repository, connector=connector)
    case = _case(delivery_mode="target_fixture")

    rows = await service.run_dataset(
        target=_target(),
        dataset=_dataset(case),
        budget=RunBudget(max_cases=1, max_target_calls=1),
        mode="deterministic",
        fixture_evidence_refs={case.id: "fixture-log://setup/123"},
    )

    assert connector.calls == 1
    assert rows["steps"][0]["outcome"] == "safe"
    assert rows["steps"][0]["result"]["precondition_evidence"] == {
        "fixture_preparation": "fixture-log://setup/123"
    }


async def test_fixture_evidence_rejects_non_fixture_case_ids(session_factory) -> None:
    repository = RunRepository(session_factory)
    service = DeterministicRunService(repository, connector=RecordingConnector())

    with pytest.raises(ValueError, match="unknown or non-fixture"):
        await service.run_dataset(
            target=_target(),
            dataset=_dataset(_case(delivery_mode="direct")),
            budget=RunBudget(max_cases=1, max_target_calls=1),
            mode="deterministic",
            fixture_evidence_refs={"case-direct": "fixture-log://setup/123"},
        )
