"""有界调用外部 HTTP Target，并把传输失败转换为结构化 TargetResponse。"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from time import perf_counter
from typing import Any

import httpx

from app.equipment.security import (
    HTTPResponsePolicyError,
    HTTPResponseTooLargeError,
    buffered_identity_response,
    read_bounded_response,
    send_pinned_request,
)
from app.schemas.judge_schema import TargetErrorType, TargetResponse
from app.schemas.target_schema import TargetConfig

DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
_MAX_RESPONSE_BYTES = ContextVar(
    "http_target_max_response_bytes",
    default=DEFAULT_MAX_RESPONSE_BYTES,
)


@contextmanager
def target_response_limit(max_response_bytes: int) -> Iterator[None]:
    """Apply a per-run response limit without changing connector substitute signatures."""

    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")
    token = _MAX_RESPONSE_BYTES.set(max_response_bytes)
    try:
        yield
    finally:
        _MAX_RESPONSE_BYTES.reset(token)


class HTTPTargetConnector:
    """构造请求模板、注入测试 Prompt，并限制响应大小和重定向行为。"""

    @staticmethod
    def supports_message_history(target: TargetConfig) -> bool:
        """Only top-level messages are currently populated with conversation history."""

        return "messages" in target.request_template.body_template

    def build_request_body(
        self,
        target: TargetConfig,
        prompt: str,
        messages: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = self._replace_prompt(
            deepcopy(target.request_template.body_template),
            prompt,
        )
        if messages is not None and "messages" in body:
            body["messages"] = messages
        return body

    # 递归替换请求模板中的 prompt 占位符。
    def _replace_prompt(self, value: Any, prompt: str) -> Any:
        if isinstance(value, str):
            return value.replace("{prompt}", prompt)
        if isinstance(value, list):
            return [self._replace_prompt(item, prompt) for item in value]
        if isinstance(value, dict):
            return {key: self._replace_prompt(item, prompt) for key, item in value.items()}
        return value

    # 根据目标配置合并普通请求头和鉴权请求头。
    def build_headers(self, target: TargetConfig) -> dict[str, str]:
        headers = dict(target.headers)
        if target.auth.type == "bearer" and target.auth.token:
            headers[target.auth.header_name] = f"{target.auth.token_prefix} {target.auth.token}"
        return headers

    async def call(
        self,
        target: TargetConfig,
        prompt: str,
        messages: list[dict[str, str]] | None = None,
    ) -> tuple[dict[str, Any], TargetResponse]:
        """调用显式配置的 Target；不会从响应发现或跟随新的目标地址。"""

        request_body = self.build_request_body(target, prompt, messages)
        headers = self.build_headers(target)
        start = perf_counter()

        try:
            async with httpx.AsyncClient(
                timeout=target.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                request = client.build_request(
                    "POST", str(target.endpoint), headers=headers, json=request_body
                )
                async with asyncio.timeout(target.timeout_seconds):
                    response = await send_pinned_request(
                        client,
                        request,
                        local_only=not target.allow_public_target,
                        public_only=target.allow_public_target,
                        stream=True,
                    )
                    content = await read_bounded_response(response, _MAX_RESPONSE_BYTES.get())
                buffered = buffered_identity_response(response, content)
                latency_ms = int((perf_counter() - start) * 1000)
                try:
                    body = buffered.json()
                except ValueError:
                    body = None
                return request_body, TargetResponse(
                    status_code=response.status_code,
                    body=body,
                    text=buffered.text,
                    latency_ms=latency_ms,
                    response_bytes=len(content),
                )
        except HTTPResponseTooLargeError as exc:
            latency_ms = int((perf_counter() - start) * 1000)
            return request_body, TargetResponse(
                latency_ms=latency_ms,
                response_bytes=exc.limit + 1,
                error=str(exc),
                error_type=TargetErrorType.connection_error,
            )
        except HTTPResponsePolicyError as exc:
            latency_ms = int((perf_counter() - start) * 1000)
            return request_body, TargetResponse(
                latency_ms=latency_ms,
                error=str(exc),
                error_type=TargetErrorType.connection_error,
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            latency_ms = int((perf_counter() - start) * 1000)
            return request_body, TargetResponse(
                latency_ms=latency_ms,
                error=str(exc) or "target request timed out",
                error_type=TargetErrorType.timeout,
            )
        except httpx.RequestError as exc:
            latency_ms = int((perf_counter() - start) * 1000)
            return request_body, TargetResponse(
                latency_ms=latency_ms,
                error=str(exc),
                error_type=TargetErrorType.connection_error,
            )


http_target_connector = HTTPTargetConnector()
