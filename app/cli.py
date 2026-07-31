"""Attacker Worker、装备管理、契约测试和开发辅助命令入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
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
from app.runtime import create_runtime
from app.schemas.equipment_schema import PackageType, SkillDryRunRequest
from app.services.equipment_service import EquipmentService
from app.services.harness_service import HarnessService
from app.services.job_service import JobWorker
from conf.logging import setup_logger
from conf.settings import WorkerSettings, settings


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

    worker = commands.add_parser("worker")
    worker.add_argument(
        "--worker-id",
        default=f"{socket.gethostname()}-{os.getpid()}",
    )
    worker.add_argument("--concurrency", type=int, default=None)
    worker.add_argument("--poll-seconds", type=float, default=None)
    worker.add_argument("--once", action="store_true")

    config = commands.add_parser("config")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("validate-production")
    return parser


async def _run(args: argparse.Namespace) -> Any:
    if args.command == "config" and args.config_command == "validate-production":
        settings.validate_production()
        return {"status": "valid", "profile": "production"}
    if args.command == "worker":
        return await _run_worker(args)

    database = Database.from_settings(settings.database)
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
            return await contract_check(
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


async def _run_worker(args: argparse.Namespace) -> dict[str, Any]:
    setup_logger()
    worker_settings = WorkerSettings.model_validate(
        {
            **settings.worker.model_dump(),
            **({"concurrency": args.concurrency} if args.concurrency is not None else {}),
            **({"poll_seconds": args.poll_seconds} if args.poll_seconds is not None else {}),
        }
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handlers: dict[signal.Signals, Any] = {}

    def request_stop(signum, _frame) -> None:
        loop.call_soon_threadsafe(stop_event.set)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)
    try:
        async with create_runtime(settings, recover_cleanups=True) as runtime:
            worker = JobWorker(
                worker_id=args.worker_id,
                repository=runtime.job_repository,
                dispatcher=runtime.job_dispatcher,
                settings=worker_settings,
            )
            await worker.run(stop_event, once=args.once)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return {"status": "stopped", "worker_id": args.worker_id}


def main() -> None:
    args = _parser().parse_args()
    result = asyncio.run(_run(args))
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
