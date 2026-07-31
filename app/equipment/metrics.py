"""线程安全的装备执行计数与耗时聚合，并同步到全局可观测指标。"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock

from app.observability import (
    record_equipment_counter,
    record_equipment_duration,
    record_equipment_invalid_packages,
)


class EquipmentMetrics:
    """保存进程内调试快照；Prometheus 指标由 observability 模块导出。"""

    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._durations_ms: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {"count": 0, "total": 0.0, "max": 0.0}
        )
        self._lock = Lock()

    def increment(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] += value
        record_equipment_counter(name, value)

    def observe(self, name: str, duration_ms: float) -> None:
        with self._lock:
            metric = self._durations_ms[name]
            metric["count"] = int(metric["count"]) + 1
            metric["total"] = float(metric["total"]) + duration_ms
            metric["max"] = max(float(metric["max"]), duration_ms)
        record_equipment_duration(name, duration_ms)

    @staticmethod
    def set_invalid_packages(value: int) -> None:
        record_equipment_invalid_packages(value)

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {
                "counters": dict(sorted(self._counters.items())),
                "durations_ms": {
                    name: {
                        "count": int(value["count"]),
                        "total": round(float(value["total"]), 3),
                        "max": round(float(value["max"]), 3),
                    }
                    for name, value in sorted(self._durations_ms.items())
                },
            }
