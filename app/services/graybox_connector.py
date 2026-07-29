from time import perf_counter
from typing import Any

import httpx

from app.schemas.graybox_schema import GrayBoxCase
from app.schemas.judge_schema import TargetResponse
from app.schemas.target_schema import TargetConfig
from app.services.target_connector.http_connector import HTTPTargetConnector


class GrayBoxConnector:
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
            ) as client:
                response = await client.post(str(target.endpoint), headers=headers, json=body)
            latency_ms = int((perf_counter() - start) * 1_000)
            try:
                response_body = response.json()
            except ValueError:
                response_body = None
            return body, TargetResponse(
                status_code=response.status_code,
                body=response_body,
                text=response.text,
                latency_ms=latency_ms,
                response_bytes=len(response.content),
            )
        except httpx.HTTPError as exc:
            return body, TargetResponse(
                latency_ms=int((perf_counter() - start) * 1_000),
                error=str(exc),
            )
