"""装备 Catalog 的应用服务，负责注册、归档、绑定解析和 Run 快照冻结。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from time import perf_counter
from typing import Any, ClassVar

import yaml

from app.equipment.catalog import EquipmentCatalog
from app.equipment.json_schema import validate_instance
from app.equipment.metrics import EquipmentMetrics
from app.repositories.equipment_repository import EquipmentRepository
from app.schemas.equipment_schema import (
    CapabilityBindingRef,
    DiscoveredPackage,
    PackageType,
    ProviderInstanceCreate,
)


class EquipmentService:
    """把可变部署目录转换为 Run 可引用的不可变装备事实。"""

    _RUN_BINDINGS: ClassVar[dict[str, dict[str, Any]]] = {
        "blackbox": {
            "skill_id": "prompt-injection-evaluator",
            "casepack_id": "attacker-baseline-v1",
            "provider_instance_id": "http-agent-dev",
            "capabilities": ["agent.invoke.v1"],
        },
        "graybox": {
            "skill_id": "tool-policy-trace-evaluator",
            "casepack_id": "attacker-baseline-v1",
            "provider_instance_id": "http-agent-dev",
            "capabilities": ["agent.invoke.v1", "agent.trace.read.v1"],
        },
        "stateful": {
            "skill_id": "state-poisoning-evaluator",
            "casepack_id": "attacker-baseline-v1",
            "provider_instance_id": "isolated-state-default",
            "capabilities": [
                "memory.fixture.write.v1",
                "memory.fixture.read.v1",
                "memory.fixture.cleanup.v1",
                "rag.document.index.v1",
                "rag.retrieval.query.v1",
            ],
        },
    }

    def __init__(
        self,
        repository: EquipmentRepository,
        catalog: EquipmentCatalog,
        metrics: EquipmentMetrics | None = None,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.metrics = metrics

    async def reload(self) -> dict[str, Any]:
        started = perf_counter()
        packages = self.catalog.discover()
        self._archive_packages(packages)
        records = await self.repository.register_packages(packages)
        instances: list[dict[str, Any]] = []
        for package in packages:
            if package.package_type != PackageType.provider or package.validation_status != "valid":
                continue
            instance_file = Path(package.source_path) / "instances.yaml"
            if not instance_file.is_file():
                continue
            loaded = yaml.safe_load(instance_file.read_text(encoding="utf-8")) or []
            if not isinstance(loaded, list):
                raise TypeError(f"{instance_file}: root must be an array")
            for item in loaded:
                instances.append(
                    await self.create_provider_instance(
                        ProviderInstanceCreate.model_validate(
                            {
                                **item,
                                "provider_package_id": package.package_id,
                                "provider_version": package.version,
                            }
                        )
                    )
                )
        result = {
            "packages": records,
            "provider_instances": instances,
            "counts": {
                package_type.value: sum(
                    package.package_type == package_type for package in packages
                )
                for package_type in PackageType
            },
            "invalid_count": sum(package.validation_status == "invalid" for package in packages),
        }
        if self.metrics is not None:
            self.metrics.set_invalid_packages(result["invalid_count"])
            self.metrics.observe(
                "equipment_discovery",
                (perf_counter() - started) * 1000,
            )
            self.metrics.increment("invalid_package", result["invalid_count"])
            self.metrics.increment(
                "enabled_package",
                sum(bool(record.get("enabled")) for record in records),
            )
        return result

    def materialize_package(
        self, package: dict[str, Any], expected_checksum: str | None = None
    ) -> dict[str, Any]:
        checksum = expected_checksum or str(package["checksum"])
        source = Path(package["source_path"])
        if source.is_dir() and self.catalog.checksum_path(source) == checksum:
            return package
        archived = Path(self.catalog.settings.archive_root) / checksum
        if not archived.is_dir() or self.catalog.checksum_path(archived) != checksum:
            raise ValueError(
                f"package content unavailable for {package['package_id']} checksum {checksum}"
            )
        return {**package, "source_path": str(archived.resolve())}

    def _archive_packages(self, packages: list[DiscoveredPackage]) -> None:
        archive_root = Path(self.catalog.settings.archive_root)
        archive_root.mkdir(parents=True, exist_ok=True)
        for package in packages:
            if package.validation_status != "valid":
                continue
            destination = archive_root / package.checksum
            if destination.exists():
                if self.catalog.checksum_path(destination) != package.checksum:
                    raise ValueError(f"equipment archive checksum conflict: {package.checksum}")
                continue
            shutil.copytree(
                package.source_path,
                destination,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )

    async def validate_package(self, package_type: PackageType, package_id: str) -> dict[str, Any]:
        package = await self.repository.get_package(package_type, package_id)
        discovered = self.catalog.validate_path(Path(package["source_path"]), package_type)
        results = await self.repository.register_packages([discovered])
        return results[0]

    async def create_provider_instance(self, payload: ProviderInstanceCreate) -> dict[str, Any]:
        package = await self.repository.get_package(
            PackageType.provider,
            payload.provider_package_id,
            payload.provider_version,
        )
        if package["validation_status"] != "valid":
            raise ValueError("provider package has not passed validation")
        if package["enabled"] is False and payload.enabled:
            raise ValueError("provider package must be enabled first")
        schema_path = (
            Path(package["source_path"]) / package["manifest"]["configuration"]["schema_file"]
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validate_instance(payload.config, schema)
        manifest_hosts = set(
            package["manifest"].get("runtime", {}).get("network", {}).get("allowed_hosts", [])
        )
        if not set(payload.allowed_hosts).issubset(manifest_hosts):
            raise ValueError("instance allowed_hosts exceed the Provider manifest")
        return await self.repository.create_provider_instance(payload, package)

    async def freeze_run_bindings(
        self,
        *,
        run_id: str,
        stage: str,
        target_binding_ref: str,
        test_principal_ref: str,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """冻结 Skill、Case Pack、Provider、Contract、Instance 与身份绑定。"""

        try:
            binding_spec = self._RUN_BINDINGS[stage]
        except KeyError as exc:
            raise ValueError(f"unsupported equipment binding stage: {stage}") from exc

        overrides = overrides or {}
        skill = await self._selected_package(
            PackageType.skill,
            str(binding_spec["skill_id"]),
            overrides,
        )
        casepack = await self._selected_package(
            PackageType.casepack,
            str(binding_spec["casepack_id"]),
            overrides,
        )
        default_provider_package_id = (
            "isolated-state-provider" if stage == "stateful" else "http-agent-provider"
        )
        provider_override = overrides.get(default_provider_package_id, {})
        instance = await self.repository.get_provider_instance(
            str(
                provider_override.get("provider_instance_id")
                or binding_spec["provider_instance_id"]
            )
        )
        if not instance["enabled"]:
            raise ValueError(f"provider instance {instance['instance_id']} is not enabled")
        provider = await self._selected_package(
            PackageType.provider,
            str(instance["provider_package_id"]),
            overrides,
            default_version=str(instance["provider_version"]),
        )
        if (
            provider["version"] != instance["provider_version"]
            or provider["checksum"] != instance["package_checksum"]
        ):
            raise ValueError("Provider Instance package checksum does not match the catalog")

        snapshot_values = [
            self._package_snapshot(
                run_id=run_id,
                package=package,
                target_binding_ref=target_binding_ref,
                test_principal_ref=test_principal_ref,
            )
            for package in (skill, casepack)
        ]
        snapshot_values.append(
            {
                **self._package_snapshot(
                    run_id=run_id,
                    package=provider,
                    target_binding_ref=target_binding_ref,
                    test_principal_ref=test_principal_ref,
                ),
                "provider_instance_id": instance["instance_id"],
                "config_revision": instance["config_revision"],
                "config_json": instance["config"],
                "secret_binding_revision": instance["secret_binding_revision"],
            }
        )

        implemented = {str(item["contract"]) for item in provider["manifest"].get("implements", [])}
        for capability in binding_spec["capabilities"]:
            if capability not in implemented:
                raise ValueError(
                    f"Provider {provider['package_id']} does not implement {capability}"
                )
            contract = await self._selected_package(
                PackageType.contract,
                capability,
                overrides,
            )
            snapshot_values.append(
                {
                    **self._package_snapshot(
                        run_id=run_id,
                        package=contract,
                        target_binding_ref=target_binding_ref,
                        test_principal_ref=test_principal_ref,
                    ),
                    "provider_instance_id": instance["instance_id"],
                    "capability_contract_id": capability,
                    "capability_contract_checksum": contract["checksum"],
                }
            )
        return await self.repository.save_snapshots(snapshot_values)

    async def _selected_package(
        self,
        package_type: PackageType,
        package_id: str,
        overrides: dict[str, dict[str, Any]],
        *,
        default_version: str | None = None,
    ) -> dict[str, Any]:
        selection = overrides.get(package_id, {})
        package = await self._enabled_package(
            package_type,
            package_id,
            str(selection["version"]) if selection.get("version") is not None else default_version,
        )
        expected_checksum = selection.get("checksum")
        if expected_checksum is not None and package["checksum"] != expected_checksum:
            raise ValueError(f"selected checksum for {package_id} does not match the catalog")
        return package

    async def clone_run_bindings(
        self,
        *,
        source_run_id: str,
        target_run_id: str,
    ) -> list[dict[str, Any]]:
        """Replay 只克隆可按原 checksum 重新物化的历史绑定。"""

        source_snapshots = await self.repository.list_snapshots(source_run_id)
        if not source_snapshots:
            raise ValueError(f"source Run {source_run_id} has no equipment snapshots")
        cloned_values: list[dict[str, Any]] = []
        for snapshot in source_snapshots:
            package = await self.repository.get_package(
                PackageType(snapshot["package_type"]),
                snapshot["package_id"],
                snapshot["version"],
            )
            self.materialize_package(package, snapshot["checksum"])
            cloned_values.append(
                {
                    "run_id": target_run_id,
                    "package_type": snapshot["package_type"],
                    "package_id": snapshot["package_id"],
                    "version": snapshot["version"],
                    "checksum": snapshot["checksum"],
                    "manifest_json": snapshot["manifest"],
                    "provider_instance_id": snapshot["provider_instance_id"],
                    "config_revision": snapshot["config_revision"],
                    "config_json": snapshot["config"],
                    "secret_binding_revision": snapshot["secret_binding_revision"],
                    "capability_contract_id": snapshot["capability_contract_id"],
                    "capability_contract_checksum": snapshot["capability_contract_checksum"],
                    "test_principal_ref": snapshot["test_principal_ref"],
                    "target_binding_ref": snapshot["target_binding_ref"],
                }
            )
        return await self.repository.save_snapshots(cloned_values)

    async def _enabled_package(
        self,
        package_type: PackageType,
        package_id: str,
        version: str | None = None,
    ) -> dict[str, Any]:
        package = await self.repository.get_package(package_type, package_id, version)
        if package["validation_status"] != "valid" or not package["enabled"]:
            raise ValueError(f"{package_type.value} package {package_id} is not valid and enabled")
        return self.materialize_package(package)

    @staticmethod
    def _package_snapshot(
        *,
        run_id: str,
        package: dict[str, Any],
        target_binding_ref: str,
        test_principal_ref: str,
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "package_type": package["package_type"],
            "package_id": package["package_id"],
            "version": package["version"],
            "checksum": package["checksum"],
            "manifest_json": package["manifest"],
            "provider_instance_id": None,
            "config_revision": None,
            "config_json": None,
            "secret_binding_revision": None,
            "capability_contract_id": None,
            "capability_contract_checksum": None,
            "test_principal_ref": test_principal_ref,
            "target_binding_ref": target_binding_ref,
        }

    async def resolve(
        self,
        capability: str,
        *,
        explicit_instance_id: str | None = None,
        policy_default_instance_id: str | None = None,
        environment: str | None = None,
    ) -> CapabilityBindingRef:
        preferred = explicit_instance_id or policy_default_instance_id
        if preferred:
            instances = await self.repository.list_provider_instances(
                instance_id=preferred, enabled=True
            )
        else:
            instances = await self.repository.list_provider_instances(
                environment=environment, enabled=True
            )
        candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for instance in instances:
            if instance["health_status"] not in {"healthy", "unknown"}:
                continue
            package = await self.repository.get_package(
                PackageType.provider,
                instance["provider_package_id"],
                instance["provider_version"],
            )
            implements = {item["contract"] for item in package["manifest"].get("implements", [])}
            if (
                capability in implements
                and package["enabled"]
                and package["checksum"] == instance["package_checksum"]
            ):
                candidates.append((instance, package))
        if not candidates:
            raise LookupError(f"no enabled Provider Instance implements {capability}")
        if len(candidates) > 1:
            raise ValueError(
                f"ambiguous capability binding for {capability}: "
                f"{sorted(item[0]['instance_id'] for item in candidates)}"
            )
        instance, package = candidates[0]
        contract = await self.repository.get_package(PackageType.contract, capability)
        return CapabilityBindingRef(
            capability=capability,
            provider_instance_id=instance["instance_id"],
            provider_package_id=package["package_id"],
            provider_version=package["version"],
            provider_checksum=package["checksum"],
            config_revision=instance["config_revision"],
            secret_binding_revision=instance["secret_binding_revision"],
            contract_checksum=contract["checksum"],
        )
