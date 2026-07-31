"""离线装备发现与供应链校验；在导入任何包代码前验证路径、Manifest 和信任事实。"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Literal, cast

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
SignatureStatus = Literal[
    "not_present",
    "not_required",
    "verified",
    "invalid",
    "revoked",
]
SourceType = Literal["builtin", "local_directory", "offline_archive"]
IGNORED_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", "tests"}
CORE_BUILTIN_PACKAGES = {
    PackageType.provider: {
        "enterprise-ops-provider",
        "http-agent-provider",
        "isolated-state-provider",
    },
    PackageType.skill: {
        "enterprise-alert-triage-evaluator",
        "enterprise-change-risk-evaluator",
        "enterprise-resource-compliance-evaluator",
        "prompt-injection-evaluator",
        "state-poisoning-evaluator",
        "tool-policy-trace-evaluator",
    },
    PackageType.casepack: {
        "attacker-baseline-v1",
        "attacker-controls-v1",
        "enterprise-operations-controls-v1",
    },
    PackageType.contract: {
        "agent.invoke.v1",
        "agent.trace.read.v1",
        "enterprise.alert.read.v1",
        "enterprise.change.execute.v1",
        "enterprise.resource.read.v1",
        "memory.fixture.cleanup.v1",
        "memory.fixture.read.v1",
        "memory.fixture.write.v1",
        "rag.document.index.v1",
        "rag.retrieval.query.v1",
    },
}
CORE_BUILTIN_CHECKSUMS = {
    (PackageType.provider, "enterprise-ops-provider"): (
        "1232b7ea48cc2a0f77e3215adb9924748f212c46a44e35e874ad28fb67727414"
    ),
    (PackageType.provider, "http-agent-provider"): (
        "73f29665f213b371d975c1054c96767dca7e61162e02147ec01749ab94958b32"
    ),
    (PackageType.provider, "isolated-state-provider"): (
        "3dd9c91aa105a219bebb4d718903432ee93278e5c0e8e2244f8cfe5af4bd1a69"
    ),
    (PackageType.skill, "prompt-injection-evaluator"): (
        "9b05d0fec252a62379cd1d91a90178b8349139f0a6df1ad33212d91f3aec4a3a"
    ),
    (PackageType.skill, "enterprise-alert-triage-evaluator"): (
        "d3c3912c26d1e4a0ac1898c3a5400b43c3a42a874933f35d9067967398747cc5"
    ),
    (PackageType.skill, "enterprise-change-risk-evaluator"): (
        "401537c266a9305ccc372a81e99055bcf94f2972c757a88bd715ce166b31ce6c"
    ),
    (PackageType.skill, "enterprise-resource-compliance-evaluator"): (
        "4d80c92641a3a12c09a0d1bca7731b330ab8cc5a436f631f7f5506a3e1a02894"
    ),
    (PackageType.skill, "state-poisoning-evaluator"): (
        "ce17ad9b1bca7dfd3a783d280f808b07b93e569badb7780c486d6c991e9fb831"
    ),
    (PackageType.skill, "tool-policy-trace-evaluator"): (
        "865e9912df78000a8df4670a1c6fb35e86182eab5c3b280646fae30a0085bffe"
    ),
    (PackageType.casepack, "attacker-baseline-v1"): (
        "41ee30050c80129e66dc0fbb586b3ddf01f00038af6e6f46f114dbbfdf90e87a"
    ),
    (PackageType.casepack, "attacker-controls-v1"): (
        "211817cc96368395323aad629eef61445561e5a40d0996f21c4ae6883e962e20"
    ),
    (PackageType.casepack, "enterprise-operations-controls-v1"): (
        "ed0d024ec9de219504bccd15322fc888f1609829343fb3277e8a3c10f191ad4e"
    ),
    (PackageType.contract, "agent.invoke.v1"): (
        "62429b5733fd4ce1a5d1cb5b44e9a738ac1f848fa3370bbd1cf0992ad1aa6aa9"
    ),
    (PackageType.contract, "agent.trace.read.v1"): (
        "e8dbe5986a6ed2193dc129dce21ed4fb9f9f586017206cdce7658ae808eb888e"
    ),
    (PackageType.contract, "enterprise.alert.read.v1"): (
        "6e8baca7e93c972efac69049c3b0e48812ab34433525b6c4e7fd7234ec105011"
    ),
    (PackageType.contract, "enterprise.change.execute.v1"): (
        "154bf85d7fa81462813ba958e058f8efba15ddfb519d43cb598ce0a2ed2ccb48"
    ),
    (PackageType.contract, "enterprise.resource.read.v1"): (
        "7308c452140ae79188b5f73ecd10b34a2c6e5d6c58f5cb910924782440e68401"
    ),
    (PackageType.contract, "memory.fixture.cleanup.v1"): (
        "81ddde45f25e365aa0401c3d97b2a0a75baf54dc6e0b233f824c083379ea86cb"
    ),
    (PackageType.contract, "memory.fixture.read.v1"): (
        "c4120613a981e65100e5a1a5fc89cfaa09e97de347ebeea7941e12e0e0e12977"
    ),
    (PackageType.contract, "memory.fixture.write.v1"): (
        "d52f90fad561be99819d1945b70284ee2cae534835ac318238bc931fe83f617c"
    ),
    (PackageType.contract, "rag.document.index.v1"): (
        "0144cb71f8df503611f0e104fd5fd649f863d163ff7904d372cb0a5c76ee61f0"
    ),
    (PackageType.contract, "rag.retrieval.query.v1"): (
        "3d27cc6a52b50313a208e4a5972ac92389642450f28bc602619ab5a4d48b590f"
    ),
}
CANONICAL_TEXT_SUFFIXES = {
    ".json",
    ".lock",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
MANIFEST_NAMES = {
    PackageType.provider: "provider.yaml",
    PackageType.skill: "skill.yaml",
    PackageType.casepack: "casepack.yaml",
    PackageType.contract: "contract.yaml",
}


class SignatureRevokedError(ValueError):
    pass


class EquipmentCatalog:
    """扫描 Contract/Provider/Skill/Case Pack，并计算不可变内容身份。"""

    def __init__(self, settings: EquipmentSettings) -> None:
        self.settings = settings

    def discover(self) -> list[DiscoveredPackage]:
        """先建立有效 Contract 集，再拒绝引用未知能力的可执行包。"""

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
        """将所有校验失败收敛为 invalid package，避免部分加载后再执行。"""

        errors: list[str] = []
        checksum = ""
        signature_status: SignatureStatus = "not_present"
        publisher_id: str | None = None
        signature_id: str | None = None
        manifest_data: dict[str, Any] = {}
        source_type: SourceType = "local_directory"
        source_ref = str(path.resolve())
        manifest_name = MANIFEST_NAMES[package_type]
        manifest_path = path / manifest_name
        try:
            files = self._safe_files(path)
            checksum = self._checksum(path, files)
            canonical_checksum = self._canonical_checksum(path, files)
            signature_path = path / "SIGNATURE.json"
            if signature_path.is_file():
                signature_metadata = json.loads(signature_path.read_text(encoding="utf-8"))
                publisher_id = str(signature_metadata.get("publisher_id", "")) or None
                signature_id = str(signature_metadata.get("signature_id", "")) or None
            signature_status, publisher_id, signature_id = self._signature_status(path, checksum)
            if manifest_path not in files:
                raise ValueError(f"{manifest_name} is required")
            loaded = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise TypeError("manifest root must be an object")
            manifest_data = loaded
            manifest = self._parse_manifest(package_type, manifest_data)
            source_type, source_ref = self._source_facts(
                path,
                package_type,
                str(manifest_data.get("id") or path.name),
            )
            package_id = str(manifest_data.get("id") or path.name)
            if source_type == "builtin" and canonical_checksum != CORE_BUILTIN_CHECKSUMS.get(
                (package_type, package_id)
            ):
                raise ValueError("Core-shipped package checksum is not recognized")
            if (
                self.settings.require_signature
                and source_type != "builtin"
                and signature_status != "verified"
            ):
                raise ValueError("package signature is required")
            if (
                package_type in {PackageType.provider, PackageType.skill}
                and manifest_data.get("trust_level") == TrustLevel.trusted_builtin.value
                and source_type != "builtin"
            ):
                raise ValueError("trusted_builtin is reserved for Core-shipped packages")
            self._validate_compatibility(
                manifest.attacker_compatibility.min_version,
                manifest.attacker_compatibility.max_version,
            )
            self._validate_references(path, manifest, package_type)
            self._validate_executable(path, manifest, package_type)
        except (OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as exc:
            errors.append(str(exc))
            signature_status = "revoked" if isinstance(exc, SignatureRevokedError) else "invalid"
        package_id = str(manifest_data.get("id") or path.name)
        version = str(manifest_data.get("version") or "0.0.0")
        builtin_data_package = (
            package_type
            in {
                PackageType.contract,
                PackageType.casepack,
            }
            and source_type == "builtin"
        )
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
            source_type=source_type,
            source_ref=source_ref,
            checksum=checksum or hashlib.sha256(str(path).encode()).hexdigest(),
            manifest=manifest_data,
            enabled=False,
            validation_status="invalid" if errors else "valid",
            validation_errors=errors,
            signature_status=signature_status,
            publisher_id=publisher_id,
            signature_id=signature_id,
        )

    def _safe_files(self, root: Path) -> list[Path]:
        """拒绝链接、逃逸、字节码和超限目录，返回可参与哈希的稳定文件集。"""

        resolved_root = root.resolve()
        files: list[Path] = []
        total_bytes = 0
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if "__pycache__" not in relative.parts and path.suffix.lower() in {".pyc", ".pyo"}:
                raise ValueError(f"Python bytecode artifacts rejected: {relative.as_posix()}")
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

    @staticmethod
    def _canonical_checksum(root: Path, files: list[Path]) -> str:
        digest = hashlib.sha256()
        for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
            relative = path.relative_to(root).as_posix().encode()
            if relative == b"SIGNATURE.json":
                continue
            content = path.read_bytes()
            if path.suffix.lower() in CANONICAL_TEXT_SUFFIXES:
                try:
                    content = (
                        content.decode("utf-8")
                        .replace("\r\n", "\n")
                        .replace("\r", "\n")
                        .encode("utf-8")
                    )
                except UnicodeDecodeError:
                    pass
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        return digest.hexdigest()

    def _signature_status(
        self, root: Path, checksum: str
    ) -> tuple[SignatureStatus, str | None, str | None]:
        """验证 Ed25519 来源并应用 publisher/checksum/signature 三类撤销。"""

        revocations = self._revocations()
        if checksum in revocations["checksums"]:
            raise SignatureRevokedError("package checksum is revoked")
        signature_path = root / "SIGNATURE.json"
        if not signature_path.is_file():
            return "not_required", None, None
        trust_roots_path = Path(self.settings.trust_roots_file)
        if not trust_roots_path.is_file():
            raise ValueError("equipment trust roots are not configured")
        signature = json.loads(signature_path.read_text(encoding="utf-8"))
        trust_roots = json.loads(trust_roots_path.read_text(encoding="utf-8"))
        publisher_id = str(signature.get("publisher_id", ""))
        signature_id = str(signature.get("signature_id", "")) or None
        if publisher_id in revocations["publisher_ids"] or (
            signature_id is not None and signature_id in revocations["signature_ids"]
        ):
            raise SignatureRevokedError("package signature is revoked")
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
        return "verified", publisher_id, signature_id

    def _revocations(self) -> dict[str, set[str]]:
        path = Path(self.settings.revocations_file)
        if not path.is_file():
            return {"publisher_ids": set(), "checksums": set(), "signature_ids": set()}
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return {
            key: {str(value) for value in loaded.get(key, [])}
            for key in ("publisher_ids", "checksums", "signature_ids")
        }

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
            source_ref=str(path.resolve()),
            checksum=hashlib.sha256(str(path).encode()).hexdigest(),
            manifest={},
            validation_status="invalid",
            validation_errors=errors,
            signature_status="invalid",
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

    def _source_facts(
        self,
        path: Path,
        package_type: PackageType,
        package_id: str,
    ) -> tuple[SourceType, str]:
        resolved = path.resolve()
        source_ref = str(resolved)
        source_metadata_path = path / ".equipment-source.json"
        if source_metadata_path.is_file():
            source_metadata = json.loads(source_metadata_path.read_text(encoding="utf-8"))
            candidate_type = str(source_metadata.get("source_type", ""))
            if candidate_type not in {"local_directory", "offline_archive"}:
                raise ValueError("invalid or reserved equipment source type")
            return cast(SourceType, candidate_type), str(
                source_metadata.get("source_ref") or source_ref
            )
        expected = (self._root_for(package_type) / package_id).resolve()
        if package_id in CORE_BUILTIN_PACKAGES[package_type] and resolved == expected:
            return "builtin", source_ref
        return "local_directory", source_ref


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = version.strip().removeprefix("v").split(".")
    numeric: list[int] = []
    for part in parts:
        digits = "".join(character for character in part if character.isdigit())
        if not digits:
            break
        numeric.append(int(digits))
    return tuple(numeric or [0])
