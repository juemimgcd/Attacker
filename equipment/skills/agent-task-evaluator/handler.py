"""Example domain-neutral evaluator; benchmark-specific alternatives live in Equipment."""

from typing import Any

from app.schemas.equipment_schema import CapabilityRequest, SkillPreparation, SkillResult

MISSING = object()


def read_pointer(document: Any, pointer: str) -> Any:
    current = document
    for component in pointer.split("/")[1:]:
        key = component.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current.get(key, MISSING)
        elif isinstance(current, list) and key.isdecimal() and int(key) < len(current):
            current = current[int(key)]
        else:
            return MISSING
    return current


def observed_metrics(output: dict, duration_ms: float | None, expected: dict) -> dict:
    metrics = {}
    if duration_ms is not None:
        metrics["duration_ms"] = {"value": duration_ms}
    telemetry = output.get("metadata", {}).get("benchmark", {})
    if not isinstance(telemetry, dict):
        raise TypeError("metadata.benchmark must be an object")
    for name in ("input_tokens", "output_tokens", "loop_count"):
        value = telemetry.get(name)
        if value is None:
            continue
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        metrics[name] = {"value": value}
    if "input_tokens" in metrics and "output_tokens" in metrics:
        metrics["total_tokens"] = {
            "value": metrics["input_tokens"]["value"] + metrics["output_tokens"]["value"]
        }
    calls = telemetry.get("tool_calls")
    if calls is not None:
        if not isinstance(calls, list) or any(
            not isinstance(call, dict)
            or not isinstance(call.get("name"), str)
            or not isinstance(call.get("arguments"), dict)
            for call in calls
        ):
            raise ValueError("tool_calls must contain tool names and argument objects")
        allowed = expected.get("tool_calls")
        if allowed is not None and calls:
            metrics["tool_call_accuracy"] = {
                "numerator": sum(
                    any(
                        call["name"] == rule["name"] and call["arguments"] == rule["arguments"]
                        for rule in allowed
                    )
                    for call in calls
                ),
                "denominator": len(calls),
            }
    return metrics


class AgentTaskEvaluator:
    async def prepare(self, context: dict) -> SkillPreparation:
        return SkillPreparation()

    async def execute(self, payload: dict, context: dict) -> SkillResult:
        task = payload["task"]
        expected = task["expected"]
        results = {item["request_id"]: item for item in payload.get("capability_results", [])}
        if "invoke" not in results:
            for check in expected.get("checks", []):
                if check["op"] != "exists" and "value" not in check:
                    raise ValueError("equals and contains checks require a value")
            if not expected.get("checks"):
                return SkillResult(
                    status="success",
                    output={
                        "benchmark": {
                            "outcome": "invalid",
                            "reason": "Task has no completion checks",
                            "evidence": {"task_id": task["id"], "missing": "expected.checks"},
                            "metrics": {},
                        }
                    },
                )
            target_payload = dict(task["payload"])
            target_payload["metadata"] = {
                **target_payload.get("metadata", {}),
                "benchmark_task_id": task["id"],
                "benchmark_session_id": context["operation_id"],
            }
            return SkillResult(
                status="success",
                capability_requests=[
                    CapabilityRequest(request_id="invoke", binding="target", payload=target_payload)
                ],
            )
        result = results["invoke"]
        if result["status"] != "success":
            return SkillResult(status=result["status"], error_code=result.get("error_code"))
        output = result["output"]
        checks = []
        for check in expected["checks"]:
            value = read_pointer(output, check["pointer"])
            if check["op"] == "exists":
                passed: bool | None = value is not MISSING
            elif value is MISSING:
                passed = None
            elif check["op"] == "equals":
                passed = value == check["value"] and type(value) is type(check["value"])
            else:
                needle = check["value"]
                if isinstance(value, str):
                    passed = isinstance(needle, str) and needle in value
                elif isinstance(value, list):
                    passed = needle in value
                elif isinstance(value, dict):
                    passed = isinstance(needle, str) and needle in value
                else:
                    passed = False
            checks.append({"pointer": check["pointer"], "passed": passed})
        passed_values = [check["passed"] for check in checks]
        outcome = (
            "failed"
            if False in passed_values
            else "inconclusive"
            if None in passed_values
            else "passed"
        )
        metrics = observed_metrics(output, result.get("duration_ms"), expected)
        return SkillResult(
            status="success",
            output={
                "benchmark": {
                    "outcome": outcome,
                    "reason": "Evaluated configured completion checks against the target response",
                    "evidence": {
                        "target_operation_id": result["operation_id"],
                        "checks": checks,
                    },
                    "metrics": {
                        name: sample
                        for name, sample in metrics.items()
                        if name in payload["metric_names"]
                    },
                }
            },
        )
