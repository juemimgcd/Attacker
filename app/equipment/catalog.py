from __future__ import annotations

import ast
import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError

from app.equipment.json_schema import validate_json_schema_document
from app.schemas.equipment_schema import (
    CapabilityContractManifest,
    CasePackManifest,
    DiscoveredPackage,
    PackageType,
    ProviderManifest,
    SkillManifest,
    TrustLevel,
)
from conf.settings import EquipmentSettings

ATTACKER_VERSION = "0.1.0"
IGNORED_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", "tests"}
MANIFEST_NAMES = {
    PackageType.provider: "provider.yaml",
    PackageType.skill: "skill.yaml",
    PackageType.casepack: "casepack.yaml",
    PackageType.contract: "contract.yaml",
}


class EquipmentCatalog:
    def __init__(self, settings: EquipmentSettings) -> None:
        self.settings = settings

    def discover(self) -> list[DiscoveredPackage]:
        contracts = self._scan_root(Path(self.settings.contracts_root), PackageType.contract)
        known_contracts = {
            package.package_id for package in contracts if package.validation_status == "valid"
        }
        equipment_root = Path(self.settings.root)
        packages = [
            *contracts,
            *self._scan_root(equipment_root / "providers", PackageType.provider),
            *self._scan_root(equipment_root / "skills", PackageType.skill),
            *self._scan_root(equipment_root / "casepacks", PackageType.casepack),
        ]
        for package in packages:
            if package.validation_status != "valid":
                continue
            references = self._contract_references(package)
            unknown = sorted(set(references) - known_contracts)
            if unknown:
                package.validation_status = "invalid"
                package.enabled = False
                package.validation_errors.append(f"unknown capability contracts: {unknown}")
        return sorted(
            packages,
            key=lambda item: (item.package_type.value, item.package_id, item.version),
        )

    def validate_path(self, path: Path, package_type: PackageType) -> DiscoveredPackage:
        resolved = path.resolve()
        root = self._root_for(package_type).resolve()
        if not resolved.is_relative_to(root):
            return self._invalid(path, package_type, ["package path escapes configured root"])
        return self._load_package(path, package_type)

    def inspect_package(self, path: Path, package_type: PackageType) -> DiscoveredPackage:
        return self._load_package(path, package_type)

    def checksum_path(self, path: Path) -> str:
        return self._checksum(path, self._safe_files(path))

    def _scan_root(self, root: Path, package_type: PackageType) -> list[DiscoveredPackage]:
        if not root.exists():
            return []
        packages: list[DiscoveredPackage] = []
        for path in sorted(root.iterdir(), key=lambda item: item.name):
            if path.name.startswith(".") or path.name in IGNORED_NAMES:
                continue
            if path.is_symlink():
                packages.append(
                    self._invalid(path, package_type, ["symbolic package roots rejected"])
                )
            elif path.is_dir():
                packages.append(self._load_package(path, package_type))
        return packages

    def _load_package(self, path: Path, package_type: PackageType) -> DiscoveredPackage:
        errors: list[str] = []
        checksum = ""
        manifest_data: dict[str, Any] = {}
        manifest_name = MANIFEST_NAMES[package_type]
        manifest_path = path / manifest_name
        try:
            files = self._safe_files(path)
            checksum = self._checksum(path, files)
            if self.settings.require_signature:
                self._verify_signature(path, checksum)
            if manifest_path not in files:
                raise ValueError(f"{manifest_name} is required")
            loaded = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise TypeError("manifest root must be an object")
            manifest_data = loaded
            manifest = self._parse_manifest(package_type, manifest_data)
            self._validate_compatibility(
                manifest.attacker_compatibility.min_version,
                manifest.attacker_compatibility.max_version,
            )
            self._validate_references(path, manifest, package_type)
            self._validate_executable(path, manifest, package_type)
        except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
            errors.append(str(exc))
        package_id = str(manifest_data.get("id") or path.name)
        version = str(manifest_data.get("version") or "0.0.0")
        builtin_data_package = package_type in {
            PackageType.contract,
            PackageType.casepack,
        } and "builtin" in manifest_data.get("tags", ["builtin"])
        trust_level = str(
            manifest_data.get("trust_level")
            or (
                TrustLevel.trusted_builtin.value
                if builtin_data_package
                else TrustLevel.trusted_enterprise.value
            )
        )
        manifest_data.setdefault("trust_level", trust_level)
        return DiscoveredPackage(
            package_type=package_type,
            package_id=package_id,
            version=version,
            source_path=str(path.resolve()),
            checksum=checksum or hashlib.sha256(str(path).encode()).hexdigest(),
            manifest=manifest_data,
            enabled=False,
            validation_status="invalid" if errors else "valid",
            validation_errors=errors,
        )

    def _safe_files(self, root: Path) -> list[Path]:
        resolved_root = root.resolve()
        files: list[Path] = []
        total_bytes = 0
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if any(part.startswith(".") or part in IGNORED_NAMES for part in relative.parts):
                continue
            if len(relative.parts) > self.settings.max_archive_depth:
                raise ValueError("package directory depth limit exceeded")
            if path.is_symlink():
                raise ValueError(f"symbolic links rejected: {relative.as_posix()}")
            resolved = path.resolve()
            if not resolved.is_relative_to(resolved_root):
                raise ValueError(f"path escapes package: {relative.as_posix()}")
            if path.is_file():
                files.append(path)
                total_bytes += path.stat().st_size
                if len(files) > self.settings.max_package_files:
                    raise ValueError("package file count limit exceeded")
                if total_bytes > self.settings.max_package_bytes:
                    raise ValueError("package size limit exceeded")
        return files

    @staticmethod
    def _checksum(root: Path, files: list[Path]) -> str:
        digest = hashlib.sha256()
        for path in files:
            relative = path.relative_to(root).as_posix().encode()
            if relative == b"SIGNATURE.json":
                continue
            content = path.read_bytes()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        return digest.hexdigest()

    def _verify_signature(self, root: Path, checksum: str) -> None:
        signature_path = root / "SIGNATURE.json"
        if not signature_path.is_file():
            raise ValueError("package signature is required")
        trust_roots_path = Path(self.settings.trust_roots_file)
        if not trust_roots_path.is_file():
            raise ValueError("equipment trust roots are not configured")
        signature = json.loads(signature_path.read_text(encoding="utf-8"))
        trust_roots = json.loads(trust_roots_path.read_text(encoding="utf-8"))
        publisher_id = str(signature.get("publisher_id", ""))
        if signature.get("algorithm") != "ed25519" or publisher_id not in trust_roots:
            raise ValueError("package signature publisher or algorithm is not trusted")
        try:
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(trust_roots[publisher_id], validate=True)
            )
            public_key.verify(
                base64.b64decode(str(signature["signature"]), validate=True),
                checksum.encode("ascii"),
            )
        except (InvalidSignature, ValueError, KeyError) as exc:
            raise ValueError("package signature verification failed") from exc

    @staticmethod
    def _parse_manifest(package_type: PackageType, data: dict[str, Any]):
        models = {
            PackageType.provider: ProviderManifest,
            PackageType.skill: SkillManifest,
            PackageType.casepack: CasePackManifest,
            PackageType.contract: CapabilityContractManifest,
        }
        return models[package_type].model_validate(data)

    @staticmethod
    def _validate_compatibility(min_version: str, max_version: str) -> None:
        current = _version_tuple(ATTACKER_VERSION)
        if current < _version_tuple(min_version):
            raise ValueError(f"requires Attacker >= {min_version}")
        if max_version.endswith(".x"):
            allowed_prefix = _version_tuple(max_version[:-2])
            if current[: len(allowed_prefix)] != allowed_prefix:
                raise ValueError(f"requires Attacker {max_version}")
        elif current > _version_tuple(max_version):
            raise ValueError(f"requires Attacker <= {max_version}")

    @staticmethod
    def _validate_references(
        root: Path,
        manifest: ProviderManifest | SkillManifest | CasePackManifest | CapabilityContractManifest,
        package_type: PackageType,
    ) -> None:
        references: list[str] = []
        if package_type == PackageType.provider:
            references.append(manifest.configuration.schema_file)  # type: ignore[union-attr]
        elif package_type == PackageType.skill:
            references.extend([manifest.input_schema, manifest.output_schema])  # type: ignore[union-attr]
        elif package_type == PackageType.casepack:
            references.append(manifest.cases_file)  # type: ignore[union-attr]
        else:
            references.extend([manifest.request_schema, manifest.response_schema])  # type: ignore[union-attr]
        for reference in references:
            target = (root / reference).resolve()
            if not target.is_relative_to(root.resolve()) or not target.is_file():
                raise ValueError(f"missing or unsafe referenced file: {reference}")
            if reference.endswith(".json"):
                schema = json.loads(target.read_text(encoding="utf-8"))
                validate_json_schema_document(schema, name=reference)

    def _validate_executable(
        self,
        root: Path,
        manifest: ProviderManifest | SkillManifest | CasePackManifest | CapabilityContractManifest,
        package_type: PackageType,
    ) -> None:
        if package_type not in {PackageType.provider, PackageType.skill}:
            return
        if not self.settings.allow_executable_packages:
            raise ValueError("executable packages are disabled")
        trust_level = manifest.trust_level  # type: ignore[union-attr]
        if trust_level == TrustLevel.untrusted and not self.settings.allow_untrusted:
            raise ValueError("untrusted packages are disabled")
        module_name, separator, symbol = manifest.entrypoint.partition(":")  # type: ignore[union-attr]
        if not separator or not module_name or not symbol:
            raise ValueError("entrypoint must use module_or_file:Symbol")
        module_path = root / (
            module_name if module_name.endswith(".py") else f"{module_name.replace('.', '/')}.py"
        )
        if not module_path.is_file():
            raise ValueError(f"entrypoint module does not exist: {module_path.name}")
        try:
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        except SyntaxError as exc:
            raise ValueError(f"invalid entrypoint Python: {exc}") from exc
        prohibited_calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        } & {"eval", "exec"}
        if prohibited_calls:
            raise ValueError(f"prohibited dynamic execution calls: {sorted(prohibited_calls)}")
        if trust_level != TrustLevel.trusted_builtin and not (root / "requirements.lock").is_file():
            raise ValueError("executable enterprise packages require requirements.lock")

    @staticmethod
    def _contract_references(package: DiscoveredPackage) -> list[str]:
        if package.package_type == PackageType.provider:
            return [str(item["contract"]) for item in package.manifest.get("implements", [])]
        if package.package_type == PackageType.skill:
            capabilities = package.manifest.get("requires", {}).get("capabilities", [])
            return [str(item["contract"]) for item in capabilities]
        if package.package_type == PackageType.casepack:
            return [str(item) for item in package.manifest.get("required_capabilities", [])]
        cleanup = package.manifest.get("cleanup_contract")
        return [str(cleanup)] if cleanup else []

    @staticmethod
    def _invalid(path: Path, package_type: PackageType, errors: list[str]) -> DiscoveredPackage:
        return DiscoveredPackage(
            package_type=package_type,
            package_id=path.name,
            version="0.0.0",
            source_path=str(path.resolve()),
            checksum=hashlib.sha256(str(path).encode()).hexdigest(),
            manifest={},
            validation_status="invalid",
            validation_errors=errors,
        )

    def _root_for(self, package_type: PackageType) -> Path:
        if package_type == PackageType.contract:
            return Path(self.settings.contracts_root)
        suffix = {
            PackageType.provider: "providers",
            PackageType.skill: "skills",
            PackageType.casepack: "casepacks",
        }[package_type]
        return Path(self.settings.root) / suffix


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = version.strip().removeprefix("v").split(".")
    numeric: list[int] = []
    for part in parts:
        digits = "".join(character for character in part if character.isdigit())
        if not digits:
            break
        numeric.append(int(digits))
    return tuple(numeric or [0])
