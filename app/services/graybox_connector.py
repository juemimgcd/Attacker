"""灰盒 Target 连接器；在普通 HTTP 响应上保留调用耗时与幂等操作标识。"""

import asyncio
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
from app.schemas.graybox_schema import GrayBoxCase
from app.schemas.judge_schema import TargetErrorType, TargetResponse
from app.schemas.target_schema import TargetConfig
from app.services.target_connector.http_connector import (
    DEFAULT_MAX_RESPONSE_BYTES,
    HTTPTargetConnector,
)


class GrayBoxConnector:
    """调用符合灰盒契约的 Agent，Trace 解析由独立适配器负责。"""

    def __init__(self) -> None:
        self.http_connector = HTTPTargetConnector()

    async def execute(
        self,
        *,
        target: TargetConfig,
        case: GrayBoxCase,
        operation_id: str,
        approval_id: str | None,
    ) -> tuple[dict[str, Any], TargetResponse]:
        messages = [{"role": "user", "content": prompt} for prompt in case.prompts]
        body = self.http_connector.build_request_body(
            target,
            case.prompts[-1],
            messages,
        )
        body["attacker_context"] = {
            "case_id": case.id,
            "scenario": case.target_scenario,
            "operation_id": operation_id,
            "approval_id": approval_id,
        }
        headers = self.http_connector.build_headers(target)
        headers["Idempotency-Key"] = operation_id
        start = perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=target.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                request = client.build_request(
                    "POST", str(target.endpoint), headers=headers, json=body
                )
                async with asyncio.timeout(target.timeout_seconds):
                    response = await send_pinned_request(
                        client,
                        request,
                        local_only=not target.allow_public_target,
                        public_only=target.allow_public_target,
                        stream=True,
                    )
                    content = await read_bounded_response(response, DEFAULT_MAX_RESPONSE_BYTES)
            latency_ms = int((perf_counter() - start) * 1_000)
            buffered = buffered_identity_response(response, content)
            try:
                response_body = buffered.json()
            except ValueError:
                response_body = None
            return body, TargetResponse(
                status_code=response.status_code,
                body=response_body,
                text=buffered.text,
                latency_ms=latency_ms,
                response_bytes=len(content),
            )
        except HTTPResponseTooLargeError as exc:
            return body, TargetResponse(
                latency_ms=int((perf_counter() - start) * 1_000),
                response_bytes=exc.limit + 1,
                error=str(exc),
            )
        except HTTPResponsePolicyError as exc:
            return body, TargetResponse(
                latency_ms=int((perf_counter() - start) * 1_000),
                error=str(exc),
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            return body, TargetResponse(
                latency_ms=int((perf_counter() - start) * 1_000),
                error=str(exc) or "target request timed out",
                error_type=TargetErrorType.timeout,
            )
        except httpx.HTTPError as exc:
            return body, TargetResponse(
                latency_ms=int((perf_counter() - start) * 1_000),
                error=str(exc),
            )
