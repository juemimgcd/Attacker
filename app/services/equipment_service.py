"""装备 Catalog 的应用服务，负责注册、归档、绑定解析和 Run 快照冻结。"""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from typing import Any, ClassVar
from urllib.parse import urlparse
from uuid import uuid4

import yaml

from app.equipment.catalog import EquipmentCatalog
from app.equipment.json_schema import validate_instance
from app.equipment.metrics import EquipmentMetrics
from app.equipment.security import (
    SecretBroker,
    contains_private_key_pem,
    is_sensitive_field,
    normalize_sensitive_key,
    validate_outbound_url,
    validate_secret_reference,
    validate_target_url,
    validate_url_has_no_secrets,
)
from app.repositories.equipment_repository import EquipmentRepository
from app.schemas.equipment_schema import (
    CapabilityBindingRef,
    DiscoveredPackage,
    PackageType,
    ProviderInstanceCreate,
)
from app.schemas.target_schema import TargetConfig

_PROVIDER_SECRET_METADATA_KEYS = {"auth_secret_name"}


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
        *,
        secret_broker: SecretBroker | None = None,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.metrics = metrics
        self.secret_broker = secret_broker

    async def list_packages(
        self,
        *,
        package_type: PackageType,
        package_id: str | None = None,
        version: str | None = None,
        enabled: bool | None = None,
        validation_status: str | None = None,
        capability: str | None = None,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.repository.list_packages(
            package_type=package_type,
            package_id=package_id,
            version=version,
            enabled=enabled,
            validation_status=validation_status,
            capability=capability,
            tag=tag,
        )

    async def get_package(
        self,
        package_type: PackageType,
        package_id: str,
        version: str | None = None,
    ) -> dict[str, Any]:
        return await self.repository.get_package(package_type, package_id, version)

    async def list_provider_instances(
        self,
        *,
        instance_id: str | None = None,
        environment: str | None = None,
        health_status: str | None = None,
        enabled: bool | None = None,
        include_history: bool = False,
    ) -> list[dict[str, Any]]:
        return await self.repository.list_provider_instances(
            instance_id=instance_id,
            environment=environment,
            health_status=health_status,
            enabled=enabled,
            include_history=include_history,
        )

    async def get_provider_instance(self, instance_id: str) -> dict[str, Any]:
        return await self.repository.get_provider_instance(instance_id)

    async def validate_target(self, target: TargetConfig) -> dict[str, Any] | None:
        """把客户端公网意图绑定到服务端管理的 Provider Instance allowlist。"""

        if target.provider_instance_id is not None:
            self._reject_provider_target_credentials(target)
        return await self._validate_target(target, frozen=False)

    async def validate_frozen_target(self, target: TargetConfig) -> dict[str, Any] | None:
        """Validate an exact historical binding without applying new-Run enablement gates."""

        return await self._validate_target(target, frozen=True)

    async def prepare_target(
        self,
        target: TargetConfig,
        *,
        frozen: bool = False,
    ) -> TargetConfig:
        """Validate and pin a Target revision without leasing plaintext credentials."""

        prepared = target.model_copy(deep=True)
        if prepared.provider_instance_id is not None:
            self._reject_provider_target_credentials(prepared)
        await self._validate_target(prepared, frozen=frozen)
        if prepared.provider_instance_id is not None:
            prepared.auth = prepared.auth.model_copy(
                update={
                    "type": "none",
                    "token": None,
                    "header_name": "Authorization",
                    "token_prefix": "Bearer",
                }
            )
        return prepared

    async def _validate_target(
        self,
        target: TargetConfig,
        *,
        frozen: bool,
    ) -> dict[str, Any] | None:

        allowed_hosts: list[str] = []
        instance: dict[str, Any] | None = None
        if target.provider_instance_id is not None:
            expected_revision = (
                target.provider_package_checksum,
                target.provider_config_revision,
                target.provider_secret_binding_revision,
            )
            if any(expected_revision) and not all(expected_revision):
                raise ValueError("Target Provider Instance revision binding is incomplete")
            if frozen and not all(expected_revision):
                raise ValueError("frozen Target Provider Instance revision binding is incomplete")
            try:
                if all(expected_revision):
                    instance = await self.repository.get_provider_instance_revision(
                        target.provider_instance_id,
                        str(target.provider_package_checksum),
                        str(target.provider_config_revision),
                        str(target.provider_secret_binding_revision),
                    )
                else:
                    instance = await self.get_provider_instance(target.provider_instance_id)
                package = await self.get_package(
                    PackageType.provider,
                    str(instance["provider_package_id"]),
                    str(instance["provider_version"]),
                )
            except LookupError as exc:
                raise ValueError(str(exc)) from exc
            if not frozen and not instance["enabled"]:
                raise ValueError(f"provider instance {target.provider_instance_id} is not enabled")
            if (
                (not frozen and not package["enabled"])
                or (not frozen and package["validation_status"] != "valid")
                or package["checksum"] != instance["package_checksum"]
            ):
                raise ValueError(
                    f"provider instance {target.provider_instance_id} package binding is unavailable"
                )
            package = self.materialize_package(package, str(instance["package_checksum"]))
            implemented = {
                str(reference.get("contract"))
                for reference in package["manifest"].get("implements", [])
                if isinstance(reference, dict)
            }
            if "agent.invoke.v1" not in implemented:
                raise ValueError(
                    f"provider instance {target.provider_instance_id} cannot authorize HTTP Targets"
                )
            base_url = str(instance["config"].get("base_url") or "").rstrip("/")
            invoke_path = str(instance["config"].get("invoke_path", "/"))
            if not base_url or not invoke_path.startswith("/") or "://" in invoke_path:
                raise ValueError(
                    f"provider instance {target.provider_instance_id} Target binding is invalid"
                )
            expected_endpoint = str(
                TargetConfig.model_validate(
                    {
                        "name": target.name,
                        "endpoint": f"{base_url}/{invoke_path.lstrip('/')}",
                    }
                ).endpoint
            )
            if str(target.endpoint) != expected_endpoint:
                raise ValueError(
                    f"target endpoint must match provider instance {target.provider_instance_id}"
                )
            allowed_hosts = list(instance["allowed_hosts"])
            if not allowed_hosts:
                raise ValueError(
                    f"provider instance {target.provider_instance_id} has no Target host allowlist"
                )
        elif any(
            (
                target.provider_package_checksum,
                target.provider_config_revision,
                target.provider_secret_binding_revision,
            )
        ):
            raise ValueError("Target Provider Instance revision requires provider_instance_id")
        validate_target_url(
            str(target.endpoint),
            allow_public_target=target.allow_public_target,
            allowed_hosts=allowed_hosts,
        )
        if instance is not None:
            target.provider_package_checksum = str(instance["package_checksum"])
            target.provider_config_revision = str(instance["config_revision"])
            target.provider_secret_binding_revision = str(instance["secret_binding_revision"])
        return instance

    @asynccontextmanager
    async def materialize_target(
        self,
        target: TargetConfig,
        *,
        frozen: bool = False,
    ) -> AsyncIterator[TargetConfig]:
        """Validate one Target and lease its exact Provider credential for this call only."""

        runtime_target = target.model_copy(deep=True)
        if runtime_target.provider_instance_id is None:
            await self._validate_target(runtime_target, frozen=False)
            yield runtime_target
            return

        self._reject_provider_target_credentials(runtime_target)
        instance = await self._validate_target(runtime_target, frozen=frozen)
        if instance is None:
            raise RuntimeError("Provider Instance Target validation did not return a binding")
        secret_name = instance["config"].get("auth_secret_name")
        if not isinstance(secret_name, str) or not secret_name:
            runtime_target.auth = runtime_target.auth.model_copy(
                update={
                    "type": "none",
                    "token": None,
                    "header_name": "Authorization",
                    "token_prefix": "Bearer",
                }
            )
            yield runtime_target
            return
        secret_ref = self._bound_secret_ref(instance["secret_refs"], secret_name)
        if self.secret_broker is None:
            raise RuntimeError("Provider Instance Secret resolution is unavailable")
        async with self.secret_broker.lease({secret_name: secret_ref}) as lease:
            runtime_target.auth = runtime_target.auth.model_copy(
                update={
                    "type": "bearer",
                    "token": lease.value(secret_name),
                    "header_name": "Authorization",
                    "token_prefix": "Bearer",
                }
            )
            try:
                yield runtime_target
            finally:
                runtime_target.auth.token = None

    async def set_package_enabled(
        self,
        package_type: PackageType,
        package_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        return await self.repository.set_package_enabled(package_type, package_id, enabled)

    async def set_provider_instance_enabled(
        self,
        instance_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        if enabled:
            instance = await self.repository.get_provider_instance(instance_id)
            broker = self.secret_broker
            if instance["secret_refs"] and broker is None:
                raise RuntimeError("Provider Instance Secret validation is unavailable")
            for reference in instance["secret_refs"].values():
                if broker is None:
                    raise RuntimeError("Provider Instance Secret validation is unavailable")
                broker.validate_reference(reference, require_configured=True)
        return await self.repository.set_instance_enabled(instance_id, enabled)

    async def reload(self) -> dict[str, Any]:
        started = perf_counter()
        packages = self.catalog.discover()
        records = await self.repository.register_packages(packages)
        accepted = [
            (package, record)
            for package, record in zip(packages, records, strict=True)
            if (
                package.validation_status == "valid"
                and record.get("error_code") is None
                and record.get("checksum") == package.checksum
                and record.get("validation_status") == "valid"
            )
        ]
        self._archive_packages([package for package, _ in accepted])
        instances: list[dict[str, Any]] = []
        for package, record in accepted:
            if package.package_type != PackageType.provider:
                continue
            materialized = self.materialize_package(record, package.checksum)
            instance_file = Path(materialized["source_path"]) / "instances.yaml"
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
        archived = Path(self.catalog.settings.archive_root) / checksum
        if archived.is_dir() and self.catalog.checksum_path(archived) == checksum:
            return {**package, "source_path": str(archived.resolve())}
        raise ValueError(
            f"immutable package archive unavailable for {package['package_id']} checksum {checksum}; "
            "reload or validate the package before use"
        )

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
            temporary = archive_root / f".{package.checksum}.{uuid4().hex}.tmp"
            try:
                shutil.copytree(
                    package.source_path,
                    temporary,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
                )
                if self.catalog.checksum_path(temporary) != package.checksum:
                    raise ValueError(
                        f"equipment package changed while archiving: {package.package_id}"
                    )
                try:
                    temporary.rename(destination)
                except OSError:
                    if (
                        not destination.is_dir()
                        or self.catalog.checksum_path(destination) != package.checksum
                    ):
                        raise
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)

    async def validate_package(self, package_type: PackageType, package_id: str) -> dict[str, Any]:
        package = await self.repository.get_package(package_type, package_id)
        discovered = self.catalog.validate_path(Path(package["source_path"]), package_type)
        results = await self.repository.register_packages([discovered])
        result = results[0]
        if (
            discovered.validation_status == "valid"
            and result.get("error_code") is None
            and result.get("checksum") == discovered.checksum
            and result.get("validation_status") == "valid"
        ):
            self._archive_packages([discovered])
        return result

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
        package = self.materialize_package(package, str(package["checksum"]))
        schema_path = (
            Path(package["source_path"]) / package["manifest"]["configuration"]["schema_file"]
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validate_instance(payload.config, schema)
        self._reject_persisted_provider_secrets(payload.config)
        secret_name = payload.config.get("auth_secret_name")
        if secret_name is not None:
            if (
                not isinstance(secret_name, str)
                or not secret_name
                or not secret_name.isascii()
                or not secret_name.replace("_", "a").isalnum()
                or secret_name[0].isdigit()
            ):
                raise ValueError(
                    "Provider auth_secret_name must be an ASCII environment-safe secret name"
                )
            self._bound_secret_ref(payload.secret_refs, secret_name)
        for reference in payload.secret_refs.values():
            if self.secret_broker is not None:
                self.secret_broker.validate_reference(
                    reference,
                    require_configured=payload.enabled,
                )
            else:
                validate_secret_reference(reference)
                if payload.enabled:
                    raise RuntimeError("Provider Instance Secret validation is unavailable")
        for name, value in payload.config.items():
            if isinstance(value, str) and (name == "base_url" or name.endswith("_path")):
                validate_url_has_no_secrets(value, label=f"Provider config {name}")
        manifest_hosts = {
            str(host).rstrip(".").casefold()
            for host in package["manifest"]
            .get("runtime", {})
            .get("network", {})
            .get("allowed_hosts", [])
        }
        normalized_hosts = sorted({host.rstrip(".").casefold() for host in payload.allowed_hosts})
        if not set(normalized_hosts).issubset(manifest_hosts):
            raise ValueError("instance allowed_hosts exceed the Provider manifest")
        base_url = payload.config.get("base_url")
        if isinstance(base_url, str) and base_url:
            validate_outbound_url(base_url, normalized_hosts)
            if secret_name is not None and urlparse(base_url).scheme.casefold() != "https":
                raise ValueError("Provider Instances with an auth secret must use HTTPS")
        normalized_payload = payload.model_copy(update={"allowed_hosts": normalized_hosts})
        return await self.repository.create_provider_instance(normalized_payload, package)

    @staticmethod
    def _reject_provider_target_credentials(target: TargetConfig) -> None:
        if target.auth.token:
            raise ValueError(
                "Provider-bound Target credentials must come from the exact Instance revision"
            )
        if target.headers:
            raise ValueError("Provider-bound Target headers must come from the Instance revision")
        EquipmentService._reject_persisted_provider_secrets(
            target.request_template.body_template,
            path="target.request_template.body_template",
            allow_metadata=False,
        )

    @staticmethod
    def _bound_secret_ref(secret_refs: dict[str, str], secret_name: str) -> str:
        matches = [
            reference
            for name, reference in secret_refs.items()
            if name.casefold() == secret_name.casefold()
        ]
        if len(matches) != 1 or not matches[0]:
            raise ValueError(f"Provider auth secret {secret_name} must have one exact binding")
        return matches[0]

    @classmethod
    def _reject_persisted_provider_secrets(
        cls,
        config: Any,
        path: str = "config",
        *,
        allow_metadata: bool = True,
    ) -> None:
        if isinstance(config, dict):
            for key, value in config.items():
                normalized_key = normalize_sensitive_key(key)
                current = f"{path}.{key}"
                if (
                    not (allow_metadata and normalized_key in _PROVIDER_SECRET_METADATA_KEYS)
                    and is_sensitive_field(key, value)
                    and value not in (None, "", {}, [])
                ):
                    raise ValueError(
                        f"Provider config cannot persist secret value at {current}; use secret_refs"
                    )
                cls._reject_persisted_provider_secrets(
                    value,
                    current,
                    allow_metadata=allow_metadata,
                )
        elif isinstance(config, list):
            for index, value in enumerate(config):
                cls._reject_persisted_provider_secrets(
                    value,
                    f"{path}[{index}]",
                    allow_metadata=allow_metadata,
                )
        elif isinstance(config, str):
            if contains_private_key_pem(config):
                raise ValueError(
                    f"Provider config cannot persist private-key PEM material at {path}; "
                    "use secret_refs"
                )
            validate_url_has_no_secrets(config, label=f"Provider {path}")

    async def freeze_run_bindings(
        self,
        *,
        run_id: str,
        stage: str,
        target_binding_ref: str,
        test_principal_ref: str,
        overrides: dict[str, dict[str, Any]] | None = None,
        provider_instance_id: str | None = None,
        provider_package_checksum: str | None = None,
        provider_config_revision: str | None = None,
        provider_secret_binding_revision: str | None = None,
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
        override_instance_id = provider_override.get("provider_instance_id")
        if (
            provider_instance_id is not None
            and override_instance_id is not None
            and provider_instance_id != override_instance_id
        ):
            raise ValueError("Target and Equipment override select different Provider Instances")
        selected_instance_id = str(
            provider_instance_id or override_instance_id or binding_spec["provider_instance_id"]
        )
        expected_revision = (
            provider_package_checksum,
            provider_config_revision,
            provider_secret_binding_revision,
        )
        if any(expected_revision) and not all(expected_revision):
            raise ValueError("Target Provider Instance revision binding is incomplete")
        if provider_instance_id is not None and not all(expected_revision):
            raise ValueError("Target Provider Instance revision binding is required")
        exact_revision = all(expected_revision)
        if exact_revision:
            instance = await self.repository.get_provider_instance_revision(
                selected_instance_id,
                str(provider_package_checksum),
                str(provider_config_revision),
                str(provider_secret_binding_revision),
            )
        else:
            instance = await self.repository.get_provider_instance(selected_instance_id)
        if not exact_revision and not instance["enabled"]:
            raise ValueError(f"provider instance {instance['instance_id']} is not enabled")
        if exact_revision:
            provider = await self.repository.get_package(
                PackageType.provider,
                str(instance["provider_package_id"]),
                str(instance["provider_version"]),
            )
            selection = overrides.get(str(instance["provider_package_id"]), {})
            if selection.get("version") not in (None, instance["provider_version"]):
                raise ValueError("Equipment override conflicts with frozen Provider version")
            if selection.get("checksum") not in (None, instance["package_checksum"]):
                raise ValueError("Equipment override conflicts with frozen Provider checksum")
            provider = self.materialize_package(provider, str(instance["package_checksum"]))
        else:
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
