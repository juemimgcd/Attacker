import asyncio
from pathlib import Path

from sqlalchemy import select

from app.models import EvaluationRunRecord, EventRecord
from app.repositories.event_store import EventStore
from app.repositories.run_repository import RunRepository
from app.schemas.run_schema import LoadedDataset, RunBudget


async def test_event_store_allocates_unique_monotonic_sequences_under_concurrency(
    session_factory,
) -> None:
    run_id = await RunRepository(session_factory).create_run(
        target_snapshot={"name": "event-race", "endpoint": "http://localhost"},
        dataset=LoadedDataset(
            name="empty",
            version="1",
            source_path=Path("empty"),
            sha256="e" * 64,
            cases=[],
            snapshot={"cases": []},
        ),
        budget=RunBudget(),
    )
    store = EventStore(session_factory)

    event_ids = await asyncio.gather(
        *(
            store.append(
                run_id=run_id,
                operation_id=f"{run_id}:concurrent:{index}",
                event_type="concurrency_probe",
                evidence={"index": index},
            )
            for index in range(12)
        )
    )

    assert len(set(event_ids)) == 12
    async with session_factory() as session:
        sequences = list(
            await session.scalars(
                select(EventRecord.sequence)
                .where(EventRecord.run_id == run_id)
                .order_by(EventRecord.sequence)
            )
        )
        run = await session.get(EvaluationRunRecord, run_id)
    assert sequences == list(range(1, 14))
    assert run is not None
    assert run.event_sequence == 13
