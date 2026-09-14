"""Aggregate declared metric samples without knowing datasets, targets or grading rules."""

from collections import Counter
from math import ceil
from statistics import fmean
from typing import Any

from app.schemas.equipment_benchmark_schema import MetricDefinition, MetricSample


def validate_metrics(samples: dict[str, MetricSample], definitions: list[MetricDefinition]) -> None:
    declared = {item.name: item for item in definitions}
    unknown = samples.keys() - declared.keys()
    if unknown:
        raise ValueError(f"undeclared metrics: {sorted(unknown)}")
    for name, sample in samples.items():
        ratio = declared[name].aggregation == "ratio"
        if ratio != (sample.value is None):
            raise ValueError(f"metric {name} does not match its declared aggregation")


def benchmark_summary(rows: dict[str, Any]) -> dict[str, Any]:
    source = next(
        (
            event["evidence"]
            for event in rows["events"]
            if event["event_type"] == "equipment_benchmark_frozen"
        ),
        {},
    )
    definitions = [MetricDefinition.model_validate(item) for item in source.get("metrics", [])]
    results = [step["result"] for step in rows["steps"] if step["status"] == "completed"]
    eligible = [result for result in results if result["outcome"] != "invalid"]
    counts = Counter(result["outcome"] for result in results)
    metrics: dict[str, Any] = {}
    for definition in definitions:
        population = (
            [result for result in eligible if result["outcome"] == "passed"]
            if definition.scope == "passed"
            else eligible
        )
        samples = [
            MetricSample.model_validate(result["metrics"][definition.name])
            for result in population
            if definition.name in result.get("metrics", {})
        ]
        entry: dict[str, Any] = {
            **definition.model_dump(mode="json"),
            "value": None,
            "observed": len(samples),
            "eligible": len(population),
            "coverage": len(samples) / len(population) if population else None,
        }
        if definition.aggregation == "ratio":
            numerator = sum(sample.numerator or 0 for sample in samples)
            denominator = sum(sample.denominator or 0 for sample in samples)
            entry.update(
                numerator=numerator,
                denominator=denominator,
                value=numerator / denominator if denominator else None,
            )
        elif samples:
            values = sorted(sample.value for sample in samples if sample.value is not None)
            entry.update(
                value=sum(values) if definition.aggregation == "sum" else fmean(values),
                total=sum(values),
                mean=fmean(values),
                p50=values[ceil(0.50 * len(values)) - 1],
                p95=values[ceil(0.95 * len(values)) - 1],
            )
        metrics[definition.name] = entry
    return {
        "source": source,
        "planned": rows["run"]["total_cases"],
        "attempted": len(rows["steps"]),
        "completed": len(results),
        "pending": rows["run"]["total_cases"] - len(results),
        "outcomes": dict(counts),
        "cleanup_failures": sum(
            item.get("status") == "cleanup_failed"
            for result in results
            for item in result.get("cleanup", [])
        ),
        "task_completion_rate": {
            "value": counts["passed"] / len(eligible) if eligible else None,
            "numerator": counts["passed"],
            "denominator": len(eligible),
            "excluded_invalid": counts["invalid"],
        },
        "metrics": metrics,
    }


def benchmark_markdown(report: dict[str, Any]) -> str:
    summary = report["benchmark_summary"]
    completion = summary["task_completion_rate"]
    lines = [
        f"# Equipment benchmark {report['run']['id']}",
        "",
        f"Status: {report['run']['status']}",
        f"Completed attempts: {summary['completed']}/{summary['planned']}",
        f"Task completion: {completion['numerator']}/{completion['denominator']}",
        f"Invalid attempts excluded: {completion['excluded_invalid']}",
        "",
        "| Metric | Value | Unit | Coverage |",
        "| --- | ---: | --- | ---: |",
    ]
    for name, metric in summary["metrics"].items():
        value = "unavailable" if metric["value"] is None else f"{metric['value']:.4g}"
        lines.append(
            f"| {name} | {value} | {metric['unit']} | {metric['observed']}/{metric['eligible']} |"
        )
    lines.extend(["", "Percentiles use nearest rank; missing observations are never zero."])
    return "\n".join(lines) + "\n"
