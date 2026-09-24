"""Cross-process, fail-closed concurrency permits for orchestrated work."""

import asyncio
import hashlib
import secrets
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

from app.repositories.concurrency_repository import ConcurrencyRepository
from app.schemas.graybox_schema import PlannerConfig
from app.schemas.target_schema import TargetConfig
from app.services.target_binding import canonical_target_ref
from conf.settings import OrchestratorConcurrencySettings

T = TypeVar("T")
Quota = tuple[str, int]


def _hashed_key(kind: str, identity: str) -> str:
    return f"{kind}:{hashlib.sha256(identity.encode()).hexdigest()}"


class SharedConcurrencyLimiter:
    def __init__(
        self, repository: ConcurrencyRepository, settings: OrchestratorConcurrencySettings
    ) -> None:
        self.repository = repository
        self.settings = settings

    def worker_quotas(self, target: TargetConfig, planner: PlannerConfig) -> list[Quota]:
        identity = (
            f"instance:{target.provider_instance_id}"
            if target.provider_instance_id
            else canonical_target_ref(target)
        )
        quotas: list[Quota] = [
            ("global:workers", self.settings.global_workers),
            ("realm:default", self.settings.realm_workers),
            (_hashed_key("target", identity), self.settings.target_workers),
        ]
        if planner.backend == "openai_compatible":
            quotas.extend(self.model_quotas(planner))
        return quotas

    def model_quotas(self, config: PlannerConfig) -> list[Quota]:
        if config.endpoint is None:
            return []
        return [
            (
                _hashed_key("model", f"{config.provider_id}:{config.endpoint}"),
                self.settings.model_workers,
            )
        ]

    async def _acquire(self, quotas: list[Quota], deadline: float) -> str:
        token = secrets.token_hex(32)
        while True:
            acquired = 0
            try:
                for key, capacity in quotas:
                    if not await self.repository.try_claim(
                        key, capacity, token, self.settings.lease_seconds
                    ):
                        break
                    acquired += 1
                if acquired == len(quotas):
                    return token
            except BaseException:
                await self.repository.release(token)
                raise
            await self.repository.release(token)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("shared concurrency wait exceeded coordinator deadline")
            await asyncio.sleep(min(self.settings.poll_seconds, remaining))

    async def _heartbeat(self, token: str, expected_slots: int) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            renewed = await self.repository.renew(
                token, expected_slots, self.settings.lease_seconds
            )
            if not renewed:
                raise RuntimeError("shared concurrency lease lost")

    async def run(
        self, quotas: list[Quota], deadline: float, operation: Callable[[], Awaitable[T]]
    ) -> T:
        if not quotas:
            return await operation()
        token = await self._acquire(quotas, deadline)
        work_task: asyncio.Task[T] | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        try:

            async def invoke() -> T:
                return await operation()

            work_task = asyncio.create_task(invoke())
            heartbeat_task = asyncio.create_task(self._heartbeat(token, len(quotas)))
            done, _ = await asyncio.wait(
                {work_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat_task in done:
                raise RuntimeError("shared concurrency lease lost")
            result = await work_task
            if not await self.repository.renew(token, len(quotas), self.settings.lease_seconds):
                raise RuntimeError("shared concurrency lease lost")
            return result
        finally:
            for task in (work_task, heartbeat_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(
                    cast(Awaitable[Any], task)
                    for task in (work_task, heartbeat_task)
                    if task is not None
                ),
                return_exceptions=True,
            )
            await self.repository.release(token)
