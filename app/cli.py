from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from app.equipment.catalog import EquipmentCatalog
from app.equipment.development import (
    contract_check,
    import_offline_zip,
    scaffold_package,
)
from app.equipment.runner import EquipmentRunner
from app.infrastructure.database import Database
from app.repositories.equipment_repository import EquipmentRepository
from app.schemas.equipment_schema import PackageType, SkillDryRunRequest
from app.services.equipment_service import EquipmentService
from app.services.harness_service import HarnessService
from conf.settings import settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="attacker")
    commands = parser.add_subparsers(dest="command", required=True)
    equipment = commands.add_parser("equipment")
    equipment_commands = equipment.add_subparsers(dest="equipment_command", required=True)
    list_command = equipment_commands.add_parser("list")
    list_command.add_argument("--type", choices=[item.value for item in PackageType], default=None)
    validate = equipment_commands.add_parser("validate")
    validate.add_argument("path")
    validate.add_argument("--type", choices=[item.value for item in PackageType], required=True)
    equipment_commands.add_parser("reload")
    import_command = equipment_commands.add_parser("import")
    import_command.add_argument("archive")
    scaffold = equipment_commands.add_parser("scaffold")
    scaffold.add_argument("type", choices=["provider", "skill", "casepack"])
    scaffold.add_argument("package_id")
    contract_test = equipment_commands.add_parser("contract-test")
    contract_test.add_argument("path")
    contract_test.add_argument("--type", choices=["provider", "skill", "casepack"], required=True)

    provider = commands.add_parser("provider-instance")
    provider_commands = provider.add_subparsers(dest="provider_command", required=True)
    healthcheck = provider_commands.add_parser("healthcheck")
    healthcheck.add_argument("instance_id")

    skill = commands.add_parser("skill")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    dry_run = skill_commands.add_parser("dry-run")
    dry_run.add_argument("skill_id")
    dry_run.add_argument("--payload", default="{}")

    casepack = commands.add_parser("casepack")
    casepack_commands = casepack.add_subparsers(dest="casepack_command", required=True)
    casepack_validate = casepack_commands.add_parser("validate")
    casepack_validate.add_argument("path")
    return parser


async def _run(args: argparse.Namespace) -> Any:
    database = Database(settings.database.url, echo=settings.database.echo)
    await database.initialize()
    repository = EquipmentRepository(database.session_factory)
    catalog = EquipmentCatalog(settings.equipment)
    service = EquipmentService(repository, catalog)
    harness = HarnessService(
        repository,
        service,
        EquipmentRunner(settings.equipment),
        settings.equipment,
    )
    try:
        if args.command == "equipment" and args.equipment_command == "reload":
            return await service.reload()
        if args.command == "equipment" and args.equipment_command == "list":
            package_type = PackageType(args.type) if args.type else None
            return await repository.list_packages(package_type=package_type)
        if args.command == "equipment" and args.equipment_command == "validate":
            return catalog.validate_path(Path(args.path), PackageType(args.type)).model_dump(
                mode="json"
            )
        if args.command == "equipment" and args.equipment_command == "import":
            return import_offline_zip(catalog, Path(args.archive))
        if args.command == "equipment" and args.equipment_command == "scaffold":
            destination = scaffold_package(
                catalog,
                PackageType(args.type),
                args.package_id,
            )
            return {"created": str(destination.resolve())}
        if args.command == "equipment" and args.equipment_command == "contract-test":
            return contract_check(
                catalog,
                Path(args.path),
                PackageType(args.type),
            )
        if args.command == "provider-instance" and args.provider_command == "healthcheck":
            return await harness.healthcheck(args.instance_id)
        if args.command == "skill" and args.skill_command == "dry-run":
            return await harness.dry_run_skill(
                args.skill_id,
                SkillDryRunRequest(payload=json.loads(args.payload)),
            )
        if args.command == "casepack" and args.casepack_command == "validate":
            return catalog.validate_path(Path(args.path), PackageType.casepack).model_dump(
                mode="json"
            )
        raise ValueError("unsupported command")
    finally:
        await database.dispose()


def main() -> None:
    args = _parser().parse_args()
    result = asyncio.run(_run(args))
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
