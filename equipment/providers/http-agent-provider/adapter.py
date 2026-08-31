"""内置 HTTP Agent Provider；仅访问 Provider Instance 显式批准的主机。"""

from __future__ import annotations

import asyncio

import httpx

from app.equipment.sdk import provider_secret
from app.equipment.security import (
    buffered_identity_response,
    read_bounded_response,
    send_pinned_request,
    validate_outbound_url,
)
from app.schemas.equipment_schema import EvidenceDraft, ProviderContext, ProviderResult

MAX_RESPONSE_BYTES = 1_048_576


class HttpAgentProvider:
    """在短期 Secret 作用域内调用目标，并返回有界、可脱敏的结构化 Evidence。"""

    async def describe(self) -> dict:
        return {"id": "http-agent-provider", "version": "1.0.1"}

    async def validate_config(self, config: dict) -> dict:
        return {"valid": bool(config.get("base_url"))}

    async def healthcheck(self, context: ProviderContext | dict) -> dict:
        context = ProviderContext.model_validate(context)
        base_url = str(context.config["base_url"])
        validate_outbound_url(base_url, context.approved_host_set)
        return {"healthy": True, "host": base_url.split("://", 1)[-1]}

    async def invoke(
        self,
        capability: str,
        payload: dict,
        context: ProviderContext | dict,
    ) -> ProviderResult:
        context = ProviderContext.model_validate(context)
        base_url = str(context.config["base_url"]).rstrip("/")
        path_key = "invoke_path" if capability == "agent.invoke.v1" else "trace_path"
        protocol_error = (
            "target_protocol_error" if capability == "agent.invoke.v1" else "trace_protocol_error"
        )
        timeout_error = (
            "target_timeout" if capability == "agent.invoke.v1" else "trace_protocol_error"
        )
        path = str(context.config.get(path_key, "/"))
        url = f"{base_url}/{path.lstrip('/')}"
        headers = {}
        if secret_name := context.config.get("auth_secret_name"):
            headers["authorization"] = f"Bearer {provider_secret(str(secret_name))}"
        try:
            addresses = validate_outbound_url(url, context.approved_host_set)
            async with httpx.AsyncClient(
                follow_redirects=False,
                trust_env=False,
            ) as client:
                request = client.build_request(
                    "POST",
                    url,
                    json=payload,
                    headers=headers,
                    timeout=context.budget.timeout_seconds,
                )
                async with asyncio.timeout(context.budget.timeout_seconds):
                    response = await send_pinned_request(
                        client,
                        request,
                        addresses=addresses,
                        stream=True,
                    )
                    content = await read_bounded_response(response, MAX_RESPONSE_BYTES)
            buffered = buffered_identity_response(response, content)
            buffered.raise_for_status()
            output = buffered.json()
            if not isinstance(output, dict):
                raise TypeError("HTTP Agent response must be a JSON object")
        except (TimeoutError, httpx.TimeoutException):
            return ProviderResult(
                status="timeout",
                error_code=timeout_error,
                error_message="HTTP Agent request timed out",
            )
        except (httpx.HTTPError, TypeError, ValueError):
            return ProviderResult(
                status="error",
                error_code=protocol_error,
                error_message="HTTP Agent response did not satisfy the protocol",
            )
        return ProviderResult(
            status="success",
            output=output,
            evidence=[
                EvidenceDraft(
                    evidence_type=(
                        "target_request" if capability == "agent.invoke.v1" else "tool_trace"
                    ),
                    summary={"host": url.split("/")[2]},
                ),
                EvidenceDraft(
                    evidence_type="target_response",
                    summary={"status_code": response.status_code, "host": url.split("/")[2]},
                ),
            ]
            if capability == "agent.invoke.v1"
            else [
                EvidenceDraft(
                    evidence_type="tool_trace",
                    summary={"status_code": response.status_code, "host": url.split("/")[2]},
                )
            ],
            external_operation_id=response.headers.get("x-operation-id"),
        )

    async def cleanup(self, resource: dict, context: ProviderContext | dict) -> dict:
        return {"cleaned": False, "reason": "HTTP agent Provider creates no leased resources"}
