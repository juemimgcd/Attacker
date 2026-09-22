"""OpenAI-compatible 模型传输层；负责有限重试、用量和结构化错误。"""

import json
from decimal import Decimal
from time import perf_counter
from typing import Any, Literal, Protocol

import httpx
from pydantic import HttpUrl, SecretStr

from app.schemas.model_provider_schema import (
    ModelInferenceRequest,
    ModelInferenceResult,
    ModelProviderUsage,
    ProviderAttempt,
)
from app.schemas.prompt_schema import ModelToolCall


def _aggregate_usage(attempts: tuple[ProviderAttempt, ...]) -> ModelProviderUsage:
    return ModelProviderUsage(
        physical_attempts=len(attempts),
        input_tokens=sum(attempt.input_tokens for attempt in attempts),
        output_tokens=sum(attempt.output_tokens for attempt in attempts),
        latency_ms=sum(attempt.latency_ms for attempt in attempts),
        estimated_cost=sum((attempt.estimated_cost for attempt in attempts), Decimal(0)),
        attempts=attempts,
    )


class ModelProviderError(RuntimeError):
    def __init__(
        self,
        *,
        error_category: str,
        attempts: tuple[ProviderAttempt, ...],
    ) -> None:
        super().__init__(f"model provider failed: {error_category}")
        self.error_category = error_category
        self.attempts = attempts
        self.usage = _aggregate_usage(attempts)

    @property
    def physical_attempts(self) -> int:
        return len(self.attempts)

    @property
    def latency_ms(self) -> int:
        return sum(attempt.latency_ms for attempt in self.attempts)


class ModelProvider(Protocol):
    """Planner 与 Model Judge 共享的最小推理传输契约。"""

    async def infer(self, request: ModelInferenceRequest) -> ModelInferenceResult: ...

    async def healthcheck(self, timeout_seconds: float = 5) -> bool: ...


class OpenAICompatibleModelProvider:
    """执行物理模型请求；重试次数和累计耗时写入返回结果。"""

    def __init__(
        self,
        *,
        endpoint: HttpUrl | str,
        api_key: SecretStr | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.endpoint = str(endpoint)
        self.api_key = api_key
        self.transport = transport

    async def infer(self, request: ModelInferenceRequest) -> ModelInferenceResult:
        attempts: list[ProviderAttempt] = []
        headers = {"Content-Type": "application/json"}
        if self.api_key is not None:
            headers["Authorization"] = f"Bearer {self.api_key.get_secret_value()}"
        payload: dict[str, Any] = {
            "model": request.model_id,
            "temperature": request.temperature,
            "messages": [
                message.model_dump(mode="json", exclude_none=True) for message in request.messages
            ],
        }
        if request.tools:
            payload.update(
                tools=list(request.tools), tool_choice="required", parallel_tool_calls=False
            )
        else:
            payload["response_format"] = {"type": "json_object"}
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens

        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            for attempt_number in range(1, request.max_physical_attempts + 1):
                started = perf_counter()
                attempt = ProviderAttempt(attempt=attempt_number, status="success", latency_ms=0)
                try:
                    response = await client.post(
                        self.endpoint,
                        headers=headers,
                        json=payload,
                    )
                    try:
                        body = response.json()
                    except ValueError:
                        response.raise_for_status()
                        raise
                    if not isinstance(body, dict):
                        response.raise_for_status()
                        raise TypeError("model response must be a JSON object")
                    raw_usage = body.get("usage", {})
                    if not isinstance(raw_usage, dict):
                        raise TypeError("model response usage must be a JSON object")
                    # 收到的用量先校验并保留；HTTP 错误、截断及内容解析失败也可能已计费。
                    attempt = ProviderAttempt(
                        attempt=attempt_number,
                        status="success",
                        latency_ms=0,
                        input_tokens=raw_usage.get("prompt_tokens", 0),
                        output_tokens=raw_usage.get("completion_tokens", 0),
                        estimated_cost=raw_usage.get("estimated_cost", 0),
                    )
                    response.raise_for_status()
                    choice = body["choices"][0]
                    if choice.get("finish_reason") in {"length", "content_filter"}:
                        raise ValueError("model response did not complete")
                    message = choice["message"]
                    tool_calls = tuple(
                        ModelToolCall.model_validate(call)
                        for call in (message.get("tool_calls") or ())
                    )
                    structured_output: dict[str, Any] = {}
                    if tool_calls:
                        if not request.tools:
                            raise ValueError("unexpected model tool calls")
                    else:
                        content = message.get("content")
                        structured_output = (
                            json.loads(content) if isinstance(content, str) else content
                        )
                        if not isinstance(structured_output, dict):
                            raise TypeError("model response content must be a JSON object")
                except Exception as exc:
                    category, status, retryable = self._classify_error(exc)
                    attempts.append(
                        attempt.model_copy(
                            update={
                                "status": status,
                                "latency_ms": self._elapsed_ms(started),
                                "error_category": category,
                            }
                        )
                    )
                    if not retryable or attempt_number == request.max_physical_attempts:
                        raise ModelProviderError(
                            error_category=category,
                            attempts=tuple(attempts),
                        ) from exc
                else:
                    attempts.append(
                        attempt.model_copy(update={"latency_ms": self._elapsed_ms(started)})
                    )
                    return ModelInferenceResult(
                        structured_output=structured_output,
                        tool_calls=tool_calls,
                        usage=_aggregate_usage(tuple(attempts)),
                    )

        raise RuntimeError("unreachable model provider state")

    async def healthcheck(self, timeout_seconds: float = 5) -> bool:
        headers: dict[str, str] = {}
        if self.api_key is not None:
            headers["Authorization"] = f"Bearer {self.api_key.get_secret_value()}"
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = await client.get(self.endpoint, headers=headers)
            return response.status_code < 500
        except httpx.HTTPError:
            return False

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(round((perf_counter() - started) * 1_000), 0)

    @staticmethod
    def _classify_error(
        exc: Exception,
    ) -> tuple[str, Literal["error", "timeout"], bool]:
        if isinstance(exc, httpx.TimeoutException):
            return "timeout", "timeout", True
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            return (
                f"http_{status_code}",
                "error",
                status_code == 429 or status_code >= 500,
            )
        if isinstance(exc, httpx.RequestError):
            return "request_error", "error", True
        if isinstance(exc, (json.JSONDecodeError, KeyError, TypeError, ValueError)):
            return "invalid_response", "error", False
        return "provider_error", "error", False
