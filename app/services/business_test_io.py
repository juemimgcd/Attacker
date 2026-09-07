"""业务测试的配置解析、声明式 JSON 绑定和有界 HTTP 传输。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml

from app.equipment.security import (
    read_bounded_response,
    send_pinned_request,
    validate_target_url,
)
from app.schemas.business_test_schema import BusinessEndpoint, BusinessSuite

BINDINGS = {
    "run_id",
    "task_id",
    "scenario_id",
    "session_id",
    "query",
    "messages",
    "facts",
    "setup",
    "target",
}
TEMPLATE = re.compile(r"\$\{([^{}]+)\}")


class BusinessExecutionError(RuntimeError):
    """仅暴露稳定错误码，不把传输异常中的凭据或响应复制进日志。"""

    evidence: Any = None


def json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError("invalid JSON pointer")
    for raw in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw):
            raise ValueError("invalid JSON pointer escape")
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", key):
                raise KeyError(key)
            value = value[int(key)]
        elif isinstance(value, dict):
            value = value[key]
        else:
            raise KeyError(key)
    return value


def render(value: Any, bindings: dict[str, Any]) -> Any:
    """只替换配置中的占位符，不递归解释外部返回值或执行表达式。"""
    if isinstance(value, dict):
        return {key: render(nested, bindings) for key, nested in value.items()}
    if isinstance(value, list):
        return [render(nested, bindings) for nested in value]
    if not isinstance(value, str):
        return value

    def lookup(expression: str) -> Any:
        name, separator, pointer = expression.partition("#")
        if name not in bindings:
            raise ValueError(f"unknown binding {name}")
        return json_pointer(bindings[name], pointer) if separator else bindings[name]

    match = TEMPLATE.fullmatch(value)
    if match:
        return deepcopy(lookup(match.group(1)))

    def replace(match: re.Match[str]) -> str:
        item = lookup(match.group(1))
        if not isinstance(item, (str, int, float, bool)):
            raise TypeError("embedded binding must be a scalar")
        return str(item)

    return TEMPLATE.sub(replace, value)


def validate_templates(value: Any, allowed: set[str]) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            validate_templates(nested, allowed)
    elif isinstance(value, list):
        for nested in value:
            validate_templates(nested, allowed)
    elif isinstance(value, str):
        for match in TEMPLATE.finditer(value):
            name, separator, pointer = match.group(1).partition("#")
            if name not in allowed:
                raise ValueError(f"binding {name} unavailable in this configuration phase")
            if (
                separator
                and pointer
                and (not pointer.startswith("/") or re.search(r"~(?![01])", pointer))
            ):
                raise ValueError("invalid binding JSON pointer")
        if "${" in TEMPLATE.sub("", value):
            raise ValueError("malformed binding")


def endpoints(suite: BusinessSuite) -> list[BusinessEndpoint]:
    return [
        suite.target,
        *([suite.target.completion] if suite.target.completion else []),
        *[
            endpoint
            for scenario in suite.scenarios
            for endpoint in (scenario.setup, scenario.verification, scenario.cleanup)
            if endpoint is not None
        ],
    ]


def load_suite(path: Path) -> BusinessSuite:
    if path.stat().st_size > 1048576:
        raise ValueError("suite exceeds 1 MiB")
    suite = BusinessSuite.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    json.dumps(suite.model_dump(mode="json"), allow_nan=False)
    # Reuse the Core local Target policy. Public provider bindings are not silently bypassed.
    for endpoint in endpoints(suite):
        validate_target_url(str(endpoint.endpoint), allow_public_target=False, allowed_hosts=[])
        validate_templates(endpoint.body, BINDINGS)
        validate_templates(endpoint.path_suffix, BINDINGS)
        remainder = TEMPLATE.sub("segment", endpoint.path_suffix)
        if any(part in {".", ".."} for part in remainder.split("/")) or any(
            character in remainder for character in "?#%\\"
        ):
            raise ValueError("path_suffix must contain only literal paths and encoded bindings")
    for scenario in suite.scenarios:
        initial = BINDINGS - {"setup", "target", "query", "messages"}
        validate_templates(scenario.task, initial)
        validate_templates(scenario.user_facts, initial - {"facts"})
        if scenario.setup:
            validate_templates(scenario.setup.body, initial)
            validate_templates(scenario.setup.path_suffix, initial)
        for criterion in scenario.criteria:
            validate_templates(criterion.expected, BINDINGS)
    return suite


def credentials(suite: BusinessSuite) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for config in [*endpoints(suite), suite.simulator, suite.judge]:
        name = config.credential_env
        if name is not None:
            value = os.environ.get(name)
            if not value or "\r" in value or "\n" in value:
                raise ValueError(f"credential environment variable {name} is missing or invalid")
            resolved[name] = value
    return resolved


class BusinessTransport:
    def __init__(self, secrets: dict[str, str]) -> None:
        self.secrets = secrets

    async def call(
        self,
        endpoint: BusinessEndpoint,
        bindings: dict[str, Any],
        *,
        on_observation: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        body = render(endpoint.body, bindings)
        if len(json.dumps(body, ensure_ascii=False).encode()) > 1048576:
            raise BusinessExecutionError("request_too_large")

        def path_value(match: re.Match[str]) -> str:
            value = render(match.group(0), bindings)
            if not isinstance(value, (str, int)) or str(value) in {".", ".."}:
                raise ValueError("path bindings must be scalar path segments")
            return quote(str(value), safe="")

        suffix = TEMPLATE.sub(path_value, endpoint.path_suffix)
        url = str(endpoint.endpoint).rstrip("/") + suffix if suffix else str(endpoint.endpoint)
        headers = dict(endpoint.headers)
        if endpoint.credential_env:
            value = self.secrets[endpoint.credential_env]
            headers[endpoint.credential_header] = f"{endpoint.credential_prefix} {value}".strip()
        try:
            async with asyncio.timeout(endpoint.timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=endpoint.timeout_seconds, follow_redirects=False, trust_env=False
                ) as client:
                    request = client.build_request(
                        endpoint.method,
                        url,
                        headers=headers,
                        **({"json": body} if endpoint.method == "POST" else {}),
                    )
                    response = await send_pinned_request(
                        client, request, local_only=True, stream=True
                    )
                    try:
                        if not 200 <= response.status_code < 300:
                            raise BusinessExecutionError(f"http_{response.status_code}")
                        if endpoint.stream is not None:
                            return await read_business_stream(
                                response, endpoint.stream, on_observation=on_observation
                            )
                        content = await read_bounded_response(response, 1048576)
                    finally:
                        await response.aclose()
            return json.loads(content)
        except BusinessExecutionError:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise BusinessExecutionError("request_timeout") from exc
        except httpx.RequestError as exc:
            raise BusinessExecutionError("connection_error") from exc
        except (ValueError, UnicodeError) as exc:
            raise BusinessExecutionError("invalid_json_response") from exc


async def read_business_stream(
    response: httpx.Response,
    config: Any,
    *,
    on_observation: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """有界读取 SSE；只有声明的成功终止信号才能完成，EOF/错误事件不是成功。"""
    import codecs

    if "text/event-stream" not in response.headers.get("content-type", "").lower():
        raise BusinessExecutionError("expected_event_stream")
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    buffer = ""
    total = 0
    event_name = "message"
    data_lines: list[str] = []
    result: dict[str, Any] = {"text": "", "events": []}

    def dispatch() -> bool:
        nonlocal event_name, data_lines
        if not data_lines:
            event_name = "message"
            return False
        data = "\n".join(data_lines)
        name = event_name
        event_name, data_lines = "message", []
        if name in config.error_events:
            result["events"].append({"event": name, "data": data})
            if on_observation:
                on_observation(result)
            raise BusinessExecutionError("target_stream_error")
        if config.done_data and data == config.done_data:
            result["terminal"] = data
            return True
        payload = json.loads(data)
        result["events"].append({"event": name, "data": payload})
        if name == config.text_event:
            try:
                text = json_pointer(payload, config.text_pointer)
            except (KeyError, IndexError):
                text = None
            if text is not None:
                if not isinstance(text, str):
                    raise BusinessExecutionError("invalid_stream_text")
                result["text"] = result["text"] + text if config.text_mode == "append" else text
        if on_observation:
            on_observation(result)
        if name == config.terminal_event:
            terminal = json_pointer(payload, config.terminal_pointer)
            if terminal in config.failure_values:
                raise BusinessExecutionError("target_stream_failed")
            if terminal in config.success_values:
                result["terminal"] = terminal
                return True
        return False

    try:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > 1048576:
                raise BusinessExecutionError("stream_response_too_large")
            buffer += decoder.decode(chunk)
            while match := re.search(r"\r\n|\r(?!$)|\n", buffer):
                line, buffer = buffer[: match.start()], buffer[match.end() :]
                if not line:
                    if dispatch():
                        return result
                elif not line.startswith(":"):
                    field, separator, value = line.partition(":")
                    if separator and value.startswith(" "):
                        value = value[1:]
                    if field == "event":
                        event_name = value
                    elif field == "data":
                        data_lines.append(value)
        raise BusinessExecutionError("stream_ended_without_terminal")
    except BusinessExecutionError as exc:
        exc.evidence = result
        raise
    except (ValueError, UnicodeError, KeyError, IndexError) as exc:
        failure = BusinessExecutionError("invalid_stream_event")
        failure.evidence = result
        raise failure from exc
