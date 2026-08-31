"""持久 Job 分发与 Worker 循环；物理执行和租约状态由数据库协调。"""

from __future__ import annotations

import asyncio
import platform
from contextlib import suppress
from typing import Any

from loguru import logger

from app.observability import record_job_event
from app.repositories.job_repository import JobRepository
from app.schemas.graybox_schema import DeterministicGrayBoxRunRequest, GrayBoxRunRequest
from app.schemas.job_schema import JobKind, JobStatus, RunJobCreate
from app.schemas.run_schema import DeterministicRunRequest
from app.schemas.stateful_schema import StatefulRunRequest
from app.services.run_lifecycle import (
    RunCreatedHook,
    RunCreationCancelled,
    bind_requested_run_id,
    requested_run_id_from_hook,
)
from conf.settings import WorkerSettings


class JobCancellationRequested(RuntimeError):
    """Worker 观察到持久取消请求；与租约故障区分处理。"""


class JobApplicationService:
    """持久 Job 的应用边界；HTTP 层不直接操作租约仓库。"""

    def __init__(self, repository: JobRepository, *, default_max_attempts: int) -> None:
        self.repository = repository
        self.default_max_attempts = default_max_attempts

    async def enqueue(self, request: RunJobCreate) -> dict[str, Any]:
        return await self.repository.enqueue(
            request,
            default_max_attempts=self.default_max_attempts,
        )

    async def list(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return await self.repository.list(status=status, limit=limit)

    async def get(self, job_id: str) -> dict[str, Any]:
        return await self.repository.get(job_id)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        return await self.repository.cancel(job_id)

    async def retry(self, job_id: str) -> dict[str, Any]:
        return await self.repository.retry(job_id)

    async def metrics_snapshot(self, *, stale_after_seconds: int) -> dict[str, Any]:
        return await self.repository.metrics_snapshot(stale_after_seconds=stale_after_seconds)


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

    async def dispatch(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        on_run_created: RunCreatedHook | None = None,
    ) -> dict[str, Any]:
        return await self._dispatch(
            kind,
            payload,
            on_run_created=on_run_created,
        )

    async def _dispatch(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        on_run_created: RunCreatedHook | None,
    ) -> dict[str, Any]:
        requested_run_id = requested_run_id_from_hook(on_run_created)
        if kind == JobKind.deterministic.value:
            result = await self.deterministic_run_service.run(
                DeterministicRunRequest.model_validate(payload),
                on_run_created=on_run_created,
                requested_run_id=requested_run_id,
            )
        elif kind == JobKind.adaptive.value:
            result = await self.adaptive_run_service.start(
                GrayBoxRunRequest.model_validate(payload),
                on_run_created=on_run_created,
                requested_run_id=requested_run_id,
            )
        elif kind == JobKind.deterministic_graybox.value:
            result = await self.deterministic_graybox_service.run(
                DeterministicGrayBoxRunRequest.model_validate(payload),
                on_run_created=on_run_created,
                requested_run_id=requested_run_id,
            )
        elif kind == JobKind.stateful.value:
            result = await self.stateful_run_service.run(
                StatefulRunRequest.model_validate(payload),
                on_run_created=on_run_created,
                requested_run_id=requested_run_id,
            )
        else:
            raise ValueError(f"unsupported job kind {kind}")
        reference = self._result_reference(result)
        if requested_run_id is not None and reference["run_id"] != requested_run_id:
            raise RuntimeError("job dispatch returned a different Run identifier")
        return reference

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
        monitor: asyncio.Task[None] | None = None
        dispatch_task: asyncio.Task[dict[str, Any]] | None = None
        try:
            started = await self.repository.mark_running(
                job_id,
                worker_id=self.worker_id,
                lease_token=lease_token,
            )
            if not started:
                await self.repository.fail(
                    job_id,
                    worker_id=self.worker_id,
                    lease_token=lease_token,
                    error_code="job_cancelled",
                    error_summary="cancellation was accepted before the run started",
                )
                record_job_event("cancelled")
                return
            monitor = asyncio.create_task(
                self._monitor_lease_and_cancellation(job_id, lease_token),
                name=f"attacker-job-monitor-{job_id}",
            )
            run_creation_cancellation = asyncio.Event()

            async def bind_run(run_id: str) -> bool:
                binding = asyncio.create_task(
                    self.repository.bind_run(
                        job_id,
                        run_id=run_id,
                        worker_id=self.worker_id,
                        lease_token=lease_token,
                    ),
                    name=f"attacker-job-bind-run-{job_id}",
                )
                try:
                    cancel_requested = bool(await asyncio.shield(binding))
                except asyncio.CancelledError:
                    # Run 已存在时必须先保存关联，随后才把取消传回 Run Service。
                    cancel_requested = bool(await binding)
                    if cancel_requested:
                        run_creation_cancellation.set()
                    raise
                if cancel_requested:
                    run_creation_cancellation.set()
                return cancel_requested

            run_created_hook = bind_requested_run_id(bind_run, job_id)

            dispatch_task = asyncio.create_task(
                self.dispatcher.dispatch(
                    str(job["kind"]),
                    dict(job["payload"]),
                    on_run_created=run_created_hook,
                ),
                name=f"attacker-job-dispatch-{job_id}",
            )
            done, _ = await asyncio.wait(
                {dispatch_task, monitor},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if monitor in done:
                monitor_error = monitor.exception()
                if isinstance(monitor_error, JobCancellationRequested):
                    if run_creation_cancellation.is_set():
                        await asyncio.gather(dispatch_task, return_exceptions=True)
                    else:
                        dispatch_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await dispatch_task
                    await self.repository.fail(
                        job_id,
                        worker_id=self.worker_id,
                        lease_token=lease_token,
                        error_code="job_cancelled",
                        error_summary="cancellation was requested while the run was executing",
                    )
                    record_job_event("cancelled")
                    return
                raise RuntimeError(
                    "job lease/cancellation monitor stopped before execution completed"
                ) from monitor_error
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
            completed = await self.repository.complete(
                job_id,
                worker_id=self.worker_id,
                lease_token=lease_token,
                result=result,
            )
            if completed["status"] == JobStatus.cancelled.value:
                record_job_event("cancelled")
                return
            record_job_event("succeeded")
            logger.info("durable job completed", job_id=job_id, worker_id=self.worker_id)
        except RunCreationCancelled:
            with suppress(Exception):
                await self.repository.fail(
                    job_id,
                    worker_id=self.worker_id,
                    lease_token=lease_token,
                    error_code="job_cancelled",
                    error_summary="cancellation was accepted before Run execution",
                )
            record_job_event("cancelled")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - worker boundary serializes package failures
            error_type = type(exc).__name__[:100]
            safe_error = "job execution failed"
            with suppress(Exception):
                await self.repository.fail(
                    job_id,
                    worker_id=self.worker_id,
                    lease_token=lease_token,
                    error_code=error_type,
                    error_summary=safe_error,
                )
            record_job_event("failed")
            logger.error(
                "durable job failed",
                job_id=job_id,
                worker_id=self.worker_id,
                error_type=error_type,
            )
        finally:
            if dispatch_task is not None and not dispatch_task.done():
                dispatch_task.cancel()
                with suppress(asyncio.CancelledError):
                    await dispatch_task
            if monitor is not None:
                if not monitor.done():
                    monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)

    async def _monitor_lease_and_cancellation(self, job_id: str, lease_token: str) -> None:
        loop = asyncio.get_running_loop()
        next_heartbeat = loop.time() + self.settings.heartbeat_seconds
        while True:
            if await self.repository.is_cancel_requested(job_id):
                raise JobCancellationRequested("job cancellation was requested")
            if loop.time() >= next_heartbeat:
                await self.repository.heartbeat(
                    job_id,
                    worker_id=self.worker_id,
                    lease_token=lease_token,
                    lease_seconds=self.settings.lease_seconds,
                )
                next_heartbeat = loop.time() + self.settings.heartbeat_seconds
            await asyncio.sleep(min(self.settings.poll_seconds, self.settings.heartbeat_seconds))

    async def _worker_heartbeat(self, *, draining: bool) -> None:
        await self.repository.worker_heartbeat(
            worker_id=self.worker_id,
            active_jobs=len(self._tasks),
            draining=draining,
            metadata={"hostname": platform.node(), "python": platform.python_version()},
        )
