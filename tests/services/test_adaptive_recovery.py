import json
from typing import Any, cast

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.repositories.adaptive_repository import AdaptiveRepository
from app.repositories.run_repository import RunRepository
from app.schemas.graybox_schema import GrayBoxRunRequest
from app.schemas.judge_schema import TargetResponse
from app.schemas.target_schema import TargetConfig
from app.services.adaptive_run_service import AdaptiveRunService


class CountingConnector:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **_: Any) -> tuple[dict[str, Any], TargetResponse]:
        self.calls += 1
        return (
            {"messages": [{"role": "user", "content": "approved sandbox request"}]},
            TargetResponse(status_code=200, body={"status": "ok"}, text='{"status":"ok"}'),
        )


def _target() -> TargetConfig:
    return TargetConfig.model_validate(
        {
            "name": "approval-sandbox",
            "endpoint": "http://localhost:9000/agent",
            "auth": {"type": "bearer", "token": "runtime-only-secret"},
        }
    )


async def test_approval_resume_after_runtime_rehydration_calls_target_once(
    session_factory,
    tmp_path,
) -> None:
    checkpoint_path = tmp_path / "adaptive-checkpoints.sqlite3"
    repository = AdaptiveRepository(session_factory)
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
        await checkpointer.setup()
        first_process = AdaptiveRunService(
            repository=repository,
            checkpointer=checkpointer,
        )
        started = await first_process.start(
            GrayBoxRunRequest(
                target=_target(),
                case_ids=["gb_approval_bypass_attack"],
            )
        )
        approval = started["pending_approvals"][0]

        second_process = AdaptiveRunService(
            repository=repository,
            checkpointer=checkpointer,
        )
        connector = CountingConnector()
        cast(Any, second_process.graph).connector = connector
        resumed = await second_process.resume(
            run_id=started["run_id"],
            approval_id=approval["id"],
            approved=True,
            resolved_by="test-reviewer",
            reason="authorized isolated test",
            target=_target(),
        )

    report = await RunRepository(session_factory).get_report_rows(started["run_id"])
    event_types = [event["event_type"] for event in report["events"]]

    assert resumed["status"] == "completed"
    assert connector.calls == 1
    assert event_types.count("target_called") == 1
    assert event_types.index("runtime_rehydrated") < event_types.index(
        "recovery_policy_revalidated"
    )
    assert event_types.index("recovery_policy_revalidated") < event_types.index("target_called")
    assert "runtime-only-secret" not in json.dumps(report)
    assert b"runtime-only-secret" not in checkpoint_path.read_bytes()
