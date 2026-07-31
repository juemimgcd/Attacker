"""按装备信任级别选择 Core 进程、JSON 子进程或强容器沙箱执行。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.equipment.metrics import EquipmentMetrics
from app.equipment.sdk import provider_secret_scope
from app.equipment.worker import execute_request
from app.schemas.equipment_schema import TrustLevel
from conf.settings import EquipmentSettings


class EquipmentProtocolError(RuntimeError):
    """装备进程未遵守有界 JSON stdin/stdout 协议。"""


class EquipmentRunner:
    """只负责隔离与协议，不授予 Capability，也不决定安全 Finding。"""

    def __init__(
        self,
        settings: EquipmentSettings,
        metrics: EquipmentMetrics | None = None,
    ) -> None:
        self.settings = settings
        self.metrics = metrics

    async def execute(
        self,
        package: dict[str, Any],
        *,
        method: str,
        kwargs: dict[str, Any],
        workspace: Path,
        timeout_seconds: float,
        secret_environment: dict[str, str] | None = None,
    ) -> Any:
        """信任级别只决定隔离方式；Capability 授权必须在调用前完成。"""

        trust = TrustLevel(package["trust_level"])
        request = {
            "package_path": package["source_path"],
            "entrypoint": package["manifest"]["entrypoint"],
            "method": method,
            "kwargs": kwargs,
        }
        workspace.mkdir(parents=True, exist_ok=True)
        if trust == TrustLevel.trusted_builtin:
            if package.get("source_type") != "builtin":
                raise PermissionError(
                    "trusted_builtin execution requires Core-owned package provenance"
                )
            with provider_secret_scope(secret_environment or {}):
                return await asyncio.wait_for(execute_request(request), timeout=timeout_seconds)
        if trust == TrustLevel.untrusted:
            return await self._container(
                request,
                package=package,
                workspace=workspace,
                timeout_seconds=timeout_seconds,
                secret_environment=secret_environment or {},
            )
        return await self._subprocess(
            request,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
            secret_environment=secret_environment or {},
        )

    async def _subprocess(
        self,
        request: dict[str, Any],
        *,
        workspace: Path,
        timeout_seconds: float,
        secret_environment: dict[str, str],
    ) -> Any:
        """企业可信包使用有界 JSON 子进程；该边界不是恶意代码沙箱。"""

        project_root = Path(__file__).resolve().parents[2]
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(project_root),
            **secret_environment,
        }
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.equipment.worker",
            cwd=workspace,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        payload = json.dumps(
            request,
            separators=(",", ":"),
            default=lambda value: (
                value.model_dump(mode="json") if isinstance(value, BaseModel) else str(value)
            ),
        ).encode()
        try:
            stdout, stderr = await asyncio.wait_for(
                self._communicate_bounded(process, payload),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            await self._terminate_tree(process)
            raise TimeoutError("equipment execution timed out") from None
        except asyncio.CancelledError:
            await self._terminate_tree(process)
            raise
        except EquipmentProtocolError:
            await self._terminate_tree(process)
            raise
        try:
            response = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EquipmentProtocolError("malformed equipment stdout") from exc
        if process.returncode != 0 or not response.get("ok"):
            error = response.get("error", {})
            raise EquipmentProtocolError(
                f"{error.get('code', 'equipment_failed')}: "
                f"{error.get('message', stderr.decode(errors='replace'))}"
            )
        return response["result"]

    async def _container(
        self,
        request: dict[str, Any],
        *,
        package: dict[str, Any],
        workspace: Path,
        timeout_seconds: float,
        secret_environment: dict[str, str],
    ) -> Any:
        """不可信包只在 Linux 强约束容器后端可用时执行，否则安全失败。"""

        if (
            os.name == "nt"
            or not self.settings.allow_untrusted
            or not self.settings.sandbox_image
            or not shutil.which("docker")
        ):
            raise PermissionError(
                "strong sandbox unavailable: untrusted equipment cannot run on this platform"
            )
        request = {**request, "package_path": "/package"}
        command = [
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--read-only",
            "--network",
            "none",
            "--cpus",
            "1",
            "--memory",
            "256m",
            "--pids-limit",
            "64",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65532:65532",
            "--volume",
            f"{Path(package['source_path']).resolve()}:/package:ro",
            "--volume",
            f"{workspace.resolve()}:/workspace:rw",
            "--workdir",
            "/workspace",
        ]
        for name in secret_environment:
            command.extend(["--env", name])
        command.extend(
            [
                self.settings.sandbox_image,
                "python",
                "-m",
                "app.equipment.worker",
            ]
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            env={
                "PATH": os.environ.get("PATH", ""),
                **secret_environment,
            },
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = json.dumps(
            request,
            separators=(",", ":"),
            default=lambda value: (
                value.model_dump(mode="json") if isinstance(value, BaseModel) else str(value)
            ),
        ).encode()
        try:
            stdout, stderr = await asyncio.wait_for(
                self._communicate_bounded(process, payload),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            if self.metrics is not None:
                self.metrics.increment("sandbox_termination")
            raise TimeoutError("sandbox execution timed out") from None
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            if self.metrics is not None:
                self.metrics.increment("sandbox_termination")
            raise
        except EquipmentProtocolError:
            process.kill()
            await process.wait()
            if self.metrics is not None:
                self.metrics.increment("sandbox_termination")
            raise
        try:
            response = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EquipmentProtocolError("malformed sandbox stdout") from exc
        if process.returncode != 0 or not response.get("ok"):
            error = response.get("error", {})
            raise EquipmentProtocolError(
                f"{error.get('code', 'sandbox_failed')}: "
                f"{error.get('message', stderr.decode(errors='replace'))}"
            )
        return response["result"]

    async def _communicate_bounded(
        self,
        process: asyncio.subprocess.Process,
        payload: bytes,
    ) -> tuple[bytes, bytes]:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise EquipmentProtocolError("equipment process pipes are unavailable")
        process.stdin.write(payload)
        await process.stdin.drain()
        process.stdin.close()
        stdout_task = asyncio.create_task(
            self._read_limited(
                process.stdout,
                self.settings.max_stdout_bytes,
                "stdout",
            )
        )
        stderr_task = asyncio.create_task(
            self._read_limited(
                process.stderr,
                self.settings.max_stderr_bytes,
                "stderr",
            )
        )
        try:
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            await process.wait()
            return stdout, stderr
        finally:
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()

    @staticmethod
    async def _read_limited(
        stream: asyncio.StreamReader,
        limit: int,
        stream_name: str,
    ) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while chunk := await stream.read(65_536):
            size += len(chunk)
            if size > limit:
                raise EquipmentProtocolError(f"equipment {stream_name} limit exceeded")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    async def _terminate_tree(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt" and shutil.which("taskkill"):
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
        await process.wait()
