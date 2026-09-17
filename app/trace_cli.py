"""模型协议代理为主入口；Python 函数包装为可选深度采集。"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from app.schemas.execution_trace_schema import CaptureSummary, ExecutionEvent, SpanKind
from app.tracing import ExecutionRecorder


def add_parser(commands: Any) -> None:
    command = commands.add_parser("trace", help="Observe agents through a model protocol proxy")
    actions = command.add_subparsers(dest="trace_command", required=True)
    proxy = actions.add_parser("proxy", help="Run a local Chat Completions tracing proxy")
    proxy.add_argument("--upstream-base-url", required=True, help="Provider base URL including /v1")
    proxy.add_argument("--output-dir", type=Path, default=Path("data/traces/proxy"))
    proxy.add_argument("--host", choices=["127.0.0.1", "::1"], default="127.0.0.1")
    proxy.add_argument("--port", type=int, default=8787)
    proxy.add_argument("--timeout", type=float, default=120)
    proxy.add_argument("--max-capture-bytes", type=int, default=1_048_576)
    proxy.add_argument("--redact-field", action="append", default=[])
    run = actions.add_parser("run", help="Optional: wrap known Python functions")
    run.add_argument("entry", help="Importable module:callable (keyword arguments only)")
    run.add_argument("--input", type=Path, required=True, help="JSON object of entry arguments")
    run.add_argument("--output", type=Path, required=True, help="New JSONL file")
    run.add_argument("--model", action="append", default=[], help="module:attribute to wrap")
    run.add_argument("--tool", action="append", default=[], help="module:attribute to wrap")
    run.add_argument("--loop", action="append", default=[], help="Explicit loop-step callable")
    run.add_argument("--redact-field", action="append", default=[])
    show = actions.add_parser("show")
    show.add_argument("path", type=Path)


def _resolve(binding: str) -> tuple[Any, str, Any]:
    module_name, separator, attribute = binding.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("binding must be module:attribute")
    owner: Any = importlib.import_module(module_name)
    parts = attribute.split(".")
    for part in parts[:-1]:
        owner = getattr(owner, part)
    name = parts[-1]
    raw = inspect.getattr_static(owner, name)
    if isinstance(raw, (classmethod, staticmethod)):
        raise TypeError("bind a module function or ordinary instance method, not a descriptor")
    function = getattr(owner, name)
    if not callable(function):
        raise TypeError(f"{binding} is not callable")
    return owner, name, function


def show_trace(path: Path) -> dict[str, Any]:
    """保留父子 ID；未闭合 Span 显式报告，不据此推断目标是否执行成功。"""
    if path.is_dir():
        reports = [show_trace(file) for file in sorted(path.glob("*.jsonl"))]
        sessions: dict[str, list[dict[str, Any]]] = {}
        for report in reports:
            for span in report["spans"]:
                if span["source"] != "model_proxy":
                    continue
                data = span["input"] if isinstance(span["input"], dict) else {}
                session_id = str(data.get("session_id", span["trace_id"]))
                sessions.setdefault(session_id, []).append(span)
        return {
            "coverage": "model_protocol_only",
            "ordering": "observation_time_not_execution_dependency",
            "sessions": {
                key: sorted(spans, key=lambda span: span["timestamp"])
                for key, spans in sessions.items()
            },
            "captures": reports,
        }
    events: list[ExecutionEvent] = []
    errors: list[str] = []
    summary: CaptureSummary | None = None
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
                if isinstance(record, dict) and record.get("record_type") == "capture_summary":
                    summary = CaptureSummary.model_validate(record)
                else:
                    events.append(ExecutionEvent.model_validate(record))
            except ValueError:
                errors.append(f"invalid event at line {line_number}")
    starts = {event.span_id: event for event in events if event.phase == "start"}
    ends = {event.span_id: event for event in events if event.phase == "end"}
    if summary is not None:
        if summary.events != len(events):
            errors.append("capture summary event count does not match saved events")
        if any(event.trace_id != summary.trace_id for event in events):
            errors.append("capture contains events from a different trace")
    if len(starts) + len(ends) != len(events):
        errors.append("capture contains duplicate span events")
    return {
        "coverage": (
            "model_protocol_only"
            if any(event.source == "model_proxy" for event in events)
            else "instrumented_boundaries_only"
        ),
        "errors": errors,
        "capture_summary": summary.model_dump() if summary else None,
        "capture_finished": summary is not None,
        "unfinished_span_ids": sorted(starts.keys() - ends.keys()),
        "orphan_end_span_ids": sorted(ends.keys() - starts.keys()),
        "spans": [
            {
                "trace_id": start.trace_id,
                "span_id": span_id,
                "parent_span_id": start.parent_span_id,
                "kind": start.kind,
                "source": start.source,
                "timestamp": start.timestamp.isoformat(),
                "name": start.name,
                "input": start.data,
                "output": ends[span_id].data if span_id in ends else None,
                "status": ends[span_id].status if span_id in ends else "unknown",
                "duration_ms": ends[span_id].duration_ms if span_id in ends else None,
                "truncated": start.truncated or (span_id in ends and ends[span_id].truncated),
            }
            for span_id, start in starts.items()
        ],
    }


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.trace_command == "proxy":
        import uvicorn

        from app.tracing.model_proxy import create_proxy_app

        app = create_proxy_app(
            upstream_base_url=args.upstream_base_url,
            output_dir=args.output_dir,
            timeout=args.timeout,
            max_capture_bytes=args.max_capture_bytes,
            redacted_fields=set(args.redact_field),
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=args.host,
                port=args.port,
                access_log=False,
            )
        )
        await server.serve()
        return {"status": "stopped", "output_dir": str(args.output_dir)}
    if args.trace_command == "show":
        return show_trace(args.path)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("--input must contain a JSON object of keyword arguments")
    # 导入目标属于显式执行操作；所有绑定预检在执行入口之前完成。
    _, _, entry = _resolve(args.entry)
    bindings: list[tuple[Any, str, Any, SpanKind, str]] = []
    seen: set[tuple[int, str]] = set()
    kinds: tuple[SpanKind, ...] = ("model", "tool", "loop")
    for kind in kinds:
        for binding in getattr(args, kind):
            owner, name, function = _resolve(binding)
            key = (id(owner), name)
            if key in seen or function is entry:
                raise ValueError("duplicate binding or entry bound as a nested span")
            seen.add(key)
            bindings.append((owner, name, function, kind, binding))
    recorder = ExecutionRecorder(output=args.output, redacted_fields=set(args.redact_field))
    wrapped_entry = recorder.wrap(entry, kind="agent", name=args.entry)
    wrapped_bindings = [
        (owner, name, original, recorder.wrap(original, kind=kind, name=binding))
        for owner, name, original, kind, binding in bindings
    ]
    try:
        with ExitStack() as stack:
            for owner, name, original, wrapped in wrapped_bindings:
                owned = name in vars(owner)
                setattr(owner, name, wrapped)
                if owned:
                    stack.callback(setattr, owner, name, original)
                else:
                    stack.callback(delattr, owner, name)
            result = wrapped_entry(**payload)
            if inspect.isawaitable(result):
                await result
    finally:
        recorder.finish()
    snapshot = recorder.snapshot()
    return {
        "trace_id": snapshot.trace_id,
        "output": str(args.output),
        "events": len(snapshot.events),
        "dropped_events": snapshot.dropped_events,
        "capture_errors": snapshot.capture_errors,
        "coverage": snapshot.coverage,
        "observed_kinds": sorted({event.kind for event in snapshot.events}),
    }
