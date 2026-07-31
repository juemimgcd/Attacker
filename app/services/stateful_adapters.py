"""内置状态评测适配器；用隔离 SQL 夹具模拟 Memory 与 RAG 行为。"""

from app.repositories.stateful_repository import StatefulRepository
from app.schemas.stateful_schema import (
    CleanupResult,
    MemoryEvidence,
    RetrievalDocumentEvidence,
    RetrievalTrace,
    StateIdentity,
)


class MemoryAdapter:
    """按 run/tenant/user/session/namespace 作用域写入、读取和清理 Memory 夹具。"""

    def __init__(self, repository: StatefulRepository) -> None:
        self.repository = repository

    async def write(
        self,
        *,
        run_id: str,
        operation_id: str,
        scope_id: str,
        identity: StateIdentity,
        namespace: str,
        marker: str,
        active: bool,
    ) -> MemoryEvidence:
        fixture = await self.repository.write_fixture(
            run_id=run_id,
            operation_id=operation_id,
            scope_id=scope_id,
            kind="memory",
            identity=identity,
            namespace=namespace,
            resource_id=f"memory:{marker}",
            content_summary=f"test-marker:{marker}",
            provenance="untrusted_test_input",
            permissions={
                "tenant_id": identity.tenant_id,
                "user_id": identity.user_id,
            },
            poisoned=True,
            active=active,
        )
        return self._evidence(fixture)

    async def read(
        self,
        *,
        scope_id: str,
        identity: StateIdentity,
        namespace: str,
        strict_identity: bool,
    ) -> list[MemoryEvidence]:
        fixtures = await self.repository.list_fixtures(
            scope_id=scope_id,
            namespace=namespace,
            kind="memory",
        )
        return [
            self._evidence(fixture)
            for fixture in fixtures
            if fixture.active
            and (
                not strict_identity
                or (fixture.tenant_id == identity.tenant_id and fixture.user_id == identity.user_id)
            )
        ]

    async def cleanup(
        self,
        *,
        run_id: str,
        scope_id: str,
        operation_id: str,
    ) -> CleanupResult:
        return await self.repository.cleanup(
            run_id=run_id,
            scope_id=scope_id,
            operation_id=operation_id,
        )

    @staticmethod
    def _evidence(fixture) -> MemoryEvidence:
        return MemoryEvidence(
            resource_id=fixture.resource_id,
            tenant_id=fixture.tenant_id,
            user_id=fixture.user_id,
            session_id=fixture.session_id,
            namespace=fixture.namespace,
            content_summary=fixture.content_summary,
            provenance=fixture.provenance,
            poisoned=fixture.poisoned,
            active=fixture.active,
        )


class RAGAdapter:
    """索引合成文档并返回带 allowed/poisoned 标记的检索证据。"""

    def __init__(self, repository: StatefulRepository) -> None:
        self.repository = repository

    async def index(
        self,
        *,
        run_id: str,
        operation_id: str,
        scope_id: str,
        identity: StateIdentity,
        namespace: str,
        marker: str,
        active: bool,
    ) -> None:
        await self.repository.write_fixture(
            run_id=run_id,
            operation_id=operation_id,
            scope_id=scope_id,
            kind="rag",
            identity=identity,
            namespace=namespace,
            resource_id=f"document:{marker}",
            content_summary=f"test-marker:{marker}",
            provenance="isolated_test_corpus",
            permissions={
                "tenant_id": identity.tenant_id,
                "user_id": identity.user_id,
            },
            poisoned=True,
            active=active,
        )

    async def retrieve(
        self,
        *,
        scope_id: str,
        identity: StateIdentity,
        namespace: str,
        query: str,
        strict_identity: bool,
    ) -> RetrievalTrace:
        fixtures = await self.repository.list_fixtures(
            scope_id=scope_id,
            namespace=namespace,
            kind="rag",
        )
        documents: list[RetrievalDocumentEvidence] = []
        for rank, fixture in enumerate(fixtures, start=1):
            identity_allowed = (
                fixture.tenant_id == identity.tenant_id and fixture.user_id == identity.user_id
            )
            allowed = fixture.active and (identity_allowed or not strict_identity)
            if not fixture.active:
                reason = "quarantined"
            elif not identity_allowed and strict_identity:
                reason = "tenant_or_user_mismatch"
            else:
                reason = "allowed"
            documents.append(
                RetrievalDocumentEvidence(
                    document_id=fixture.resource_id,
                    rank=rank,
                    source=fixture.provenance,
                    tenant_id=fixture.tenant_id,
                    user_id=fixture.user_id,
                    allowed=allowed,
                    filter_reason=reason,
                    poisoned=fixture.poisoned,
                )
            )
        return RetrievalTrace(
            query_summary=query,
            documents=documents,
            permission_filter={
                "tenant_id": identity.tenant_id,
                "user_id": identity.user_id,
                "strict_identity": strict_identity,
            },
        )
