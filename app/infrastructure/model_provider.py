import json
from time import perf_counter
from typing import Literal, Protocol

import httpx
from pydantic import HttpUrl, SecretStr

from app.schemas.model_provider_schema import (
    ModelInferenceRequest,
    ModelInferenceResult,
    ModelProviderUsage,
    ProviderAttempt,
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

    @property
    def physical_attempts(self) -> int:
        return len(self.attempts)

    @property
    def latency_ms(self) -> int:
        return sum(attempt.latency_ms for attempt in self.attempts)


class ModelProvider(Protocol):
    async def infer(self, request: ModelInferenceRequest) -> ModelInferenceResult: ...

    async def healthcheck(self, timeout_seconds: float = 5) -> bool: ...


class OpenAICompatibleModelProvider:
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
        payload = {
            "model": request.model_id,
            "temperature": request.temperature,
            "response_format": {"type": "json_object"},
            "messages": [message.model_dump(mode="json") for message in request.messages],
        }

        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            for attempt_number in range(1, request.max_physical_attempts + 1):
                started = perf_counter()
                try:
                    response = await client.post(
                        self.endpoint,
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    body = response.json()
                    content = body["choices"][0]["message"]["content"]
                    structured_output = json.loads(content) if isinstance(content, str) else content
                    if not isinstance(structured_output, dict):
                        raise TypeError("model response content must be a JSON object")
                    attempts.append(
                        ProviderAttempt(
                            attempt=attempt_number,
                            status="success",
                            latency_ms=self._elapsed_ms(started),
                        )
                    )
                    raw_usage = body.get("usage", {})
                    usage = ModelProviderUsage(
                        physical_attempts=len(attempts),
                        input_tokens=int(raw_usage.get("prompt_tokens", 0)),
                        output_tokens=int(raw_usage.get("completion_tokens", 0)),
                        latency_ms=sum(item.latency_ms for item in attempts),
                        attempts=tuple(attempts),
                    )
                    return ModelInferenceResult(
                        structured_output=structured_output,
                        usage=usage,
                    )
                except Exception as exc:
                    category, status, retryable = self._classify_error(exc)
                    attempts.append(
                        ProviderAttempt(
                            attempt=attempt_number,
                            status=status,
                            latency_ms=self._elapsed_ms(started),
                            error_category=category,
                        )
                    )
                    if not retryable or attempt_number == request.max_physical_attempts:
                        raise ModelProviderError(
                            error_category=category,
                            attempts=tuple(attempts),
                        ) from exc

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
