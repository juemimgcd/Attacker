"""有界、按注册顺序执行的生命周期回调；Hook 只能观察或收紧执行。"""

import asyncio
import math
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from app.agent.state import RunState
from app.schemas.graybox_schema import PlannerContext

type HookName = Literal["before_model", "after_model", "before_tool", "after_tool", "after_turn"]


@dataclass(frozen=True)
class Decision:
    stop: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        if type(self.stop) is not bool or not isinstance(self.reason, str):
            raise TypeError("hook decision requires a bool stop and a string reason")
        if self.stop and not self.reason.strip():
            raise ValueError("hook denial or stop requires a reason")


@dataclass(frozen=True)
class HookContext:
    """每个回调收到独立副本，不暴露 Runtime、Repository 或运行时凭据。"""

    state: RunState
    planner_context: PlannerContext | None = None
    tool_name: str | None = None


type Callback = Callable[[HookContext], Awaitable[Decision | None]]


class Hooks:
    def __init__(self, *, timeout: float = 5.0) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("hook timeout must be positive and finite")
        self.timeout = timeout
        self._callbacks: dict[HookName, list[Callback]] = {
            name: []
            for name in ("before_model", "after_model", "before_tool", "after_tool", "after_turn")
        }

    def register(self, name: HookName, callback: Callback) -> None:
        self._callbacks[name].append(callback)

    async def invoke(self, name: HookName, context: HookContext) -> Decision:
        callbacks = tuple(self._callbacks[name])
        if not callbacks:
            return Decision()
        async with asyncio.timeout(self.timeout):
            for callback in callbacks:
                result = await callback(deepcopy(context))
                if result is None:
                    continue
                if name not in {"before_tool", "after_turn"}:
                    raise TypeError(f"{name} is read-only and must return None")
                if not isinstance(result, Decision):
                    raise TypeError("decision hook must return Decision or None")
                if result.stop:
                    return result
        return Decision()
