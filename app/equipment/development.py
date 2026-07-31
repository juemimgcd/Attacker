"""装备脚手架、离线 ZIP 导入与 Contract 场景校验工具。"""

from __future__ import annotations

import ast
import json
import platform
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from app.equipment.catalog import MANIFEST_NAMES, EquipmentCatalog
from app.equipment.json_schema import validate_instance
from app.equipment.runner import EquipmentRunner
from app.schemas.equipment_schema import (
    CapabilityContractManifest,
    PackageType,
    ProviderResult,
    SkillManifest,
    SkillResult,
)

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
        (destination / ".equipment-source.json").write_text(
            json.dumps(
                {
                    "source_type": "offline_archive",
                    "source_ref": str(archive_path),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    validated = catalog.validate_path(destination, package_type)
    return validated.model_dump(mode="json")


async def contract_check(
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
    expected_parameters = (
        {
            "describe": ["self"],
            "validate_config": ["self", "config"],
            "healthcheck": ["self", "context"],
            "invoke": ["self", "capability", "payload", "context"],
            "cleanup": ["self", "resource", "context"],
        }
        if package_type == PackageType.provider
        else {
            "prepare": ["self", "context"],
            "execute": ["self", "payload", "context"],
        }
    )
    signatures = _inspect_method_signatures(
        Path(package.source_path),
        str(package.manifest["entrypoint"]),
    )
    missing = [name for name in expected_parameters if name not in signatures]
    if missing:
        raise ValueError(f"missing async contract methods: {missing}")
    signature_errors: list[str] = []
    for name, expected in expected_parameters.items():
        actual = signatures[name]
        if actual != expected:
            signature_errors.append(f"{name} parameters must be {expected}, received {actual}")
    if signature_errors:
        raise ValueError("; ".join(signature_errors))
    known_contracts = {
        item.package_id
        for item in catalog.discover()
        if item.package_type == PackageType.contract and item.validation_status == "valid"
    }
    referenced_contracts = EquipmentCatalog._contract_references(package)
    unknown_contracts = sorted(set(referenced_contracts) - known_contracts)
    if unknown_contracts:
        raise ValueError(f"unknown capability contracts: {unknown_contracts}")
    scenario_results = await _run_contract_scenarios(
        catalog,
        package,
        package_type,
        expected_parameters=set(expected_parameters),
    )
    return {
        "package_id": package.package_id,
        "package_type": package_type.value,
        "valid": True,
        "checked_methods": list(expected_parameters),
        "method_signatures": signatures,
        "referenced_contracts": referenced_contracts,
        "scenario_results": scenario_results,
        "platform": platform.system().lower(),
        "checksum": package.checksum,
    }


def _inspect_method_signatures(package_path: Path, entrypoint: str) -> dict[str, list[str]]:
    module_name, separator, symbol_name = entrypoint.partition(":")
    if not separator or not module_name or not symbol_name:
        raise ValueError("entrypoint must use module_or_file:Symbol")
    module_path = package_path / (
        module_name if module_name.endswith(".py") else f"{module_name.replace('.', '/')}.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    entrypoint_class = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == symbol_name),
        None,
    )
    if entrypoint_class is None:
        raise ValueError(f"entrypoint class {symbol_name} was not found")
    signatures: dict[str, list[str]] = {}
    for node in entrypoint_class.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        parameters = [
            argument.arg
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
        ]
        if node.args.vararg is not None:
            parameters.append(f"*{node.args.vararg.arg}")
        if node.args.kwarg is not None:
            parameters.append(f"**{node.args.kwarg.arg}")
        signatures[node.name] = parameters
    return signatures


async def _run_contract_scenarios(
    catalog: EquipmentCatalog,
    package,
    package_type: PackageType,
    *,
    expected_parameters: set[str],
) -> list[dict[str, Any]]:
    scenario_path = Path(package.source_path) / "contract-tests.yaml"
    if not scenario_path.is_file():
        return []
    loaded = yaml.safe_load(scenario_path.read_text(encoding="utf-8")) or {}
    scenarios = loaded.get("scenarios", []) if isinstance(loaded, dict) else loaded
    if not isinstance(scenarios, list):
        raise TypeError("contract-tests.yaml scenarios must be an array")

    runtime_package = package.model_dump(mode="json")
    runtime_package["trust_level"] = str(package.manifest.get("trust_level", "trusted_enterprise"))
    runner = EquipmentRunner(catalog.settings)
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="attacker-contract-test-") as temporary:
        for index, scenario in enumerate(scenarios, start=1):
            if not isinstance(scenario, dict):
                raise TypeError(f"contract scenario {index} must be an object")
            name = str(scenario.get("name") or f"scenario-{index}")
            method = str(scenario.get("method", ""))
            if method not in expected_parameters:
                raise ValueError(f"{name}: unsupported contract method {method}")
            kwargs = scenario.get("kwargs", {})
            if not isinstance(kwargs, dict):
                raise TypeError(f"{name}: kwargs must be an object")
            _validate_scenario_request(
                catalog,
                package,
                package_type,
                method,
                kwargs,
            )
            raw_result = await runner.execute(
                runtime_package,
                method=method,
                kwargs=kwargs,
                workspace=Path(temporary) / str(index),
                timeout_seconds=float(scenario.get("timeout_seconds", 30)),
            )
            normalized = _validate_scenario_result(
                catalog,
                package,
                package_type,
                method,
                kwargs,
                raw_result,
            )
            expected = scenario.get("expected", {})
            if not isinstance(expected, dict):
                raise TypeError(f"{name}: expected must be an object")
            mismatches = {
                key: {"expected": value, "actual": normalized.get(key)}
                for key, value in expected.items()
                if normalized.get(key) != value
            }
            if mismatches:
                raise ValueError(f"{name}: result mismatch {mismatches}")
            results.append({"name": name, "method": method, "status": "passed"})
    return results


def _validate_scenario_request(
    catalog: EquipmentCatalog,
    package,
    package_type: PackageType,
    method: str,
    kwargs: dict[str, Any],
) -> None:
    if package_type == PackageType.provider and method == "invoke":
        capability = str(kwargs.get("capability", ""))
        contract = catalog.validate_path(
            Path(catalog.settings.contracts_root) / capability,
            PackageType.contract,
        )
        if contract.validation_status != "valid":
            raise ValueError(f"invalid scenario Capability Contract: {capability}")
        manifest = CapabilityContractManifest.model_validate(contract.manifest)
        validate_instance(
            kwargs.get("payload"),
            json.loads(
                (Path(contract.source_path) / manifest.request_schema).read_text(encoding="utf-8")
            ),
        )
    elif package_type == PackageType.skill and method == "execute":
        manifest = SkillManifest.model_validate(package.manifest)
        validate_instance(
            kwargs.get("payload"),
            json.loads(
                (Path(package.source_path) / manifest.input_schema).read_text(encoding="utf-8")
            ),
        )


def _validate_scenario_result(
    catalog: EquipmentCatalog,
    package,
    package_type: PackageType,
    method: str,
    kwargs: dict[str, Any],
    raw_result: Any,
) -> dict[str, Any]:
    if package_type == PackageType.provider and method == "invoke":
        result = ProviderResult.model_validate(raw_result)
        capability = str(kwargs["capability"])
        contract = catalog.validate_path(
            Path(catalog.settings.contracts_root) / capability,
            PackageType.contract,
        )
        manifest = CapabilityContractManifest.model_validate(contract.manifest)
        validate_instance(
            result.output,
            json.loads(
                (Path(contract.source_path) / manifest.response_schema).read_text(encoding="utf-8")
            ),
        )
        return result.model_dump(mode="json")
    if package_type == PackageType.skill and method == "execute":
        result = SkillResult.model_validate(raw_result)
        manifest = SkillManifest.model_validate(package.manifest)
        if result.status == "success" and not result.capability_requests:
            validate_instance(
                result.output,
                json.loads(
                    (Path(package.source_path) / manifest.output_schema).read_text(encoding="utf-8")
                ),
            )
        return result.model_dump(mode="json")
    if not isinstance(raw_result, dict):
        raise TypeError(f"{method} contract result must be an object")
    return raw_result


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
