from __future__ import annotations

import inspect
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from app.equipment.catalog import MANIFEST_NAMES, EquipmentCatalog
from app.equipment.worker import _load_symbol
from app.schemas.equipment_schema import PackageType

PACKAGE_ID = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


def scaffold_package(
    catalog: EquipmentCatalog,
    package_type: PackageType,
    package_id: str,
) -> Path:
    if not PACKAGE_ID.fullmatch(package_id):
        raise ValueError("package id must use lowercase letters, numbers, dots, and hyphens")
    root = _package_root(catalog, package_type)
    destination = root / package_id
    if destination.exists():
        raise FileExistsError(f"package already exists: {destination}")
    destination.mkdir(parents=True)
    files = _scaffold_files(package_type, package_id)
    for relative, content in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return destination


def import_offline_zip(
    catalog: EquipmentCatalog,
    archive_path: Path,
) -> dict[str, Any]:
    archive_path = archive_path.resolve()
    if not archive_path.is_file() or not zipfile.is_zipfile(archive_path):
        raise ValueError("offline package must be a ZIP archive")
    with tempfile.TemporaryDirectory(prefix="attacker-equipment-import-") as temporary:
        temporary_root = Path(temporary)
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > catalog.settings.max_package_files:
                raise ValueError("archive file count limit exceeded")
            total_size = 0
            for member in members:
                relative = PurePosixPath(member.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError(f"unsafe archive path: {member.filename}")
                mode = member.external_attr >> 16
                if mode & 0o170000 == 0o120000:
                    raise ValueError(f"archive symbolic link rejected: {member.filename}")
                total_size += member.file_size
                if total_size > catalog.settings.max_package_bytes:
                    raise ValueError("archive expanded-size limit exceeded")
                if member.file_size > max(member.compress_size, 1) * 100:
                    raise ValueError("archive compression ratio limit exceeded")
                if member.is_dir():
                    continue
                target = temporary_root.joinpath(*relative.parts).resolve()
                if not target.is_relative_to(temporary_root.resolve()):
                    raise ValueError(f"archive path escapes destination: {member.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
        manifest_files = [
            path for path in temporary_root.rglob("*.yaml") if path.name in MANIFEST_NAMES.values()
        ]
        if len(manifest_files) != 1:
            raise ValueError("archive must contain exactly one equipment manifest")
        manifest_path = manifest_files[0]
        package_root = manifest_path.parent
        package_type = next(
            item for item, name in MANIFEST_NAMES.items() if name == manifest_path.name
        )
        inspected = catalog.inspect_package(package_root, package_type)
        if inspected.validation_status != "valid":
            raise ValueError("; ".join(inspected.validation_errors))
        destination = _package_root(catalog, package_type) / inspected.package_id
        if destination.exists():
            raise FileExistsError(f"package destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(package_root, destination)
    validated = catalog.validate_path(destination, package_type)
    return validated.model_dump(mode="json")


def contract_check(
    catalog: EquipmentCatalog,
    package_path: Path,
    package_type: PackageType,
) -> dict[str, Any]:
    package = catalog.validate_path(package_path, package_type)
    if package.validation_status != "valid":
        raise ValueError("; ".join(package.validation_errors))
    if package_type not in {PackageType.provider, PackageType.skill}:
        return {
            "package_id": package.package_id,
            "package_type": package_type.value,
            "valid": True,
            "checked_methods": [],
        }
    symbol = _load_symbol(Path(package.source_path), str(package.manifest["entrypoint"]))
    required = (
        ["describe", "validate_config", "healthcheck", "invoke", "cleanup"]
        if package_type == PackageType.provider
        else ["prepare", "execute"]
    )
    missing = [
        name
        for name in required
        if not hasattr(symbol, name) or not inspect.iscoroutinefunction(getattr(symbol, name))
    ]
    if missing:
        raise ValueError(f"missing async contract methods: {missing}")
    return {
        "package_id": package.package_id,
        "package_type": package_type.value,
        "valid": True,
        "checked_methods": required,
        "checksum": package.checksum,
    }


def _package_root(catalog: EquipmentCatalog, package_type: PackageType) -> Path:
    if package_type == PackageType.contract:
        return Path(catalog.settings.contracts_root)
    suffix = {
        PackageType.provider: "providers",
        PackageType.skill: "skills",
        PackageType.casepack: "casepacks",
    }[package_type]
    return Path(catalog.settings.root) / suffix


def _scaffold_files(package_type: PackageType, package_id: str) -> dict[str, str]:
    compatibility = "attacker_compatibility: {min_version: 0.1.0, max_version: 0.x}\n"
    object_schema = json.dumps(
        {"type": "object", "properties": {}, "additionalProperties": False},
        indent=2,
    )
    if package_type == PackageType.provider:
        manifest = (
            "schema_version: provider.v1\n"
            f"id: {package_id}\nname: {package_id}\nversion: 1.0.0\n"
            "description: Enterprise Provider.\n"
            f"{compatibility}"
            "entrypoint: adapter.py:EnterpriseProvider\n"
            "implements: []\n"
            "configuration: {schema_file: config.schema.json}\n"
            "runtime: {network: {required: false, allowed_hosts: []}, "
            "timeout_seconds: 30, max_response_bytes: 1048576}\n"
            "healthcheck: {timeout_seconds: 5}\n"
            "trust_level: trusted_enterprise\n"
        )
        adapter = (
            "class EnterpriseProvider:\n"
            "    async def describe(self): return {}\n"
            "    async def validate_config(self, config): return {'valid': True}\n"
            "    async def healthcheck(self, context): return {'healthy': True}\n"
            "    async def invoke(self, capability, payload, context): "
            "raise NotImplementedError\n"
            "    async def cleanup(self, resource, context): return {'cleaned': True}\n"
        )
        return {
            "provider.yaml": manifest,
            "adapter.py": adapter,
            "config.schema.json": object_schema + "\n",
            "requirements.lock": "",
            "PROVIDER.md": f"# {package_id}\n",
        }
    if package_type == PackageType.skill:
        manifest = (
            "schema_version: skill.v1\n"
            f"id: {package_id}\nname: {package_id}\nversion: 1.0.0\n"
            "description: Enterprise evaluation Skill.\n"
            f"{compatibility}"
            "entrypoint: handler.py:EnterpriseSkill\n"
            "types: [evaluator]\n"
            "requires: {capabilities: []}\n"
            "permissions: {network: false, filesystem: {read: [], write: []}}\n"
            "execution: {timeout_seconds: 30, max_steps: 1, max_provider_calls: 0, "
            "max_output_bytes: 262144}\n"
            "input_schema: input.schema.json\noutput_schema: output.schema.json\n"
            "cleanup: {required: false}\ntrust_level: trusted_enterprise\n"
        )
        handler = (
            "class EnterpriseSkill:\n"
            "    async def prepare(self, context): return {'ready': True}\n"
            "    async def execute(self, payload, context): "
            "return {'status': 'success', 'output': {}, 'evidence': []}\n"
        )
        return {
            "skill.yaml": manifest,
            "handler.py": handler,
            "input.schema.json": object_schema + "\n",
            "output.schema.json": object_schema + "\n",
            "requirements.lock": "",
            "SKILL.md": f"# {package_id}\n",
        }
    if package_type == PackageType.casepack:
        return {
            "casepack.yaml": (
                "schema_version: casepack.v1\n"
                f"id: {package_id}\nname: {package_id}\nversion: 1.0.0\n"
                "description: Enterprise Case Pack.\n"
                f"{compatibility}"
                "cases_file: cases.yaml\nrequired_capabilities: []\ntags: []\n"
            ),
            "cases.yaml": "cases: []\n",
            "README.md": f"# {package_id}\n",
        }
    raise ValueError("Core Capability Contracts are not scaffolded as deployment equipment")
