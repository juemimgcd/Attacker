"""Run 创建生命周期钩子；用于把后台 Job 立即绑定到实际执行实例。"""

from __future__ import annotations

from asyncio import CancelledError
from collections.abc import Awaitable, Callable

RunCreatedHook = Callable[[str], Awaitable[bool | None]]


class RunCreationCancelled(CancelledError):
    """Job cancellation committed before the new Run may begin external work."""


class _RequestedRunCreatedHook:
    def __init__(self, hook: RunCreatedHook, run_id: str) -> None:
        self.hook = hook
        self.requested_run_id = run_id

    async def __call__(self, run_id: str) -> bool | None:
        return await self.hook(run_id)


def bind_requested_run_id(hook: RunCreatedHook, run_id: str) -> RunCreatedHook:
    """Attach the preallocated Job/Run identity without changing fake dispatcher APIs."""

    return _RequestedRunCreatedHook(hook, run_id)


def requested_run_id_from_hook(hook: RunCreatedHook | None) -> str | None:
    run_id = getattr(hook, "requested_run_id", None)
    return run_id if isinstance(run_id, str) and run_id else None


async def notify_run_created(hook: RunCreatedHook | None, run_id: str) -> None:
    if hook is not None and await hook(run_id):
        raise RunCreationCancelled("job cancellation was accepted before Run execution")
