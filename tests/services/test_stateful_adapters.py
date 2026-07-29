from app.repositories.stateful_repository import StatefulRepository
from app.schemas.stateful_schema import StatefulProfile, StateIdentity
from app.services.sample_loader import StatefulDatasetLoader
from app.services.stateful_adapters import MemoryAdapter, RAGAdapter


async def test_memory_and_rag_are_identity_isolated_and_scope_cleanup_is_safe(
    session_factory,
) -> None:
    repository = StatefulRepository(session_factory)
    dataset = await StatefulDatasetLoader().load(
        "samples/stateful/phase3.yaml",
        ["st_identity_isolation_attack"],
    )
    run_id = await repository.create_run(
        dataset=dataset,
        profile=StatefulProfile.hardened,
        target_name="test",
        mode="stateful_test",
    )
    attacker = StateIdentity(tenant_id="tenant-a", user_id="user-a", session_id="session-a")
    observer = StateIdentity(tenant_id="tenant-b", user_id="user-b", session_id="session-b")
    memory = MemoryAdapter(repository)
    rag = RAGAdapter(repository)

    await memory.write(
        run_id=run_id,
        operation_id="memory-write",
        scope_id="scope-a",
        identity=attacker,
        namespace="shared-name",
        marker="poison",
        active=True,
    )
    await rag.index(
        run_id=run_id,
        operation_id="rag-write",
        scope_id="scope-a",
        identity=attacker,
        namespace="shared-name",
        marker="poison",
        active=True,
    )
    observed_memory = await memory.read(
        scope_id="scope-a",
        identity=observer,
        namespace="shared-name",
        strict_identity=True,
    )
    retrieval = await rag.retrieve(
        scope_id="scope-a",
        identity=observer,
        namespace="shared-name",
        query="poison",
        strict_identity=True,
    )
    cleanup = await memory.cleanup(
        run_id=run_id,
        scope_id="scope-a",
        operation_id="scope-cleanup",
    )

    assert observed_memory == []
    assert retrieval.documents[0].allowed is False
    assert retrieval.documents[0].filter_reason == "tenant_or_user_mismatch"
    assert cleanup.remaining_active_count == 0
    assert cleanup.outside_scope_affected_count == 0
