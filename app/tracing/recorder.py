"""记录同步/异步调用边界，不修改返回值、不重试目标、不推断隐藏步骤。"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from itertools import islice
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from app.equipment.security import is_sensitive_field
from app.schemas.execution_trace_schema import (
    CaptureSummary,
    ExecutionEvent,
    ExecutionTrace,
    SpanKind,
    TraceCoverage,
    TraceSource,
)
from app.services.prompt_governance import redact_sensitive_text


class ExecutionRecorder:
    """每个 Query 创建一个实例；ContextVar 保留 async 并行调用的父子关系。"""

    def __init__(
        self,
        *,
        output: Path | None = None,
        secret_values: set[str] | None = None,
        redacted_fields: set[str] | None = None,
        max_events: int = 10_000,
        max_data_chars: int = 16_000,
        source: TraceSource = "runtime_wrapper",
    ) -> None:
        if max_events < 2 or max_data_chars < 128:
            raise ValueError("max_events >= 2 and max_data_chars >= 128 are required")
        self.trace_id = uuid4().hex
        self.output = output
        self.secret_values = secret_values or set()
        self.redacted_fields = redacted_fields or set()
        self.max_events = max_events
        self.max_data_chars = max_data_chars
        self.source: TraceSource = source
        self.coverage: TraceCoverage = (
            "model_protocol_only" if source == "model_proxy" else "instrumented_boundaries_only"
        )
        self._parent: ContextVar[str | None] = ContextVar(self.trace_id, default=None)
        self._lock = threading.RLock()
        self._events: list[ExecutionEvent] = []
        self._dropped = 0
        self._errors = 0
        self._sequence = 0
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            # 新建专属文件，避免覆盖已有证据；初始化失败在执行目标之前报错。
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)

    def snapshot(self) -> ExecutionTrace:
        with self._lock:
            return ExecutionTrace(
                trace_id=self.trace_id,
                events=[event.model_copy(deep=True) for event in self._events],
                dropped_events=self._dropped,
                capture_errors=self._errors,
                coverage=self.coverage,
            )

    def finish(self) -> CaptureSummary:
        """所有调用结束后保存采集计数；写入失败不掩盖目标异常。"""
        with self._lock:
            summary = CaptureSummary(
                trace_id=self.trace_id,
                events=len(self._events),
                dropped_events=self._dropped,
                capture_errors=self._errors,
                coverage=self.coverage,
            )
            if self.output is not None:
                try:
                    with self.output.open("a", encoding="utf-8") as stream:
                        stream.write(summary.model_dump_json() + "\n")
                except OSError:
                    self._errors += 1
                    summary.capture_errors = self._errors
            return summary

    def _safe(self, value: Any, depth: int = 0) -> Any:
        if depth > 10:
            return {"capture_omitted": "maximum nesting depth"}
        if isinstance(value, BaseModel):
            return self._safe(value.model_dump(mode="python"), depth + 1)
        if isinstance(value, dict):
            result = {
                redact_sensitive_text(str(key), self.secret_values): (
                    "[REDACTED]"
                    if is_sensitive_field(key, item, self.redacted_fields)
                    else self._safe(item, depth + 1)
                )
                for key, item in islice(value.items(), 128)
            }
            if len(value) > 128:
                return {"captured_items": result, "capture_omitted_items": len(value) - 128}
            return result
        if isinstance(value, (list, tuple)):
            items = [self._safe(item, depth + 1) for item in value[:128]]
            if len(value) > 128:
                return {"captured_items": items, "capture_omitted_items": len(value) - 128}
            return items
        if isinstance(value, str):
            return redact_sensitive_text(value, self.secret_values)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        # 不使用 repr/str，避免泄露客户端对象中的凭据或消费迭代器。
        return {"capture_omitted": type(value).__name__}

    def emit(self, *, data: Any, **fields: Any) -> None:
        """协议采集器可直接写事件；所有入口统一执行脱敏与限额。"""
        with self._lock:
            self._sequence += 1
            if len(self._events) >= self.max_events:
                self._dropped += 1
                return
            try:
                safe = self._safe(data)
                encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False)
                truncated = len(encoded) > self.max_data_chars
                if truncated:
                    safe = {"preview": encoded[: self.max_data_chars], "truncated": True}
                event = ExecutionEvent(
                    trace_id=self.trace_id,
                    sequence=self._sequence,
                    timestamp=datetime.now(UTC),
                    data=safe,
                    truncated=truncated,
                    source=self.source,
                    **fields,
                )
                self._events.append(event)
                if self.output is not None:
                    with self.output.open("a", encoding="utf-8") as stream:
                        stream.write(event.model_dump_json() + "\n")
            except Exception:  # noqa: BLE001 -- telemetry must preserve target behavior
                # 采集失败不替换目标返回值或原始异常；调用方可检查 snapshot。
                self._errors += 1

    @contextmanager
    def span(self, kind: SpanKind, name: str, data: Any = None) -> Iterator[dict[str, Any]]:
        """显式 loop 边界可用此上下文；result 字段用来记录最终输出。"""
        span_id = uuid4().hex
        parent = self._parent.get()
        fields = {
            "span_id": span_id,
            "parent_span_id": parent,
            "kind": kind,
            "name": redact_sensitive_text(name, self.secret_values),
        }
        self.emit(**fields, phase="start", status="running", data=data)
        token = self._parent.set(span_id)
        started = time.perf_counter()
        result: dict[str, Any] = {}
        status = "ok"
        try:
            yield result
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            # 异常消息可能携带任意私密输入，仅记录类型。
            result = {"error_type": type(exc).__name__}
            raise
        finally:
            self._parent.reset(token)
            self.emit(
                **fields,
                phase="end",
                status=status,
                duration_ms=(time.perf_counter() - started) * 1000,
                data=result,
            )

    def wrap(self, function: Callable[..., Any], *, kind: SpanKind, name: str | None = None):
        """包装实际执行的函数；签名绑定捕获位置参数，不消费流式输出。"""
        if not inspect.isroutine(function):
            raise TypeError("wrap a function or bound method, such as instance.__call__")
        if inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function):
            raise ValueError("streaming generators require a dedicated adapter")
        signature = inspect.signature(function)
        label = name or function.__qualname__

        def arguments(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
            try:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                return {key: value for key, value in bound.arguments.items() if key != "self"}
            except TypeError:
                return {"args": args, "kwargs": kwargs}

        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def async_call(*args: Any, **kwargs: Any) -> Any:
                with self.span(kind, label, arguments(args, kwargs)) as event:
                    result = await function(*args, **kwargs)
                    event["result"] = result
                    return result

            return async_call

        @wraps(function)
        def sync_call(*args: Any, **kwargs: Any) -> Any:
            with self.span(kind, label, arguments(args, kwargs)) as event:
                result = function(*args, **kwargs)
                event["result"] = result
                return result

        return sync_call
