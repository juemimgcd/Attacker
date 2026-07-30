from __future__ import annotations

from app.schemas.equipment_schema import (
    EvidenceDraft,
    ProviderContext,
    ProviderResult,
    ResourceLeaseDraft,
)


class IsolatedStateProvider:
    async def describe(self) -> dict:
        return {"id": "isolated-state-provider", "version": "1.0.0"}

    async def validate_config(self, config: dict) -> dict:
        return {"valid": True}

    async def healthcheck(self, context: ProviderContext | dict) -> dict:
        return {"healthy": True, "backend": "isolated-test-state"}

    async def invoke(
        self,
        capability: str,
        payload: dict,
        context: ProviderContext | dict,
    ) -> ProviderResult:
        context = ProviderContext.model_validate(context)
        resource_id = f"{context.operation_id}:resource"
        if capability in {"memory.fixture.write.v1", "rag.document.index.v1"}:
            return ProviderResult(
                status="success",
                output={"resource_id": resource_id},
                evidence=[
                    EvidenceDraft(
                        evidence_type=(
                            "indexed_document"
                            if capability == "rag.document.index.v1"
                            else "state_fixture"
                        ),
                        summary={"resource_id": resource_id, "scope": context.test_principal_ref},
                    )
                ],
                resource_leases=[
                    ResourceLeaseDraft(
                        resource_type="isolated_state",
                        external_resource_id=resource_id,
                        cleanup_contract="memory.fixture.cleanup.v1",
                        cleanup_payload={"resource_id": resource_id},
                    )
                ],
            )
        if capability == "memory.fixture.read.v1":
            return ProviderResult(
                status="success",
                output={"items": []},
                evidence=[EvidenceDraft(evidence_type="state_snapshot", summary={"item_count": 0})],
            )
        if capability == "rag.retrieval.query.v1":
            return ProviderResult(
                status="success",
                output={"documents": []},
                evidence=[
                    EvidenceDraft(
                        evidence_type="retrieval_trace",
                        summary={"document_count": 0},
                    )
                ],
            )
        if capability == "memory.fixture.cleanup.v1":
            return ProviderResult(
                status="success",
                output={"cleaned": True},
                evidence=[EvidenceDraft(evidence_type="cleanup_result", summary={"cleaned": True})],
            )
        return ProviderResult(
            status="denied",
            error_code="capability_not_implemented",
            error_message=capability,
        )

    async def cleanup(self, resource: dict, context: ProviderContext | dict) -> dict:
        return {"cleaned": True, "resource_id": resource.get("external_resource_id")}
