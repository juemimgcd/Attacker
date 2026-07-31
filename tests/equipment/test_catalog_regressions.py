import shutil
from pathlib import Path

import pytest

from app.equipment.catalog import EquipmentCatalog
from app.repositories.equipment_repository import EquipmentRepository
from app.schemas.equipment_schema import PackageType
from conf.settings import EquipmentSettings

ROOT = Path(__file__).parents[2]


def _catalog(tmp_path: Path) -> EquipmentCatalog:
    return EquipmentCatalog(
        EquipmentSettings(
            root=str(ROOT / "equipment"),
            contracts_root=str(ROOT / "contracts"),
            workspace_root=str(tmp_path / "workspaces"),
            archive_root=str(tmp_path / "archive"),
            require_signature=False,
            trust_roots_file=str(ROOT / "deploy/security/equipment_trust_roots.json"),
            revocations_file=str(ROOT / "deploy/security/equipment_revocations.json"),
        )
    )


def test_builtin_catalog_discovery_is_deterministic_and_complete(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)

    first = catalog.discover()
    second = catalog.discover()

    first_facts = [
        (item.package_type.value, item.package_id, item.version, item.checksum) for item in first
    ]
    second_facts = [
        (item.package_type.value, item.package_id, item.version, item.checksum) for item in second
    ]
    assert first_facts == second_facts
    assert all(item.validation_status == "valid" for item in first)
    counts = {
        package_type: sum(item.package_type == package_type for item in first)
        for package_type in PackageType
    }
    assert counts == {
        PackageType.provider: 3,
        PackageType.skill: 6,
        PackageType.casepack: 3,
        PackageType.contract: 10,
    }


@pytest.mark.asyncio
async def test_repository_rejects_changed_content_for_an_immutable_version(
    tmp_path: Path,
    session_factory,
) -> None:
    catalog = _catalog(tmp_path)
    repository = EquipmentRepository(session_factory)
    original = next(
        item
        for item in catalog.discover()
        if item.package_type == PackageType.skill
        and item.package_id == "enterprise-change-risk-evaluator"
    )
    registered = await repository.register_packages([original])
    assert registered[0]["checksum"] == original.checksum

    changed = original.model_copy(update={"checksum": "f" * 64})
    conflict = await repository.register_packages([changed])

    assert conflict[0]["error_code"] == "immutable_version_conflict"
    persisted = await repository.get_package(
        PackageType.skill,
        original.package_id,
        original.version,
    )
    assert persisted["checksum"] == original.checksum


@pytest.mark.asyncio
async def test_invalid_package_is_never_enabled(tmp_path: Path, session_factory) -> None:
    catalog = _catalog(tmp_path)
    repository = EquipmentRepository(session_factory)
    source = next(
        item
        for item in catalog.discover()
        if item.package_type == PackageType.skill
        and item.package_id == "enterprise-change-risk-evaluator"
    )
    invalid = source.model_copy(
        update={
            "package_id": "invalid-enterprise-skill",
            "checksum": "e" * 64,
            "validation_status": "invalid",
            "validation_errors": ["entrypoint is missing"],
        }
    )

    result = await repository.register_packages([invalid])

    assert result[0]["enabled"] is False
    assert result[0]["validation_status"] == "invalid"


def test_catalog_rejects_cached_bytecode_outside_pycache(tmp_path: Path) -> None:
    equipment_root = tmp_path / "equipment"
    copied = equipment_root / "skills" / "enterprise-change-risk-evaluator"
    copied.parent.mkdir(parents=True)
    shutil.copytree(
        ROOT / "equipment/skills/enterprise-change-risk-evaluator",
        copied,
    )
    (copied / "handler.pyc").write_bytes(b"untrusted-cached-bytecode")
    catalog = EquipmentCatalog(
        EquipmentSettings(
            root=str(equipment_root),
            contracts_root=str(ROOT / "contracts"),
            workspace_root=str(tmp_path / "workspaces"),
            archive_root=str(tmp_path / "archive"),
            require_signature=False,
            trust_roots_file=str(ROOT / "deploy/security/equipment_trust_roots.json"),
            revocations_file=str(ROOT / "deploy/security/equipment_revocations.json"),
        )
    )

    package = catalog.validate_path(copied, PackageType.skill)

    assert package.validation_status == "invalid"
    assert any("bytecode artifacts rejected" in error for error in package.validation_errors)
