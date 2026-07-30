from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import yaml

from app.equipment.catalog import EquipmentCatalog
from app.equipment.json_schema import validate_instance
from app.repositories.equipment_repository import EquipmentRepository
from app.schemas.equipment_schema import (
    CapabilityBindingRef,
    DiscoveredPackage,
    PackageType,
    ProviderInstanceCreate,
)


class EquipmentService:
    def __init__(
        self,
        repository: EquipmentRepository,
        catalog: EquipmentCatalog,
    ) -> None:
        self.repository = repository
        self.catalog = catalog

    async def reload(self) -> dict[str, Any]:
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
        return {
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
            shutil.copytree(package.source_path, destination)

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
