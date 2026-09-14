"""CLI for pluggable Equipment benchmarks, separate from public benchmark imports."""

import argparse
import json
from typing import Any

from app.schemas.equipment_benchmark_schema import EquipmentBenchmarkRequest
from app.services.equipment_benchmark_service import EquipmentBenchmarkService
from app.services.equipment_service import EquipmentService
from app.services.harness_service import HarnessService
from app.services.report_service import ReportService


def add_parser(commands: Any) -> None:
    benchmark = commands.add_parser("benchmark")
    actions = benchmark.add_subparsers(dest="benchmark_action", required=True)
    for action in ("validate", "run"):
        parser = actions.add_parser(action)
        parser.add_argument("benchmark_id")
        parser.add_argument("--target", required=True)
        parser.add_argument("--version")
        parser.add_argument("--task-id", action="append", dest="task_ids")
        parser.add_argument("--approve-capability", action="append", default=[])
    report = actions.add_parser("report")
    report.add_argument("run_id")
    report.add_argument("--format", choices=["json", "markdown"], default="json")


async def execute(
    args: argparse.Namespace, equipment: EquipmentService, harness: HarnessService
) -> Any:
    service = EquipmentBenchmarkService(equipment, harness)
    if args.benchmark_action == "report":
        reports = ReportService(service.runs, equipment.repository)
        result = await reports.build_json(args.run_id)
        if result["run"]["mode"] != "equipment_benchmark":
            raise ValueError("the requested run is not an Equipment benchmark")
        if args.format == "markdown":
            from app.equipment.benchmark_metrics import benchmark_markdown

            return {"markdown": benchmark_markdown(result)}
        return result
    request = EquipmentBenchmarkRequest(
        target=args.target,
        version=args.version,
        task_ids=args.task_ids,
        approved_high_risk_capabilities=args.approve_capability,
    )
    if args.benchmark_action == "validate":
        plan = await service.prepare(args.benchmark_id, request)
        return {
            "valid": True,
            "benchmark": plan["manifest"].model_dump(mode="json"),
            "task_count": len(plan["tasks"]),
            "bindings": {
                name: binding.model_dump(mode="json") for name, binding in plan["bindings"].items()
            },
            "external_calls": 0,
        }
    # Core's CLI prints JSON once, preserving the complete report for redirection.
    result = await service.run(args.benchmark_id, request)
    json.dumps(result, allow_nan=False)
    return result
