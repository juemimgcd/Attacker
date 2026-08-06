"""持久 Job 分发与 Worker 循环；物理执行和租约状态由数据库协调。"""

from __future__ import annotations

import asyncio
import platform
from contextlib import suppress
from typing import Any

from loguru import logger

from app.equipment.security import redact
from app.observability import record_job_event
from app.repositories.job_repository import JobRepository
from app.schemas.graybox_schema import DeterministicGrayBoxRunRequest, GrayBoxRunRequest
from app.schemas.job_schema import JobKind
from app.schemas.run_schema import DeterministicRunRequest
from app.schemas.stateful_schema import StatefulRunRequest
from conf.settings import WorkerSettings


class JobDispatcher:
    """把已校验 Job payload 路由到四类 Run Service，并只返回有限结果引用。"""

    def __init__(
        self,
        *,
        deterministic_run_service: Any,
        adaptive_run_service: Any,
        deterministic_graybox_service: Any,
        stateful_run_service: Any,
    ) -> None:
        self.deterministic_run_service = deterministic_run_service
        self.adaptive_run_service = adaptive_run_service
        self.deterministic_graybox_service = deterministic_graybox_service
        self.stateful_run_service = stateful_run_service

    async def dispatch(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == JobKind.deterministic.value:
            result = await self.deterministic_run_service.run(
                DeterministicRunRequest.model_validate(payload)
            )
        elif kind == JobKind.adaptive.value:
            result = await self.adaptive_run_service.start(
                GrayBoxRunRequest.model_validate(payload)
            )
        elif kind == JobKind.deterministic_graybox.value:
            result = await self.deterministic_graybox_service.run(
                DeterministicGrayBoxRunRequest.model_validate(payload)
            )
        elif kind == JobKind.stateful.value:
            result = await self.stateful_run_service.run(StatefulRunRequest.model_validate(payload))
        else:
            raise ValueError(f"unsupported job kind {kind}")
        return self._result_reference(result)

    @staticmethod
    def _result_reference(result: dict[str, Any]) -> dict[str, Any]:
        run_value = result.get("run")
        run: dict[str, Any] = run_value if isinstance(run_value, dict) else {}
        run_id = result.get("run_id") or run.get("id")
        if not isinstance(run_id, str) or not run_id:
            raise RuntimeError("run service did not return a run identifier")
        return {
            "run_id": run_id,
            "status": result.get("status") or run.get("status") or "completed",
            "thread_id": result.get("thread_id") or run.get("thread_id"),
            "report_path": f"/runs/{run_id}/report.json",
        }


class JobWorker:
    """领取短租约、执行任务、持续心跳，并在失去租约后停止提交结果。"""

    def __init__(
        self,
        *,
        worker_id: str,
        repository: JobRepository,
        dispatcher: JobDispatcher,
        settings: WorkerSettings,
    ) -> None:
        self.worker_id = worker_id
        self.repository = repository
        self.dispatcher = dispatcher
        self.settings = settings
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self, stop_event: asyncio.Event, *, once: bool = False) -> None:
        """在并发上限内领取任务；关闭时停止领取并等待已有任务收敛。"""

        await self.repository.recover_expired()
        await self._worker_heartbeat(draining=False)
        try:
            while not stop_event.is_set():
                self._tasks = {task for task in self._tasks if not task.done()}
                claimed_any = False
                while len(self._tasks) < self.settings.concurrency and not stop_event.is_set():
                    job = await self.repository.claim(
                        worker_id=self.worker_id,
                        lease_seconds=self.settings.lease_seconds,
                    )
                    if job is None:
                        break
                    claimed_any = True
                    task = asyncio.create_task(
                        self._execute(job),
                        name=f"attacker-job-{job['id']}",
                    )
                    self._tasks.add(task)
                    record_job_event("claimed")
                await self._worker_heartbeat(draining=False)
                if once and not claimed_any:
                    break
                await asyncio.sleep(self.settings.poll_seconds)
        finally:
            await self._worker_heartbeat(draining=True)
            if self._tasks:
                _, pending = await asyncio.wait(
                    self._tasks,
                    timeout=self.settings.shutdown_grace_seconds,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            await self._worker_heartbeat(draining=True)

    async def _execute(self, job: dict[str, Any]) -> None:
        """持有租约期间分发任务；心跳失败或取消后不再提交成功结果。"""

        job_id = str(job["id"])
        lease_token = str(job["lease_token"])
        heartbeat: asyncio.Task[None] | None = None
        dispatch_task: asyncio.Task[dict[str, Any]] | None = None
        try:
            if await self.repository.is_cancel_requested(job_id):
                raise RuntimeError("job was cancelled before execution")
            await self.repository.mark_running(
                job_id,
                worker_id=self.worker_id,
                lease_token=lease_token,
            )
            heartbeat = asyncio.create_task(
                self._lease_heartbeat(job_id, lease_token),
                name=f"attacker-job-heartbeat-{job_id}",
            )
            dispatch_task = asyncio.create_task(
                self.dispatcher.dispatch(str(job["kind"]), dict(job["payload"])),
                name=f"attacker-job-dispatch-{job_id}",
            )
            done, _ = await asyncio.wait(
                {dispatch_task, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                dispatch_task.cancel()
                with suppress(asyncio.CancelledError):
                    await dispatch_task
                heartbeat_error = heartbeat.exception()
                raise RuntimeError(
                    "job lease heartbeat stopped before execution completed"
                ) from heartbeat_error
            result = dispatch_task.result()
            if await self.repository.is_cancel_requested(job_id):
                await self.repository.fail(
                    job_id,
                    worker_id=self.worker_id,
                    lease_token=lease_token,
                    error_code="job_cancelled",
                    error_summary="cancellation was requested while the run was executing",
                )
                record_job_event("cancelled")
                return
            await self.repository.complete(
                job_id,
                worker_id=self.worker_id,
                lease_token=lease_token,
                result=result,
            )
            record_job_event("succeeded")
            logger.info("durable job completed", job_id=job_id, worker_id=self.worker_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - worker boundary serializes package failures
            safe_error = str(redact(str(exc))) or type(exc).__name__
            with suppress(Exception):
                await self.repository.fail(
                    job_id,
                    worker_id=self.worker_id,
                    lease_token=lease_token,
                    error_code=type(exc).__name__[:100],
                    error_summary=safe_error,
                )
            record_job_event("failed")
            logger.error(
                "durable job failed",
                job_id=job_id,
                worker_id=self.worker_id,
                error_type=type(exc).__name__,
                error_summary=safe_error,
            )
        finally:
            if dispatch_task is not None and not dispatch_task.done():
                dispatch_task.cancel()
                with suppress(asyncio.CancelledError):
                    await dispatch_task
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

    async def _lease_heartbeat(self, job_id: str, lease_token: str) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            await self.repository.heartbeat(
                job_id,
                worker_id=self.worker_id,
                lease_token=lease_token,
                lease_seconds=self.settings.lease_seconds,
            )

    async def _worker_heartbeat(self, *, draining: bool) -> None:
        await self.repository.worker_heartbeat(
            worker_id=self.worker_id,
            active_jobs=len(self._tasks),
            draining=draining,
            metadata={"hostname": platform.node(), "python": platform.python_version()},
        )
