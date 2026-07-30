from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import BaseModel


def _load_symbol(package_path: Path, entrypoint: str) -> type:
    sys.dont_write_bytecode = True
    module_name, _, symbol_name = entrypoint.partition(":")
    module_path = package_path / (
        module_name if module_name.endswith(".py") else f"{module_name.replace('.', '/')}.py"
    )
    if not module_path.is_file():
        raise ImportError(f"cannot load equipment module {module_path}")
    module = ModuleType(f"attacker_equipment_{package_path.name}")
    module.__file__ = str(module_path)
    code = compile(module_path.read_bytes(), str(module_path), "exec")
    exec(code, module.__dict__)  # noqa: S102 - execute the verified source, never cached bytecode
    symbol = getattr(module, symbol_name)
    if not isinstance(symbol, type):
        raise TypeError("equipment entrypoint must be a class")
    return symbol


async def execute_request(request: dict[str, Any]) -> Any:
    package_path = Path(str(request["package_path"])).resolve()
    symbol = _load_symbol(package_path, str(request["entrypoint"]))
    instance = symbol()
    method = getattr(instance, str(request["method"]))
    result = method(**dict(request.get("kwargs", {})))
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    return result


async def _main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        result = await execute_request(request)
        sys.stdout.write(json.dumps({"ok": True, "result": result}, default=str))
        return 0
    # The worker boundary must serialize arbitrary package failures instead of crashing Core.
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "equipment_worker_error",
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
