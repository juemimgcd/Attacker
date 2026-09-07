"""业务测试配置验证、并发执行和适合 CI 的退出状态。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.services.business_test_io import credentials, load_suite
from app.services.business_test_service import BusinessTestService


def add_parser(commands: Any) -> None:
    parser = commands.add_parser(
        "business-test", help="configured concurrent business dialogue tests"
    )
    actions = parser.add_subparsers(dest="business_test_command", required=True)
    validate = actions.add_parser(
        "validate", help="validate configuration without network requests"
    )
    validate.add_argument("--config", type=Path, required=True)
    validate.add_argument(
        "--check-env", action="store_true", help="also check credential references"
    )
    run = actions.add_parser(
        "run", help="run scenarios concurrently and export JSON/JUnit evidence"
    )
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output", type=Path, default=Path("data/business-tests"))


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    suite = load_suite(args.config)
    if args.business_test_command == "validate":
        if args.check_env:
            credentials(suite)
        return {
            "status": "valid",
            "name": suite.name,
            "scenarios": len(suite.scenarios),
            "limits": suite.limits.model_dump(),
            "credentials_checked": args.check_env,
            "network_checked": False,
        }
    service = BusinessTestService(suite, args.output.resolve())
    return await service.run()


def run_cli(args: argparse.Namespace) -> int:
    try:
        result = asyncio.run(execute(args))
    except KeyboardInterrupt:
        print(
            json.dumps(
                {"status": "cancelled", "detail": "inspect the run directory for partial evidence"}
            )
        )
        return 130
    except ValidationError as exc:
        # Pydantic's default rendering includes rejected input, possibly containing a secret.
        print(
            json.dumps(
                {
                    "status": "configuration_error",
                    "errors": [
                        {"path": ".".join(map(str, error["loc"])), "type": error["type"]}
                        for error in exc.errors(
                            include_input=False, include_context=False, include_url=False
                        )
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 2
    except ValueError as exc:
        print(
            json.dumps({"status": "configuration_error", "message": str(exc)}, ensure_ascii=False)
        )
        return 2
    except (OSError, yaml.YAMLError, RecursionError) as exc:
        print(json.dumps({"status": "configuration_or_io_error", "type": type(exc).__name__}))
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI must not expose secrets in tracebacks
        print(json.dumps({"status": "runner_error", "type": type(exc).__name__}))
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if result.get("passed") is False else 0
