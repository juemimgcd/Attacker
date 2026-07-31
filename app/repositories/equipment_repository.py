"""装备包、Instance、Run 快照、Execution、Resource Lease 与审计事件仓库。"""

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
    EvaluationRunRecord,
    EventRecord,
    PolicySnapshotRecord,
    ProviderInstanceRecord,
    ResourceLeaseRecord,
    RunEquipmentSnapshotRecord,
    TargetRecord,
)
from app.schemas.equipment_schema import DiscoveredPackage, PackageType, ProviderInstanceCreate


class EquipmentRepository:
    """用数据库唯一约束保证版本不可变、operation 幂等和租约清理可恢复。"""

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
                    event_type = "equipment_validation_failed"
                    results.append(
                        {
                            **package.model_dump(mode="json"),
                            "error_code": "immutable_version_conflict",
                        }
                    )
                else:
                    record = existing or EquipmentPackageRecord(
                        id=str(uuid4()),
                        package_type=package.package_type.value,
                        package_id=package.package_id,
                        name=str(package.manifest.get("name", package.package_id)),
                        version=package.version,
                        source_path=package.source_path,
                        source_type=package.source_type,
                        source_ref=package.source_ref,
                        checksum=package.checksum,
                        manifest_json=package.manifest,
                        trust_level=str(package.manifest.get("trust_level", "trusted_enterprise")),
                        signature_status=package.signature_status,
                        publisher_id=package.publisher_id,
                        enabled=(
                            package.validation_status == "valid"
                            and package.source_type == "builtin"
                            and package.manifest.get("trust_level") == "trusted_builtin"
                        ),
                        validation_status=package.validation_status,
                        validation_errors_json=package.validation_errors,
                    )
                    record.source_path = package.source_path
                    record.name = str(package.manifest.get("name", package.package_id))
                    record.source_type = package.source_type
                    record.source_ref = package.source_ref
                    record.manifest_json = package.manifest
                    record.trust_level = str(
                        package.manifest.get("trust_level", "trusted_enterprise")
                    )
                    record.validation_status = package.validation_status
                    record.validation_errors_json = package.validation_errors
                    record.signature_status = package.signature_status
                    record.publisher_id = package.publisher_id
                    if package.validation_status != "valid":
                        record.enabled = False
                        event_type = "equipment_validation_failed"
                    elif record.enabled:
                        record.loaded_at = datetime.now(UTC)
                    elif (
                        record.trust_level == "trusted_builtin" and record.source_type == "builtin"
                    ):
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
                    ProviderInstanceRecord.provider_package_id == payload.provider_package_id,
                    ProviderInstanceRecord.provider_version == payload.provider_version,
                    ProviderInstanceRecord.package_checksum == package["checksum"],
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

    async def get_provider_instance_revision(
        self,
        instance_id: str,
        package_checksum: str,
        config_revision: str,
        secret_binding_revision: str,
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            record = await session.scalar(
                select(ProviderInstanceRecord).where(
                    ProviderInstanceRecord.instance_id == instance_id,
                    ProviderInstanceRecord.package_checksum == package_checksum,
                    ProviderInstanceRecord.config_revision == config_revision,
                    ProviderInstanceRecord.secret_binding_revision == secret_binding_revision,
                )
            )
        if record is None:
            raise LookupError(
                f"provider instance revision {instance_id}/{package_checksum}/"
                f"{config_revision}/"
                f"{secret_binding_revision} not found"
            )
        return self.instance_dict(record)

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
        """按 operation_id 和请求指纹开始执行；身份不明的重复项返回 in_doubt。"""

        operation_id = str(values["operation_id"])
        async with self.session_factory.begin() as session:
            existing = await session.scalar(
                select(EquipmentExecutionRecord).where(
                    EquipmentExecutionRecord.operation_id == operation_id
                )
            )
            if existing is not None:
                expected = {
                    "run_id": values.get("run_id"),
                    "step_id": values.get("step_id"),
                    "package_id": str(values["package_id"]),
                    "package_version": str(values["package_version"]),
                    "package_checksum": str(values["package_checksum"]),
                    "provider_instance_id": values.get("provider_instance_id"),
                    "config_revision": values.get("config_revision"),
                    "capability": values.get("capability"),
                    "capability_contract_checksum": values.get("contract_checksum"),
                    "test_principal_ref": str(values["test_principal_ref"]),
                }
                if existing.request_fingerprint is not None:
                    expected["secret_binding_revision"] = values.get("secret_binding_revision")
                actual = {key: getattr(existing, key) for key in expected}
                if actual != expected:
                    raise ValueError("operation_id was reused with different execution facts")
                persisted = self.execution_dict(existing)
                if existing.request_fingerprint is None:
                    persisted.update(
                        {
                            "status": "in_doubt",
                            "error_code": "equipment_legacy_execution_in_doubt",
                            "result": None,
                            "output": {},
                            "legacy_result_unavailable": True,
                        }
                    )
                elif existing.request_fingerprint != str(values["request_fingerprint"]):
                    raise ValueError("operation_id was reused with different execution facts")
                elif existing.status == "running":
                    persisted["status"] = "in_doubt"
                    persisted["error_code"] = "equipment_execution_in_doubt"
                lease_records = (
                    await session.scalars(
                        select(ResourceLeaseRecord).where(
                            ResourceLeaseRecord.created_by_operation_id == operation_id
                        )
                    )
                ).all()
                if lease_records:
                    persisted["resource_leases"] = [
                        self.lease_dict(lease) for lease in lease_records
                    ]
                return persisted, False
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
                secret_binding_revision=values.get("secret_binding_revision"),
                capability=values.get("capability"),
                capability_contract_checksum=values.get("contract_checksum"),
                test_principal_ref=str(values["test_principal_ref"]),
                status="running",
                physical_attempts=0,
                request_fingerprint=str(values["request_fingerprint"]),
                input_summary_json=values.get("input_summary", {}),
                output_summary_json={},
                result_json={},
                evidence_json=[],
            )
            session.add(record)
            return self.execution_dict(record), True

    async def complete_execution(
        self,
        operation_id: str,
        *,
        status: str,
        result: dict[str, Any],
        output_summary: dict[str, Any],
        evidence: list[dict[str, Any]],
        physical_attempts: int,
        error_code: str | None,
        leases: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """在同一事务提交执行结果和 Resource Lease，避免资源存在而清理事实丢失。"""

        async with self.session_factory.begin() as session:
            record = await session.scalar(
                select(EquipmentExecutionRecord).where(
                    EquipmentExecutionRecord.operation_id == operation_id
                )
            )
            if record is None:
                raise LookupError(f"execution {operation_id} not found")
            record.status = status
            record.result_json = result
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
                            "secret_binding_revision": record.secret_binding_revision,
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
            lease_records = await self._create_lease_records(
                session,
                run_id=record.run_id,
                operation_id=operation_id,
                provider_instance_id=record.provider_instance_id,
                leases=leases or [],
            )
            await session.flush()
            completed = self.execution_dict(record)
            if lease_records:
                completed["resource_leases"] = [self.lease_dict(lease) for lease in lease_records]
            return completed

    async def get_execution(self, operation_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            record = await session.scalar(
                select(EquipmentExecutionRecord).where(
                    EquipmentExecutionRecord.operation_id == operation_id
                )
            )
            if record is None:
                raise LookupError(f"execution {operation_id} not found")
            result = self.execution_dict(record)
            lease_records = (
                await session.scalars(
                    select(ResourceLeaseRecord).where(
                        ResourceLeaseRecord.created_by_operation_id == operation_id
                    )
                )
            ).all()
            if lease_records:
                result["resource_leases"] = [self.lease_dict(lease) for lease in lease_records]
            return result

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

    async def list_pending_leases(self) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            records = (
                await session.scalars(
                    select(ResourceLeaseRecord)
                    .where(ResourceLeaseRecord.status.in_(["active", "cleanup_failed"]))
                    .order_by(
                        ResourceLeaseRecord.run_id,
                        ResourceLeaseRecord.created_at,
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
            lease.cleanup_attempts += 1
            lease.cleanup_error = None
            lease.cleaned_at = datetime.now(UTC)
            await self._record_cleanup_event(
                session,
                lease,
                operation_id=operation_id,
                event_type="cleanup_completed",
            )

    async def mark_lease_cleanup_started(
        self,
        lease_id: str,
        operation_id: str,
    ) -> None:
        async with self.session_factory.begin() as session:
            lease = await session.get(ResourceLeaseRecord, lease_id)
            if lease is None:
                raise LookupError(f"resource lease {lease_id} not found")
            lease.last_cleanup_operation_id = operation_id
            await self._record_cleanup_event(
                session,
                lease,
                operation_id=operation_id,
                event_type="cleanup_started",
            )

    async def mark_lease_cleanup_failed(self, lease_id: str, operation_id: str, error: str) -> None:
        async with self.session_factory.begin() as session:
            lease = await session.get(ResourceLeaseRecord, lease_id)
            if lease is None:
                raise LookupError(f"resource lease {lease_id} not found")
            lease.status = "cleanup_failed"
            lease.last_cleanup_operation_id = operation_id
            lease.cleanup_attempts += 1
            lease.cleanup_error = error
            await self._record_cleanup_event(
                session,
                lease,
                operation_id=operation_id,
                event_type="cleanup_failed",
                error=error,
            )

    async def save_snapshot(self, values: dict[str, Any]) -> dict[str, Any]:
        snapshots = await self.save_snapshots([values])
        return snapshots[0]

    async def save_snapshots(self, values_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        async with self.session_factory.begin() as session:
            for raw_values in values_list:
                values = {
                    **raw_values,
                    "binding_key": str(raw_values.get("provider_instance_id") or ""),
                }
                existing = await session.scalar(
                    select(RunEquipmentSnapshotRecord).where(
                        RunEquipmentSnapshotRecord.run_id == values["run_id"],
                        RunEquipmentSnapshotRecord.package_type == values["package_type"],
                        RunEquipmentSnapshotRecord.package_id == values["package_id"],
                        RunEquipmentSnapshotRecord.binding_key == values["binding_key"],
                    )
                )
                if existing is not None:
                    self._assert_snapshot_immutable(existing, values)
                    results.append(self.snapshot_dict(existing))
                    continue
                record = RunEquipmentSnapshotRecord(id=str(uuid4()), **values)
                session.add(record)
                await session.flush()
                results.append(self.snapshot_dict(record))
            await session.flush()
        return results

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

    async def get_run_binding_facts(self, run_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            run = await session.get(EvaluationRunRecord, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            target = await session.get(TargetRecord, run.target_id)
            policy = await session.get(PolicySnapshotRecord, run.policy_id)
            started = await session.scalar(
                select(EventRecord).where(
                    EventRecord.run_id == run_id,
                    EventRecord.event_type == "run_started",
                )
            )
            snapshots = (
                await session.scalars(
                    select(RunEquipmentSnapshotRecord).where(
                        RunEquipmentSnapshotRecord.run_id == run_id
                    )
                )
            ).all()
        event_principals = (
            started.evidence_json.get("test_principal_refs", []) if started is not None else []
        )
        snapshot_principals = {
            snapshot.test_principal_ref
            for snapshot in snapshots
            if snapshot.test_principal_ref is not None
        }
        return {
            "target_endpoint": target.endpoint if target is not None else None,
            "target_config": target.config_json if target is not None else None,
            "policy": policy.config_json if policy is not None else None,
            "test_principal_refs": sorted(
                {str(item) for item in event_principals} or snapshot_principals
            ),
            "equipment_target_refs": sorted(
                {
                    snapshot.target_binding_ref
                    for snapshot in snapshots
                    if snapshot.target_binding_ref is not None
                }
            ),
        }

    async def get_snapshot(
        self,
        run_id: str,
        package_type: PackageType,
        package_id: str,
        *,
        provider_instance_id: str | None = None,
    ) -> dict[str, Any]:
        statement = select(RunEquipmentSnapshotRecord).where(
            RunEquipmentSnapshotRecord.run_id == run_id,
            RunEquipmentSnapshotRecord.package_type == package_type.value,
            RunEquipmentSnapshotRecord.package_id == package_id,
        )
        if provider_instance_id is None:
            statement = statement.where(RunEquipmentSnapshotRecord.provider_instance_id.is_(None))
        else:
            statement = statement.where(
                RunEquipmentSnapshotRecord.provider_instance_id == provider_instance_id
            )
        async with self.session_factory() as session:
            record = await session.scalar(statement)
        if record is None:
            raise LookupError(
                f"{package_type.value} snapshot {package_id} for Run {run_id} not found"
            )
        return self.snapshot_dict(record)

    @staticmethod
    def package_dict(record: EquipmentPackageRecord) -> dict[str, Any]:
        return {
            "package_type": record.package_type,
            "package_id": record.package_id,
            "name": record.name,
            "version": record.version,
            "source_path": record.source_path,
            "source_type": record.source_type,
            "source_ref": record.source_ref,
            "checksum": record.checksum,
            "manifest": record.manifest_json,
            "trust_level": record.trust_level,
            "signature_status": record.signature_status,
            "publisher_id": record.publisher_id,
            "enabled": record.enabled,
            "validation_status": record.validation_status,
            "validation_errors": record.validation_errors_json,
            "discovered_at": record.discovered_at.isoformat(),
            "installed_at": (record.installed_at.isoformat() if record.installed_at else None),
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
        result = record.result_json
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
            "secret_binding_revision": record.secret_binding_revision,
            "capability": record.capability,
            "capability_contract_checksum": record.capability_contract_checksum,
            "test_principal_ref": record.test_principal_ref,
            "status": record.status,
            "physical_attempts": record.physical_attempts,
            "request_fingerprint": record.request_fingerprint,
            "input_summary": record.input_summary_json,
            "output_summary": record.output_summary_json,
            "result": result,
            "output": result.get("output", {}) if result is not None else {},
            "evidence": record.evidence_json,
            "error_code": record.error_code,
            "legacy_result_unavailable": record.request_fingerprint is None,
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
            "cleanup_attempts": record.cleanup_attempts,
            "cleanup_error": record.cleanup_error,
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
            "binding_key": record.binding_key,
            "config_revision": record.config_revision,
            "config": record.config_json,
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
    async def _create_lease_records(
        session: AsyncSession,
        *,
        run_id: str | None,
        operation_id: str,
        provider_instance_id: str | None,
        leases: list[dict[str, Any]],
    ) -> list[ResourceLeaseRecord]:
        """把 Provider 返回的资源声明转换为可恢复清理的数据库租约。"""

        if leases and provider_instance_id is None:
            raise ValueError("Resource Leases require a Provider Instance")
        if leases:
            instance_exists = await session.scalar(
                select(ProviderInstanceRecord.id).where(
                    ProviderInstanceRecord.instance_id == provider_instance_id
                )
            )
            if instance_exists is None:
                raise ValueError("Resource Leases require a persisted Provider Instance")
        records = [
            ResourceLeaseRecord(
                id=str(uuid4()),
                run_id=run_id,
                created_by_operation_id=operation_id,
                provider_instance_id=str(provider_instance_id),
                resource_type=str(lease["resource_type"]),
                external_resource_id=str(lease["external_resource_id"]),
                cleanup_contract=str(lease["cleanup_contract"]),
                cleanup_payload_ref=dict(lease.get("cleanup_payload", {})),
                status="active",
            )
            for lease in leases
        ]
        session.add_all(records)
        return records

    @staticmethod
    def _assert_snapshot_immutable(
        existing: RunEquipmentSnapshotRecord,
        values: dict[str, Any],
    ) -> None:
        expected = {
            "version": values["version"],
            "checksum": values["checksum"],
            "manifest_json": values["manifest_json"],
            "provider_instance_id": values.get("provider_instance_id"),
            "binding_key": values["binding_key"],
            "config_revision": values.get("config_revision"),
            "config_json": values.get("config_json"),
            "secret_binding_revision": values.get("secret_binding_revision"),
            "capability_contract_id": values.get("capability_contract_id"),
            "capability_contract_checksum": values.get("capability_contract_checksum"),
            "test_principal_ref": values.get("test_principal_ref"),
            "target_binding_ref": values.get("target_binding_ref"),
        }
        actual = {key: getattr(existing, key) for key in expected}
        if actual != expected:
            raise ValueError("immutable Run equipment snapshot conflict")

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
        creator = await session.scalar(
            select(EquipmentExecutionRecord).where(
                EquipmentExecutionRecord.operation_id == lease.created_by_operation_id
            )
        )
        event_operation_id = f"{operation_id}:{event_type}:{lease.cleanup_attempts}"
        existing_event = await session.scalar(
            select(EventRecord).where(EventRecord.operation_id == event_operation_id)
        )
        if existing_event is not None:
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
                step_id=creator.step_id if creator is not None else None,
                sequence=sequence,
                operation_id=event_operation_id,
                event_type=event_type,
                evidence_json=evidence,
            )
        )


def _checksum_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
