"""为没有 Trace 的 Python Agent 提供显式、可移除的运行时采集。"""

from app.tracing.recorder import ExecutionRecorder

__all__ = ["ExecutionRecorder"]
