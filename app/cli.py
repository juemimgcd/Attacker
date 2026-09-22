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

from alembic.config import Config as AlembicConfig
from prometheus_client import start_http_server

from alembic import command as alembic_command
from app.equipment.catalog import EquipmentCatalog
from app.equipment.development import (
    contract_check,
    import_offline_zip,
    scaffold_package,
)
from app.equipment.runner import EquipmentRunner
from app.infrastructure.database import Database
from app.infrastructure.secrets import build_secret_broker
from app.repositories.equipment_repository import EquipmentRepository
from app.runtime import create_runtime
from app.schemas.equipment_schema import (
    EquipmentReplayMode,
    PackageType,
    SkillDryRunRequest,
)
from app.schemas.replay_schema import ReplayRunRequest
from app.schemas.stateful_schema import StatefulProfile, StatefulRunRequest
from app.services.equipment_service import EquipmentService
from app.services.harness_service import HarnessService
from app.services.job_service import JobWorker
from conf.logging import setup_logger
from conf.settings import WorkerSettings, settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="attacker")
    commands = parser.add_subparsers(dest="command", required=True)
    from app.benchmark_cli import add_parser

    add_parser(commands)
    from app.subagent_cli import add_parser as add_subagent_parser

    add_subagent_parser(commands)
    from app.business_test_cli import add_parser as add_business_parser

    add_business_parser(commands)
    from app.trace_cli import add_parser as add_trace_parser

    add_trace_parser(commands)
    equipment = commands.add_parser("equipment")
    equipment_commands = equipment.add_subparsers(dest="equipment_command", required=True)
    list_command = equipment_commands.add_parser("list")
    list_command.add_argument(
        "--type", choices=[item.value for item in PackageType], default="benchmark"
    )
    validate = equipment_commands.add_parser("validate")
    validate.add_argument("path")
    validate.add_argument("--type", choices=[item.value for item in PackageType], required=True)
    reload_command = equipment_commands.add_parser("reload")
    reload_command.add_argument(
        "--legacy", action="store_true", help="Also load legacy security equipment"
    )
    import_command = equipment_commands.add_parser("import")
    import_command.add_argument("archive")
    scaffold = equipment_commands.add_parser("scaffold")
    scaffold.add_argument("type", choices=["provider", "skill", "casepack", "benchmark"])
    scaffold.add_argument("package_id")
    contract_test = equipment_commands.add_parser("contract-test")
    contract_test.add_argument("path")
    contract_test.add_argument(
        "--type", choices=["provider", "skill", "casepack", "benchmark"], required=True
    )
    for action in ("enable", "disable"):
        toggle = equipment_commands.add_parser(action)
        toggle.add_argument("package_id")
        toggle.add_argument("--type", choices=[item.value for item in PackageType], required=True)
    from app.equipment.benchmark_cli import add_parser as add_equipment_benchmark_parser

    add_equipment_benchmark_parser(equipment_commands)

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
    worker.add_argument("--metrics-port", type=int, default=None)
    worker.add_argument("--once", action="store_true")

    config = commands.add_parser("config")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("validate-production")

    migrate = commands.add_parser("migrate")
    migrate.add_argument("--revision", default="head")

    demo = commands.add_parser("demo")
    demo.add_argument("--dataset", default="samples/stateful/phase3.yaml")
    demo.add_argument("--report", default=None)
    return parser


async def _run(args: argparse.Namespace) -> Any:
    if args.command == "trace":
        from app.trace_cli import execute as execute_trace

        return await execute_trace(args)
    if args.command == "subagents":
        from app.subagent_cli import execute as execute_subagents

        return await execute_subagents(args)
    if args.command == "benchmark":
        from app.benchmark_cli import execute as execute_benchmark

        return await execute_benchmark(args)
    if args.command == "business-test":
        from app.business_test_cli import execute as execute_business_test

        return await execute_business_test(args)
    if args.command == "config" and args.config_command == "validate-production":
        settings.validate_production()
        return {"status": "valid", "profile": "production"}
    if args.command == "worker":
        return await _run_worker(args)
    if args.command == "migrate":
        return await asyncio.to_thread(_run_migrate, args.revision)
    if args.command == "demo":
        return await _run_demo(args)

    database = Database.from_settings(settings.database)
    await database.initialize()
    repository = EquipmentRepository(database.session_factory)
    catalog = EquipmentCatalog(settings.equipment)
    service = EquipmentService(
        repository, catalog, secret_broker=build_secret_broker(settings.secrets)
    )
    try:
        if args.command == "equipment" and args.equipment_command == "benchmark":
            from app.equipment.benchmark_cli import execute as execute_equipment_benchmark

            return await execute_equipment_benchmark(args, service)
        if args.command == "equipment" and args.equipment_command in {"enable", "disable"}:
            return await service.set_package_enabled(
                PackageType(args.type), args.package_id, args.equipment_command == "enable"
            )
        if args.command == "equipment" and args.equipment_command == "reload":
            return await service.reload() if args.legacy else await service.reload_benchmarks()
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
        harness = HarnessService(
            repository,
            service,
            EquipmentRunner(settings.equipment),
            settings.equipment,
            secret_broker=service.secret_broker,
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
    metrics_server = None
    metrics_thread = None
    if args.metrics_port is not None:
        if not 1 <= args.metrics_port <= 65_535:
            raise ValueError("worker metrics port must be between 1 and 65535")
        metrics_server, metrics_thread = start_http_server(args.metrics_port)

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
        if metrics_server is not None:
            metrics_server.shutdown()
            metrics_server.server_close()
        if metrics_thread is not None:
            metrics_thread.join(timeout=5)
    return {"status": "stopped", "worker_id": args.worker_id}


async def _run_demo(args: argparse.Namespace) -> dict[str, Any]:
    """运行内置漏洞基线和加固 Replay，并生成可审计 Markdown 报告。"""

    if settings.app.app_env.lower() in {"production", "prod"}:
        raise ValueError("attacker demo is disabled in production environments")

    async with create_runtime(settings, recover_cleanups=True) as runtime:
        source = await runtime.stateful_run_service.run(
            StatefulRunRequest(
                profile=StatefulProfile.vulnerable,
                dataset_path=args.dataset,
                target_name="golden-path-sandbox",
            )
        )
        source_run_id = str(source["run_id"])
        snapshots = await runtime.equipment_repository.list_snapshots(source_run_id)
        bindings = {
            snapshot["package_id"]: {
                "version": snapshot["version"],
                "checksum": snapshot["checksum"],
                **(
                    {"provider_instance_id": snapshot["provider_instance_id"]}
                    if snapshot.get("provider_instance_id")
                    else {}
                ),
            }
            for snapshot in snapshots
        }
        replay = await runtime.replay_service.replay(
            source_run_id,
            ReplayRunRequest(
                mode=EquipmentReplayMode.upgrade_comparison,
                profile=StatefulProfile.hardened,
                equipment_bindings=bindings,
            ),
        )
        replay_run_id = str(replay["run_id"])
        report_path = (
            Path(args.report)
            if args.report
            else Path("data") / f"attacker-demo-report-{replay_run_id}.md"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            await runtime.report_service.build_markdown(replay_run_id),
            encoding="utf-8",
        )
        return {
            "source_run_id": source_run_id,
            "source_profile": StatefulProfile.vulnerable.value,
            "replay_run_id": replay_run_id,
            "replay_profile": StatefulProfile.hardened.value,
            "diff": replay["replay"]["diff"],
            "report": str(report_path.resolve()),
        }


def _run_migrate(revision: str) -> dict[str, str]:
    """从源码树或已安装 wheel 中运行同一套 Alembic migration。"""

    app_root = Path(__file__).resolve().parent
    package_root = app_root.parent
    migrations = app_root / "migrations"
    config_path = app_root / "alembic.ini"
    if not migrations.is_dir():
        migrations = package_root / "alembic"
        config_path = package_root / "alembic.ini"

    config = AlembicConfig(str(config_path))
    config.set_main_option("script_location", str(migrations))
    config.set_main_option("prepend_sys_path", str(package_root))
    alembic_command.upgrade(config, revision)
    return {"status": "migrated", "revision": revision}


def main() -> None:
    args = _parser().parse_args()
    if args.command == "business-test":
        from app.business_test_cli import run_cli

        raise SystemExit(run_cli(args))
    try:
        result = asyncio.run(_run(args))
    except KeyboardInterrupt:
        if args.command == "trace" and args.trace_command == "proxy":
            raise SystemExit(130) from None
        raise
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
