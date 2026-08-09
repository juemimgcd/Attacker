"""Run 创建生命周期钩子；用于把后台 Job 立即绑定到实际执行实例。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

RunCreatedHook = Callable[[str], Awaitable[None]]


async def notify_run_created(hook: RunCreatedHook | None, run_id: str) -> None:
    if hook is not None:
        await hook(run_id)
