from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    EquipmentAuditEventRecord,
    EquipmentExecutionRecord,
    EquipmentPackageRecord,
    EventRecord,
    ProviderInstanceRecord,
    ResourceLeaseRecord,
    RunEquipmentSnapshotRecord,
)
from app.schemas.equipment_schema import DiscoveredPackage, PackageType, ProviderInstanceCreate


class EquipmentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def register_packages(self, packages: list[DiscoveredPackage]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        async with self.session_factory.begin() as session:
            for package in packages:
                existing = await session.scalar(
                    select(EquipmentPackageRecord).where(
                        EquipmentPackageRecord.package_type == package.package_type.value,
                        EquipmentPackageRecord.package_id == package.package_id,
                        EquipmentPackageRecord.version == package.version,
                    )
                )
                event_type = "equipment_discovered"
                if existing is not None and existing.checksum != package.checksum:
                    existing.validation_status = "invalid"
                    existing.enabled = False
                    existing.validation_errors_json = ["immutable_version_conflict"]
                    event_type = "equipment_validation_failed"
                    results.append(
                        {
                            **self.package_dict(existing),
                            "error_code": "immutable_version_conflict",
                        }
                    )
                else:
                    record = existing or EquipmentPackageRecord(
                        id=str(uuid4()),
                        package_type=package.package_type.value,
                        package_id=package.package_id,
                        version=package.version,
                        source_path=package.source_path,
                        checksum=package.checksum,
                        manifest_json=package.manifest,
                        trust_level=str(package.manifest.get("trust_level", "trusted_enterprise")),
                        enabled=(
                            package.validation_status == "valid"
                            and package.manifest.get("trust_level") == "trusted_builtin"
                        ),
                        validation_status=package.validation_status,
                        validation_errors_json=package.validation_errors,
                    )
                    record.source_path = package.source_path
                    record.manifest_json = package.manifest
                    record.trust_level = str(
                        package.manifest.get("trust_level", "trusted_enterprise")
                    )
                    record.validation_status = package.validation_status
                    record.validation_errors_json = package.validation_errors
                    if package.validation_status != "valid":
                        record.enabled = False
                        event_type = "equipment_validation_failed"
                    elif record.enabled:
                        record.loaded_at = datetime.now(UTC)
                    elif record.trust_level == "trusted_builtin":
                        record.enabled = True
                        record.loaded_at = datetime.now(UTC)
                    if existing is None:
                        session.add(record)
                        await session.flush()
                    results.append(self.package_dict(record))
                await self._audit(
                    session,
                    event_type=event_type,
                    package_id=package.package_id,
                    evidence={
                        "package_type": package.package_type.value,
                        "version": package.version,
                        "checksum": package.checksum,
                        "validation_status": package.validation_status,
                        "validation_errors": package.validation_errors,
                    },
                )
        return results

    async def list_packages(
        self,
        *,
        package_type: PackageType | None = None,
        package_id: str | None = None,
        version: str | None = None,
        enabled: bool | None = None,
        validation_status: str | None = None,
        capability: str | None = None,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        statement = select(EquipmentPackageRecord)
        if package_type is not None:
            statement = statement.where(EquipmentPackageRecord.package_type == package_type.value)
        if package_id is not None:
            statement = statement.where(EquipmentPackageRecord.package_id == package_id)
        if version is not None:
            statement = statement.where(EquipmentPackageRecord.version == version)
        if enabled is not None:
            statement = statement.where(EquipmentPackageRecord.enabled == enabled)
        if validation_status is not None:
            statement = statement.where(
                EquipmentPackageRecord.validation_status == validation_status
            )
        statement = statement.order_by(
            EquipmentPackageRecord.package_type,
            EquipmentPackageRecord.package_id,
            EquipmentPackageRecord.version,
        )
        async with self.session_factory() as session:
            rows = [self.package_dict(row) for row in (await session.scalars(statement)).all()]
        if capability:
            rows = [row for row in rows if capability in self._capabilities(row)]
        if tag:
            rows = [row for row in rows if tag in row["manifest"].get("tags", [])]
        return rows

    async def get_package(
        self,
        package_type: PackageType,
        package_id: str,
        version: str | None = None,
    ) -> dict[str, Any]:
        rows = await self.list_packages(
            package_type=package_type,
            package_id=package_id,
            version=version,
        )
        if not rows:
            raise LookupError(f"{package_type.value} package {package_id} not found")
        return rows[-1]

    async def set_package_enabled(
        self, package_type: PackageType, package_id: str, enabled: bool
    ) -> dict[str, Any]:
        async with self.session_factory.begin() as session:
            rows = list(
                (
                    await session.scalars(
                        select(EquipmentPackageRecord).where(
                            EquipmentPackageRecord.package_type == package_type.value,
                            EquipmentPackageRecord.package_id == package_id,
                        )
                    )
                ).all()
            )
            if not rows:
                raise LookupError(f"{package_type.value} package {package_id} not found")
            for row in rows:
                if enabled and row.validation_status != "valid":
                    raise ValueError(f"package {package_id} has not passed validation")
                row.enabled = enabled
                row.loaded_at = datetime.now(UTC) if enabled else row.loaded_at
                await self._audit(
                    session,
                    event_type="equipment_loaded" if enabled else "equipment_disabled",
                    package_id=package_id,
                    evidence={
                        "package_type": package_type.value,
                        "version": row.version,
                        "checksum": row.checksum,
                    },
                )
            return self.package_dict(rows[-1])

    async def create_provider_instance(
        self,
        payload: ProviderInstanceCreate,
        package: dict[str, Any],
    ) -> dict[str, Any]:
        config_revision = _checksum_json(payload.config)
        secret_revision = _checksum_json(payload.secret_refs)
        async with self.session_factory.begin() as session:
            existing = await session.scalar(
                select(ProviderInstanceRecord).where(
                    ProviderInstanceRecord.instance_id == payload.instance_id,
                    ProviderInstanceRecord.config_revision == config_revision,
                    ProviderInstanceRecord.secret_binding_revision == secret_revision,
                )
            )
            if existing is not None:
                return self.instance_dict(existing)
            previous = list(
                (
                    await session.scalars(
                        select(ProviderInstanceRecord).where(
                            ProviderInstanceRecord.instance_id == payload.instance_id
                        )
                    )
                ).all()
            )
            for row in previous:
                row.enabled = False
            record = ProviderInstanceRecord(
                id=str(uuid4()),
                instance_id=payload.instance_id,
                provider_package_id=payload.provider_package_id,
                provider_version=payload.provider_version,
                package_checksum=str(package["checksum"]),
                display_name=payload.display_name,
                environment=payload.environment,
                config_revision=config_revision,
                config_json=payload.config,
                secret_binding_revision=secret_revision,
                secret_refs_json=payload.secret_refs,
                allowed_hosts_json=payload.allowed_hosts,
                enabled=payload.enabled,
                health_status="unknown",
            )
            session.add(record)
            await session.flush()
            await self._audit(
                session,
                event_type="provider_instance_revision_created",
                package_id=payload.provider_package_id,
                evidence={
                    "instance_id": payload.instance_id,
                    "config_revision": config_revision,
                    "secret_binding_revision": secret_revision,
                },
            )
            return self.instance_dict(record)

    async def list_provider_instances(
        self,
        *,
        instance_id: str | None = None,
        environment: str | None = None,
        health_status: str | None = None,
        enabled: bool | None = None,
        include_history: bool = False,
    ) -> list[dict[str, Any]]:
        statement = select(ProviderInstanceRecord)
        if instance_id:
            statement = statement.where(ProviderInstanceRecord.instance_id == instance_id)
        if environment:
            statement = statement.where(ProviderInstanceRecord.environment == environment)
        if health_status:
            statement = statement.where(ProviderInstanceRecord.health_status == health_status)
        if enabled is not None:
            statement = statement.where(ProviderInstanceRecord.enabled == enabled)
        statement = statement.order_by(
            ProviderInstanceRecord.instance_id,
            ProviderInstanceRecord.updated_at.desc(),
            ProviderInstanceRecord.created_at.desc(),
        )
        async with self.session_factory() as session:
            records = list((await session.scalars(statement)).all())
        if not include_history:
            latest: dict[str, ProviderInstanceRecord] = {}
            for record in records:
                latest.setdefault(record.instance_id, record)
            records = list(latest.values())
        return [self.instance_dict(record) for record in records]

    async def get_provider_instance(self, instance_id: str) -> dict[str, Any]:
        instances = await self.list_provider_instances(instance_id=instance_id)
        if not instances:
            raise LookupError(f"provider instance {instance_id} not found")
        return instances[0]

    async def set_instance_enabled(self, instance_id: str, enabled: bool) -> dict[str, Any]:
        async with self.session_factory.begin() as session:
            record = await session.scalar(
                select(ProviderInstanceRecord)
                .where(ProviderInstanceRecord.instance_id == instance_id)
                .order_by(
                    ProviderInstanceRecord.updated_at.desc(),
                    ProviderInstanceRecord.created_at.desc(),
                )
            )
            if record is None:
                raise LookupError(f"provider instance {instance_id} not found")
            package = await session.scalar(
                select(EquipmentPackageRecord).where(
                    EquipmentPackageRecord.package_type == PackageType.provider.value,
                    EquipmentPackageRecord.package_id == record.provider_package_id,
                    EquipmentPackageRecord.version == record.provider_version,
                )
            )
            if enabled and (
                package is None
                or not package.enabled
                or package.checksum != record.package_checksum
            ):
                raise ValueError("provider package must be enabled with the bound checksum")
            record.enabled = enabled
            record.updated_at = datetime.now(UTC)
            return self.instance_dict(record)

    async def update_health(self, instance_id: str, status: str) -> None:
        async with self.session_factory.begin() as session:
            record = await session.scalar(
                select(ProviderInstanceRecord)
                .where(ProviderInstanceRecord.instance_id == instance_id)
                .order_by(ProviderInstanceRecord.updated_at.desc())
            )
            if record is None:
                raise LookupError(f"provider instance {instance_id} not found")
            record.health_status = status
            record.updated_at = datetime.now(UTC)

    async def begin_execution(self, values: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        operation_id = str(values["operation_id"])
        async with self.session_factory.begin() as session:
            existing = await session.scalar(
                select(EquipmentExecutionRecord).where(
                    EquipmentExecutionRecord.operation_id == operation_id
                )
            )
            if existing is not None:
                return self.execution_dict(existing), False
            record = EquipmentExecutionRecord(
                id=str(uuid4()),
                run_id=values.get("run_id"),
                step_id=values.get("step_id"),
                operation_id=operation_id,
                package_id=str(values["package_id"]),
                package_version=str(values["package_version"]),
                package_checksum=str(values["package_checksum"]),
                provider_instance_id=values.get("provider_instance_id"),
                config_revision=values.get("config_revision"),
                capability=values.get("capability"),
                capability_contract_checksum=values.get("contract_checksum"),
                test_principal_ref=str(values["test_principal_ref"]),
                status="running",
                physical_attempts=0,
                input_summary_json=values.get("input_summary", {}),
                output_summary_json={},
                evidence_json=[],
            )
            session.add(record)
            return self.execution_dict(record), True

    async def complete_execution(
        self,
        operation_id: str,
        *,
        status: str,
        output_summary: dict[str, Any],
        evidence: list[dict[str, Any]],
        physical_attempts: int,
        error_code: str | None,
    ) -> dict[str, Any]:
        async with self.session_factory.begin() as session:
            record = await session.scalar(
                select(EquipmentExecutionRecord).where(
                    EquipmentExecutionRecord.operation_id == operation_id
                )
            )
            if record is None:
                raise LookupError(f"execution {operation_id} not found")
            record.status = status
            record.output_summary_json = output_summary
            record.evidence_json = evidence
            record.physical_attempts = physical_attempts
            record.error_code = error_code
            record.completed_at = datetime.now(UTC)
            if record.run_id is not None:
                sequence = (
                    await session.scalar(
                        select(func.max(EventRecord.sequence)).where(
                            EventRecord.run_id == record.run_id
                        )
                    )
                    or 0
                ) + 1
                session.add(
                    EventRecord(
                        id=str(uuid4()),
                        run_id=record.run_id,
                        step_id=record.step_id,
                        sequence=sequence,
                        operation_id=f"{operation_id}:event",
                        event_type=(
                            evidence[0].get("event_type", "equipment_execution_completed")
                            if evidence
                            else "equipment_execution_completed"
                        ),
                        evidence_json={
                            "package_id": record.package_id,
                            "package_version": record.package_version,
                            "package_checksum": record.package_checksum,
                            "provider_instance_id": record.provider_instance_id,
                            "config_revision": record.config_revision,
                            "capability": record.capability,
                            "test_principal_ref": record.test_principal_ref,
                            "status": status,
                            "physical_attempts": physical_attempts,
                            "input_summary": record.input_summary_json,
                            "output_summary": output_summary,
                            "error_code": error_code,
                            "redacted": True,
                        },
                    )
                )
            else:
                await self._audit(
                    session,
                    event_type=(
                        evidence[0].get("event_type", "equipment_execution_completed")
                        if evidence
                        else "equipment_execution_completed"
                    ),
                    package_id=record.package_id,
                    evidence={
                        "operation_id": operation_id,
                        "status": status,
                        "physical_attempts": physical_attempts,
                        "input_summary": record.input_summary_json,
                        "output_summary": output_summary,
                        "error_code": error_code,
                        "redacted": True,
                    },
                )
            return self.execution_dict(record)

    async def create_leases(
        self,
        *,
        run_id: str | None,
        operation_id: str,
        provider_instance_id: str,
        leases: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        records: list[ResourceLeaseRecord] = []
        async with self.session_factory.begin() as session:
            for lease in leases:
                record = ResourceLeaseRecord(
                    id=str(uuid4()),
                    run_id=run_id,
                    created_by_operation_id=operation_id,
                    provider_instance_id=provider_instance_id,
                    resource_type=str(lease["resource_type"]),
                    external_resource_id=str(lease["external_resource_id"]),
                    cleanup_contract=str(lease["cleanup_contract"]),
                    cleanup_payload_ref=dict(lease.get("cleanup_payload", {})),
                    status="active",
                )
                session.add(record)
                records.append(record)
        return [self.lease_dict(record) for record in records]

    async def list_active_leases(self, run_id: str | None) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            records = (
                await session.scalars(
                    select(ResourceLeaseRecord).where(
                        ResourceLeaseRecord.run_id == run_id,
                        ResourceLeaseRecord.status.in_(["active", "cleanup_failed"]),
                    )
                )
            ).all()
        return [self.lease_dict(record) for record in records]

    async def mark_lease_cleaned(self, lease_id: str, operation_id: str) -> None:
        async with self.session_factory.begin() as session:
            lease = await session.get(ResourceLeaseRecord, lease_id)
            if lease is None:
                raise LookupError(f"resource lease {lease_id} not found")
            lease.status = "cleaned"
            lease.last_cleanup_operation_id = operation_id
            lease.cleaned_at = datetime.now(UTC)
            await self._record_cleanup_event(
                session,
                lease,
                operation_id=operation_id,
                event_type="cleanup_completed",
            )

    async def mark_lease_cleanup_failed(self, lease_id: str, operation_id: str, error: str) -> None:
        async with self.session_factory.begin() as session:
            lease = await session.get(ResourceLeaseRecord, lease_id)
            if lease is None:
                raise LookupError(f"resource lease {lease_id} not found")
            lease.status = "cleanup_failed"
            lease.last_cleanup_operation_id = operation_id
            await self._record_cleanup_event(
                session,
                lease,
                operation_id=operation_id,
                event_type="cleanup_failed",
                error=error,
            )

    async def save_snapshot(self, values: dict[str, Any]) -> dict[str, Any]:
        async with self.session_factory.begin() as session:
            record = RunEquipmentSnapshotRecord(id=str(uuid4()), **values)
            session.add(record)
            await session.flush()
            return self.snapshot_dict(record)

    async def list_snapshots(self, run_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            records = (
                await session.scalars(
                    select(RunEquipmentSnapshotRecord)
                    .where(RunEquipmentSnapshotRecord.run_id == run_id)
                    .order_by(
                        RunEquipmentSnapshotRecord.package_type,
                        RunEquipmentSnapshotRecord.package_id,
                    )
                )
            ).all()
        return [self.snapshot_dict(record) for record in records]

    @staticmethod
    def package_dict(record: EquipmentPackageRecord) -> dict[str, Any]:
        return {
            "package_type": record.package_type,
            "package_id": record.package_id,
            "version": record.version,
            "source_path": record.source_path,
            "checksum": record.checksum,
            "manifest": record.manifest_json,
            "trust_level": record.trust_level,
            "enabled": record.enabled,
            "validation_status": record.validation_status,
            "validation_errors": record.validation_errors_json,
            "discovered_at": record.discovered_at.isoformat(),
            "loaded_at": record.loaded_at.isoformat() if record.loaded_at else None,
        }

    @staticmethod
    def instance_dict(record: ProviderInstanceRecord) -> dict[str, Any]:
        return {
            "instance_id": record.instance_id,
            "provider_package_id": record.provider_package_id,
            "provider_version": record.provider_version,
            "package_checksum": record.package_checksum,
            "display_name": record.display_name,
            "environment": record.environment,
            "config_revision": record.config_revision,
            "config": record.config_json,
            "secret_binding_revision": record.secret_binding_revision,
            "secret_refs": record.secret_refs_json,
            "allowed_hosts": record.allowed_hosts_json,
            "enabled": record.enabled,
            "health_status": record.health_status,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        }

    @staticmethod
    def execution_dict(record: EquipmentExecutionRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "run_id": record.run_id,
            "step_id": record.step_id,
            "operation_id": record.operation_id,
            "package_id": record.package_id,
            "package_version": record.package_version,
            "package_checksum": record.package_checksum,
            "provider_instance_id": record.provider_instance_id,
            "config_revision": record.config_revision,
            "capability": record.capability,
            "capability_contract_checksum": record.capability_contract_checksum,
            "test_principal_ref": record.test_principal_ref,
            "status": record.status,
            "physical_attempts": record.physical_attempts,
            "input_summary": record.input_summary_json,
            "output_summary": record.output_summary_json,
            "evidence": record.evidence_json,
            "error_code": record.error_code,
        }

    @staticmethod
    def lease_dict(record: ResourceLeaseRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "run_id": record.run_id,
            "created_by_operation_id": record.created_by_operation_id,
            "provider_instance_id": record.provider_instance_id,
            "resource_type": record.resource_type,
            "external_resource_id": record.external_resource_id,
            "cleanup_contract": record.cleanup_contract,
            "cleanup_payload": record.cleanup_payload_ref,
            "status": record.status,
            "last_cleanup_operation_id": record.last_cleanup_operation_id,
        }

    @staticmethod
    def snapshot_dict(record: RunEquipmentSnapshotRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "run_id": record.run_id,
            "package_type": record.package_type,
            "package_id": record.package_id,
            "version": record.version,
            "checksum": record.checksum,
            "manifest": record.manifest_json,
            "provider_instance_id": record.provider_instance_id,
            "config_revision": record.config_revision,
            "secret_binding_revision": record.secret_binding_revision,
            "capability_contract_id": record.capability_contract_id,
            "capability_contract_checksum": record.capability_contract_checksum,
            "test_principal_ref": record.test_principal_ref,
            "target_binding_ref": record.target_binding_ref,
        }

    @staticmethod
    def _capabilities(package: dict[str, Any]) -> set[str]:
        manifest = package["manifest"]
        if package["package_type"] == PackageType.provider.value:
            return {str(item["contract"]) for item in manifest.get("implements", [])}
        if package["package_type"] == PackageType.skill.value:
            return {
                str(item["contract"])
                for item in manifest.get("requires", {}).get("capabilities", [])
            }
        if package["package_type"] == PackageType.casepack.value:
            return set(manifest.get("required_capabilities", []))
        return {str(manifest.get("id"))}

    @staticmethod
    async def _audit(
        session: AsyncSession,
        *,
        event_type: str,
        package_id: str | None,
        evidence: dict[str, Any],
    ) -> None:
        session.add(
            EquipmentAuditEventRecord(
                id=str(uuid4()),
                operation_id=f"equipment:{event_type}:{uuid4()}",
                event_type=event_type,
                package_id=package_id,
                evidence_json=evidence,
            )
        )

    @classmethod
    async def _record_cleanup_event(
        cls,
        session: AsyncSession,
        lease: ResourceLeaseRecord,
        *,
        operation_id: str,
        event_type: str,
        error: str | None = None,
    ) -> None:
        evidence = {
            "resource_lease_id": lease.id,
            "provider_instance_id": lease.provider_instance_id,
            "resource_type": lease.resource_type,
            "external_resource_id": lease.external_resource_id,
            "status": lease.status,
            "error": error,
        }
        if lease.run_id is None:
            await cls._audit(
                session,
                event_type=event_type,
                package_id=None,
                evidence={"operation_id": operation_id, **evidence},
            )
            return
        sequence = (
            await session.scalar(
                select(func.max(EventRecord.sequence)).where(EventRecord.run_id == lease.run_id)
            )
            or 0
        ) + 1
        session.add(
            EventRecord(
                id=str(uuid4()),
                run_id=lease.run_id,
                step_id=None,
                sequence=sequence,
                operation_id=f"{operation_id}:{event_type}:{uuid4()}",
                event_type=event_type,
                evidence_json=evidence,
            )
        )


def _checksum_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
