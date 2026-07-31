from __future__ import annotations

from urllib.parse import quote

import httpx

from app.equipment.sdk import provider_secret
from app.equipment.security import validate_outbound_url
from app.schemas.equipment_schema import EvidenceDraft, ProviderContext, ProviderResult

MAX_RESPONSE_BYTES = 524_288


class EnterpriseResponseTooLargeError(ValueError):
    pass


class EnterpriseOpsProvider:
    async def describe(self) -> dict:
        return {
            "id": "enterprise-ops-provider",
            "version": "1.0.0",
            "capability_layers": ["datasource", "controlled_change"],
        }

    async def validate_config(self, config: dict) -> dict:
        errors: list[str] = []
        if not str(config.get("base_url", "")).startswith("https://"):
            errors.append("base_url must use HTTPS")
        if not str(config.get("auth_secret_name", "")).strip():
            errors.append("auth_secret_name is required")
        for key in ("resource_path", "alert_path", "change_path", "health_path"):
            value = str(config.get(key, self._default_path(key)))
            if not value.startswith("/") or "://" in value:
                errors.append(f"{key} must be a relative absolute-path")
        return {"valid": not errors, "errors": errors}

    async def healthcheck(self, context: ProviderContext | dict) -> dict:
        context = ProviderContext.model_validate(context)
        response = await self._request(
            method="GET",
            url=self._url(context, "health_path", {}),
            context=context,
        )
        return {"healthy": response.status_code == 200, "status_code": response.status_code}

    async def invoke(
        self,
        capability: str,
        payload: dict,
        context: ProviderContext | dict,
    ) -> ProviderResult:
        context = ProviderContext.model_validate(context)
        try:
            if capability == "enterprise.resource.read.v1":
                resource_id = str(payload["resource_id"])
                response = await self._request(
                    method="GET",
                    url=self._url(
                        context,
                        "resource_path",
                        {"resource_id": quote(resource_id, safe="")},
                    ),
                    context=context,
                )
                if response.status_code == 404:
                    return ProviderResult(status="error", error_code="resource_not_found")
                response.raise_for_status()
                resource = self._normalize_resource(response.json(), resource_id)
                return ProviderResult(
                    status="success",
                    output={"resource": resource},
                    evidence=[
                        EvidenceDraft(
                            evidence_type="enterprise_resource_snapshot",
                            summary={
                                "resource_id": resource["id"],
                                "resource_type": resource["type"],
                                "environment": resource["environment"],
                            },
                        )
                    ],
                )
            if capability == "enterprise.alert.read.v1":
                alert_id = str(payload["alert_id"])
                response = await self._request(
                    method="GET",
                    url=self._url(
                        context,
                        "alert_path",
                        {"alert_id": quote(alert_id, safe="")},
                    ),
                    context=context,
                )
                if response.status_code == 404:
                    return ProviderResult(status="error", error_code="alert_not_found")
                response.raise_for_status()
                alert = self._normalize_alert(response.json(), alert_id)
                return ProviderResult(
                    status="success",
                    output={"alert": alert},
                    evidence=[
                        EvidenceDraft(
                            evidence_type="enterprise_alert_snapshot",
                            summary={
                                "alert_id": alert["id"],
                                "status": alert["status"],
                                "severity": alert["severity"],
                            },
                        )
                    ],
                )
            if capability == "enterprise.change.execute.v1":
                change_id = str(payload["change_id"])
                response = await self._request(
                    method="POST",
                    url=self._url(
                        context,
                        "change_path",
                        {"change_id": quote(change_id, safe="")},
                    ),
                    context=context,
                    json_payload=payload,
                    idempotency_key=context.operation_id,
                )
                if response.status_code == 404:
                    return ProviderResult(status="error", error_code="change_not_found")
                if response.status_code in {409, 422}:
                    return ProviderResult(status="denied", error_code="change_rejected")
                response.raise_for_status()
                operation_id = response.headers.get("x-operation-id") or context.operation_id
                body = response.json()
                status = (
                    str(body.get("status", "submitted")) if isinstance(body, dict) else "submitted"
                )
                return ProviderResult(
                    status="success",
                    output={
                        "change": {
                            "id": change_id,
                            "status": status,
                            "operation_id": operation_id,
                        }
                    },
                    evidence=[
                        EvidenceDraft(
                            evidence_type="enterprise_change_result",
                            summary={
                                "change_id": change_id,
                                "status": status,
                                "approval_reference": payload["approval_reference"],
                            },
                        )
                    ],
                    external_operation_id=operation_id,
                )
            return ProviderResult(
                status="denied",
                error_code="enterprise_protocol_error",
                error_message="unsupported enterprise capability",
            )
        except EnterpriseResponseTooLargeError:
            return ProviderResult(
                status="error",
                error_code="enterprise_response_too_large",
                error_message="enterprise operations response exceeded the configured limit",
            )
        except (httpx.HTTPError, TypeError, ValueError):
            return ProviderResult(
                status="error",
                error_code="enterprise_upstream_error",
                error_message="enterprise operations request failed",
            )

    async def cleanup(self, resource: dict, context: ProviderContext | dict) -> dict:
        return {"cleaned": False, "reason": "enterprise operations create no leased resources"}

    async def _request(
        self,
        *,
        method: str,
        url: str,
        context: ProviderContext,
        json_payload: dict | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        validate_outbound_url(url, context.approved_host_set)
        secret_name = str(context.config["auth_secret_name"])
        headers = {"authorization": f"Bearer {provider_secret(secret_name)}"}
        if idempotency_key is not None:
            headers["idempotency-key"] = idempotency_key
        async with (
            httpx.AsyncClient(follow_redirects=False) as client,
            client.stream(
                method,
                url,
                headers=headers,
                json=json_payload,
                timeout=context.budget.timeout_seconds,
            ) as response,
        ):
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise EnterpriseResponseTooLargeError
            return httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                content=bytes(body),
                request=response.request,
            )

    def _url(self, context: ProviderContext, key: str, values: dict[str, str]) -> str:
        base_url = str(context.config["base_url"]).rstrip("/")
        template = str(context.config.get(key, self._default_path(key)))
        path = template.format_map(values)
        if not path.startswith("/") or "://" in path:
            raise ValueError("enterprise Provider path is unsafe")
        url = f"{base_url}{path}"
        validate_outbound_url(url, context.approved_host_set)
        return url

    @staticmethod
    def _default_path(key: str) -> str:
        return {
            "resource_path": "/api/resources/{resource_id}",
            "alert_path": "/api/alerts/{alert_id}",
            "change_path": "/api/changes/{change_id}/execute",
            "health_path": "/health",
        }[key]

    @staticmethod
    def _normalize_resource(body: object, resource_id: str) -> dict:
        source = body.get("data", body) if isinstance(body, dict) else {}
        if not isinstance(source, dict):
            raise TypeError("resource response must be an object")
        properties = source.get("properties", {})
        properties = properties if isinstance(properties, dict) else {}
        normalized_properties: dict[str, bool | int] = {
            key: value
            for key in (
                "public_access",
                "encryption_enabled",
                "backup_enabled",
                "monitoring_enabled",
            )
            if isinstance((value := properties.get(key)), bool)
        }
        patch_age = properties.get("patch_age_days")
        if isinstance(patch_age, int) and not isinstance(patch_age, bool) and patch_age >= 0:
            normalized_properties["patch_age_days"] = patch_age
        return {
            "id": str(source.get("id") or resource_id),
            "name": str(source.get("name") or source.get("displayName") or resource_id),
            "type": str(source.get("type") or source.get("resourceType") or "unknown"),
            "environment": str(source.get("environment") or "unknown"),
            "properties": normalized_properties,
        }

    @staticmethod
    def _normalize_alert(body: object, alert_id: str) -> dict:
        source = body.get("data", body) if isinstance(body, dict) else {}
        if not isinstance(source, dict):
            raise TypeError("alert response must be an object")
        resource = source.get("resource", {})
        resource = resource if isinstance(resource, dict) else {}
        return {
            "id": str(source.get("id") or alert_id),
            "status": str(source.get("status") or "unknown").lower(),
            "severity": str(source.get("severity") or source.get("level") or "unknown").lower(),
            "trigger_count": max(
                int(source.get("trigger_count") or source.get("triggerCount") or 0), 0
            ),
            "first_triggered_at": str(
                source.get("first_triggered_at") or source.get("triggerAt") or ""
            ),
            "last_triggered_at": str(
                source.get("last_triggered_at") or source.get("lastTriggerAt") or ""
            ),
            "resource": {
                "id": str(resource.get("id") or source.get("resourceId") or ""),
                "name": str(resource.get("name") or source.get("resourceName") or ""),
            },
        }
