from app.repositories.run_repository import RunRepository
from app.repositories.stateful_repository import StatefulRepository
from app.schemas.stateful_schema import StatefulProfile, StatefulRunRequest
from app.services.report_service import ReportService
from app.services.stateful_run_service import StatefulRunService


async def test_reports_rebuild_stateful_evidence_from_sqlite(session_factory) -> None:
    stateful_repository = StatefulRepository(session_factory)
    result = await StatefulRunService(stateful_repository).run(
        StatefulRunRequest(
            profile=StatefulProfile.vulnerable,
            case_ids=["st_memory_poisoning_attack"],
        )
    )
    report_service = ReportService(RunRepository(session_factory))

    report = await report_service.build_json(str(result["run_id"]))
    markdown = await report_service.build_markdown(str(result["run_id"]))

    assert report["run"]["id"] == result["run_id"]
    assert report["findings"][0]["evidence_event_ids"]
    assert report["state_fixtures"]
    assert report["state_snapshots"]
    assert "Evidence events:" in markdown
    assert "## Stateful Evidence" in markdown
