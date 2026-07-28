from copy import deepcopy
from time import perf_counter
from typing import Any

import httpx

from app.schemas.judge_schema import TargetResponse
from app.schemas.target_schema import TargetConfig


# 负责构造请求并异步调用目标 Agent 的 HTTP 接口。
class HTTPTargetConnector:
    # 根据目标请求模板和攻击 prompt 构造实际请求体。
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
            return {k: self._replace_prompt(v, prompt) for k, v in value.items()}
        return value

    # 根据目标配置合并普通请求头和鉴权请求头。
    def build_headers(self, target: TargetConfig):
        headers = dict(target.headers)
        if target.auth.type == "bearer" and target.auth.token:
            headers[target.auth.header_name] = f"{target.auth.token_prefix} {target.auth.token}"
        return headers

    # 异步调用目标 Agent 并封装请求体、响应内容、耗时和错误。
    async def call(
        self,
        target: TargetConfig,
        prompt: str,
        messages: list[dict[str, str]] | None = None,
    ):
        request_body = self.build_request_body(target, prompt, messages)
        headers = self.build_headers(target)
        start = perf_counter()

        try:
            async with httpx.AsyncClient(timeout=target.timeout_seconds) as client:
                response = await client.post(
                    str(target.endpoint), headers=headers, json=request_body
                )
                latency_ms = int((perf_counter() - start) * 1000)
                try:
                    body = response.json()
                except ValueError:
                    body = None
                return request_body, TargetResponse(
                    status_code=response.status_code,
                    body=body,
                    text=response.text,
                    latency_ms=latency_ms,
                    response_bytes=len(response.content),
                )
        except httpx.HTTPError as exc:
            latency_ms = int((perf_counter() - start) * 1000)
            return request_body, TargetResponse(latency_ms=latency_ms, error=str(exc))


http_target_connector = HTTPTargetConnector()
