"""主 Agent / Subagent 的命令行入口。"""

import argparse
from pathlib import Path
from typing import Any

from app.runtime import create_runtime
from app.schemas.subagent_schema import SubagentRunRequest


def add_parser(commands: Any) -> None:
    parser = commands.add_parser("subagents", help="delegate adaptive tests and summarize evidence")
    actions = parser.add_subparsers(dest="subagent_command", required=True)
    run = actions.add_parser("run")
    run.add_argument("--config", required=True)
    report = actions.add_parser("report")
    report.add_argument("coordinator_id")
    actions.add_parser("list")


async def execute(args: argparse.Namespace) -> Any:
    payload = None
    if args.subagent_command == "run":
        payload = SubagentRunRequest.model_validate_json(Path(args.config).read_text())
    async with create_runtime() as runtime:
        if payload is not None:
            return await runtime.subagent_service.start(payload)
        if args.subagent_command == "report":
            return await runtime.subagent_service.report(args.coordinator_id)
        return await runtime.subagent_service.repository.list_recent(20)
