"""Local-only public benchmark commands; upstream execution stays out of HTTP routers."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.infrastructure.database import Database
from app.repositories.run_repository import RunRepository
from app.schemas.benchmark_schema import BenchmarkManifest
from app.services.benchmark_service import (
    SOURCES,
    BenchmarkService,
    injecagent_catalog,
    load_observations,
    read_json,
    select_cases,
    source_metadata,
    write_json,
)
from app.services.report_service import ReportService
from conf.settings import settings

WORKER = Path(__file__).with_name("benchmark_worker.py")


def add_parser(commands: Any) -> None:
    benchmark = commands.add_parser(
        "benchmark", help="public benchmark catalog, run, import and compare"
    )
    actions = benchmark.add_subparsers(dest="benchmark_command", required=True)
    catalog = actions.add_parser("catalog")
    catalog.add_argument("benchmark", choices=SOURCES)
    catalog.add_argument("--source", type=Path, required=True)
    catalog.add_argument("--python", default=sys.executable)
    catalog.add_argument("--version")
    catalog.add_argument("--output", type=Path, required=True)
    for action in ("run", "import"):
        command = actions.add_parser(action)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--case-id", action="append")
        command.add_argument("--limit", type=int)
        command.add_argument("--report", type=Path, required=True)
        if action == "run":
            command.add_argument("--source", type=Path, required=True)
            command.add_argument("--python", required=True)
            command.add_argument("--model", required=True)
            command.add_argument("--defense", choices=["repeat_user_prompt", "tool_filter"])
            command.add_argument("--max-calls", type=int, required=True)
            command.add_argument("--timeout", type=float, default=300)
            command.add_argument("--workdir", type=Path, required=True)
        else:
            command.add_argument("--results", type=Path, required=True)
            command.add_argument(
                "--target",
                required=True,
                help="AgentDojo pipeline_name; InjecAgent caller-supplied model label",
            )
    compare = actions.add_parser("compare")
    compare.add_argument("source_run_id")
    compare.add_argument("replay_run_id")


async def run_worker(python: str, arguments: list[str], timeout: float) -> int:
    if not 0 < timeout <= 86400:
        raise ValueError("timeout must be in (0, 86400]")
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    process = await asyncio.create_subprocess_exec(
        python,
        str(WORKER),
        *arguments,
        env=environment,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(process.wait(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise


async def build_catalog(
    benchmark: str, source: Path, python: str, version: str
) -> BenchmarkManifest:
    metadata = await asyncio.to_thread(source_metadata, benchmark, source)
    if benchmark == "injecagent":
        content = await asyncio.to_thread(injecagent_catalog, source, version)
    else:
        with tempfile.TemporaryDirectory(prefix="attacker-benchmark-catalog-") as temporary:
            path = Path(temporary) / "catalog.json"
            code = await run_worker(
                python,
                [
                    "catalog",
                    "--source",
                    str(source.resolve()),
                    "--version",
                    version,
                    "--output",
                    str(path),
                ],
                180,
            )
            if code != 0:
                raise ValueError(
                    "upstream catalog failed; check the documented isolated dependencies"
                )
            content = read_json(path)
    protocol = (
        {"attack": "important_instructions"}
        if benchmark == "agentdojo"
        else {"agent": "prompted", "prompt_type": "InjecAgent"}
    )
    return BenchmarkManifest.model_validate(
        {**metadata, "version": version, "protocol": protocol, **content}
    )


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.benchmark_command == "catalog":
        manifest = await build_catalog(
            args.benchmark,
            args.source,
            args.python,
            args.version or str(SOURCES[args.benchmark]["version"]),
        )
        write_json(args.output, manifest.model_dump(mode="json"))
        return {
            "manifest": str(args.output.resolve()),
            "benchmark": manifest.benchmark,
            "revision": manifest.revision,
            "cases": len(manifest.cases),
            "attacks": sum(case.kind == "attack" for case in manifest.cases),
            "controls": sum(case.kind == "control" for case in manifest.cases),
            "not_evaluable": sum(case.validation_error is not None for case in manifest.cases),
            "status": "catalogued; not executed",
        }

    database = Database.from_settings(settings.database)
    await database.initialize()
    repository = RunRepository(database.session_factory)
    service = BenchmarkService(repository)
    try:
        if args.benchmark_command == "compare":
            return await service.compare(args.source_run_id, args.replay_run_id)
        if args.report.exists():
            raise FileExistsError("report already exists; choose a new path")
        manifest = BenchmarkManifest.model_validate(read_json(args.manifest))
        manifest = select_cases(manifest, args.case_id, args.limit)
        worker_status = "not_launched"
        if args.benchmark_command == "run":
            if args.limit is None and not args.case_id:
                raise ValueError("run requires explicit --limit or --case-id")
            if not 1 <= args.max_calls <= 10000:
                raise ValueError("max-calls must be between 1 and 10000")
            if manifest.benchmark == "injecagent" and args.defense:
                raise ValueError(
                    "InjecAgent runner uses the upstream InjecAgent prompt without defense variants"
                )
            canonical = await build_catalog(
                manifest.benchmark, args.source, args.python, manifest.version
            )
            selected = select_cases(canonical, [case.id for case in manifest.cases], None)
            if selected != manifest:
                raise ValueError("manifest differs from the pinned upstream environment or Cases")
            args.workdir.mkdir(parents=True, exist_ok=False)
            request_path = args.workdir / "request.json"
            write_json(
                request_path,
                {
                    "manifest": manifest.model_dump(mode="json"),
                    "model": args.model,
                    "defense": args.defense,
                    "max_calls": args.max_calls,
                },
            )
            args.results = args.workdir / "results"
            try:
                code = await run_worker(
                    args.python,
                    [
                        "run",
                        "--source",
                        str(args.source.resolve()),
                        "--request",
                        str(request_path.resolve()),
                        "--output",
                        str(args.results.resolve()),
                    ],
                    args.timeout,
                )
                worker_status = "completed" if code == 0 else f"failed:{code}"
            except TimeoutError:
                worker_status = "timed_out"
            args.target = args.model + (f"-{args.defense}" if args.defense else "")
            if manifest.benchmark == "agentdojo":
                args.target = f"openai-compatible:{args.target}"
        if not args.results.is_dir():
            raise ValueError(f"upstream result directory is missing (worker={worker_status})")
        observations = load_observations(manifest, args.results, args.target)
        run_id = await service.ingest(
            manifest,
            observations,
            args.target,
            execution={
                "origin": args.benchmark_command,
                "worker_status": worker_status,
                "max_model_calls": getattr(args, "max_calls", None),
                "timeout_seconds": getattr(args, "timeout", None),
            },
        )
        report = ReportService(repository)
        text = await report.build_markdown(run_id)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as stream:
            stream.write(text)
        facts = await report.build_json(run_id)
        return {
            "run_id": run_id,
            "target": args.target,
            "worker_status": worker_status,
            "report": str(args.report.resolve()),
            "benchmark": facts["benchmark_summary"],
        }
    finally:
        await database.dispose()
