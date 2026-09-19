"""Author one Benchmark package: own the target lifecycle and return portable results."""

import os
import re
from typing import Protocol

from app.schemas.equipment_benchmark_schema import (
    BenchmarkCleanup,
    BenchmarkContext,
    BenchmarkEvaluation,
    BenchmarkObservation,
    BenchmarkPreparation,
    BenchmarkTask,
    MetricSample,
)


def benchmark_secret(name: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
        raise ValueError("invalid benchmark secret name")
    value = os.environ.get(f"ATTACKER_BENCHMARK_SECRET_{name.upper()}")
    if value is None:
        raise LookupError(f"benchmark secret {name} is not available")
    return value


class Benchmark(Protocol):
    async def prepare(self, task: dict, context: dict) -> BenchmarkPreparation: ...

    async def execute(self, task: dict, state: dict, context: dict) -> BenchmarkObservation: ...

    async def evaluate(
        self, task: dict, observation: dict, context: dict
    ) -> BenchmarkEvaluation: ...

    async def cleanup(self, state: dict, context: dict) -> BenchmarkCleanup: ...


__all__ = [
    "Benchmark",
    "BenchmarkCleanup",
    "BenchmarkContext",
    "BenchmarkEvaluation",
    "BenchmarkObservation",
    "BenchmarkPreparation",
    "BenchmarkTask",
    "MetricSample",
    "benchmark_secret",
]
