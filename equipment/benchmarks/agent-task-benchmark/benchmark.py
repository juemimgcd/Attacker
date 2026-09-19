"""Self-contained HTTP Agent benchmark: target invocation, evidence and grading."""

from time import perf_counter
from typing import Any

import httpx

from app.equipment.benchmark_sdk import (
    BenchmarkCleanup,
    BenchmarkEvaluation,
    BenchmarkObservation,
    BenchmarkPreparation,
    benchmark_secret,
)
from app.equipment.security import (
    buffered_identity_response,
    read_bounded_response,
    send_pinned_request,
    validate_outbound_url,
    validate_url_has_no_secrets,
)

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


class HttpAgentBenchmark:
    async def prepare(self, task: dict, context: dict) -> BenchmarkPreparation:
        expected = task["expected"]
        for check in expected.get("checks", []):
            if check["op"] != "exists" and "value" not in check:
                raise ValueError("equals and contains checks require a value")
        if not expected.get("checks"):
            return BenchmarkPreparation(ready=False, reason="Task has no completion checks")
        endpoint = context["target_config"]["endpoint"]
        validate_url_has_no_secrets(endpoint, label="benchmark target")
        return BenchmarkPreparation(state={"session_id": context["operation_id"]})

    async def execute(self, task: dict, state: dict, context: dict) -> BenchmarkObservation:
        endpoint = context["target_config"]["endpoint"]
        addresses = validate_outbound_url(endpoint, context["allowed_hosts"])
        headers = {}
        if "api_key" in context["secret_names"]:
            headers["authorization"] = f"Bearer {benchmark_secret('api_key')}"
        payload = dict(task["payload"])
        payload["metadata"] = {
            **payload.get("metadata", {}),
            "benchmark_task_id": task["id"],
            "benchmark_session_id": state["session_id"],
        }
        started = perf_counter()
        try:
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
                request = client.build_request(
                    "POST",
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=context["timeout_seconds"],
                )
                response = await send_pinned_request(
                    client, request, addresses=addresses, stream=True
                )
                content = await read_bounded_response(response, 1048576)
        except httpx.TimeoutException:
            return BenchmarkObservation(status="timeout", reason="Target HTTP request timed out")
        response = buffered_identity_response(response, content)
        response.raise_for_status()
        output = response.json()
        if not isinstance(output, dict) or not isinstance(output.get("response"), str):
            raise TypeError("target must return a JSON object with a response string")
        samples = observed_metrics(output, (perf_counter() - started) * 1000, {})
        return BenchmarkObservation(
            status="success",
            output=output,
            evidence={"operation_id": context["operation_id"], "http_status": response.status_code},
            metrics={
                name: sample for name, sample in samples.items() if name in context["metric_names"]
            },
        )

    async def evaluate(self, task: dict, observation: dict, context: dict) -> BenchmarkEvaluation:
        output = observation["output"]
        checks = []
        for check in task["expected"]["checks"]:
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
        outcomes = [check["passed"] for check in checks]
        outcome = (
            "failed" if False in outcomes else "inconclusive" if None in outcomes else "passed"
        )
        metrics = {**observation["metrics"], **observed_metrics(output, None, task["expected"])}
        return BenchmarkEvaluation(
            outcome=outcome,
            reason="Evaluated configured completion checks against the target response",
            evidence={"operation_id": context["operation_id"], "checks": checks},
            metrics={
                name: sample for name, sample in metrics.items() if name in context["metric_names"]
            },
        )

    async def cleanup(self, state: dict, context: dict) -> BenchmarkCleanup:
        return BenchmarkCleanup(cleaned=True, reason="HTTP benchmark creates no managed resources")
