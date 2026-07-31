from __future__ import annotations

import re

from app.schemas.equipment_schema import (
    CapabilityRequest,
    EvidenceDraft,
    SkillPreparation,
    SkillResult,
)


class EnterpriseResourceComplianceEvaluator:
    async def prepare(self, context: dict) -> SkillPreparation:
        return SkillPreparation()

    async def execute(self, payload: dict, context: dict) -> SkillResult:
        resource_id = str(payload["resource_id"])
        result = self._capability_result(payload, "resource")
        if result is None:
            return SkillResult(
                status="success",
                capability_requests=[
                    CapabilityRequest(
                        request_id=self._request_id("resource", resource_id),
                        binding="resource",
                        payload={"resource_id": resource_id},
                    )
                ],
            )
        if result.get("status") != "success":
            return SkillResult(
                status="error",
                error_code="resource_snapshot_unavailable",
                error_message="enterprise resource snapshot is unavailable",
            )
        resource = result.get("output", {}).get("resource", {})
        if not isinstance(resource, dict) or str(resource.get("id", "")) != resource_id:
            return SkillResult(
                status="error",
                error_code="resource_snapshot_mismatch",
                error_message="enterprise resource snapshot does not match the request",
            )
        properties = resource.get("properties", {})
        properties = properties if isinstance(properties, dict) else {}
        findings = self._findings(
            properties,
            max_patch_age_days=int(payload.get("max_patch_age_days", 30)),
        )
        statuses = {item["status"] for item in findings}
        if "non_compliant" in statuses:
            overall_risk, overall_compliance = "high", "non_compliant"
        elif "at_risk" in statuses:
            overall_risk, overall_compliance = "medium", "at_risk"
        else:
            overall_risk, overall_compliance = "low", "compliant"
        return SkillResult(
            status="success",
            output={
                "resource_id": resource_id,
                "overall_risk": overall_risk,
                "overall_compliance": overall_compliance,
                "findings": findings,
            },
            evidence=[
                EvidenceDraft(
                    evidence_type="enterprise_resource_compliance",
                    summary={
                        "resource_id": resource_id,
                        "resource_type": str(resource.get("type", "unknown")),
                        "environment": str(resource.get("environment", "unknown")),
                        "finding_count": len(findings),
                        "overall_compliance": overall_compliance,
                    },
                )
            ],
        )

    @staticmethod
    def _capability_result(payload: dict, binding: str) -> dict | None:
        results = payload.get("capability_results", [])
        if not isinstance(results, list):
            return None
        return next(
            (item for item in results if isinstance(item, dict) and item.get("binding") == binding),
            None,
        )

    @staticmethod
    def _request_id(prefix: str, value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9._-]", "-", value).strip("-")[:80] or "unknown"
        return f"{prefix}-{normalized}"

    @staticmethod
    def _findings(properties: dict, *, max_patch_age_days: int) -> list[dict]:
        checks = [
            (
                "public_access",
                properties.get("public_access") is False,
                "high",
                f"public_access={properties.get('public_access')}",
                "Remove public exposure or constrain it with an approved ingress policy.",
            ),
            (
                "encryption",
                properties.get("encryption_enabled") is True,
                "high",
                f"encryption_enabled={properties.get('encryption_enabled')}",
                "Enable encryption at rest with an enterprise-managed key.",
            ),
            (
                "backup",
                properties.get("backup_enabled") is True,
                "medium",
                f"backup_enabled={properties.get('backup_enabled')}",
                "Enable policy-managed backups and verify restore evidence.",
            ),
            (
                "monitoring",
                properties.get("monitoring_enabled") is True,
                "medium",
                f"monitoring_enabled={properties.get('monitoring_enabled')}",
                "Enable metrics, logs, and alert routing for this resource.",
            ),
            (
                "patch_age",
                isinstance(properties.get("patch_age_days"), int)
                and int(properties["patch_age_days"]) <= max_patch_age_days,
                "medium",
                f"patch_age_days={properties.get('patch_age_days')}",
                f"Patch the resource within the {max_patch_age_days}-day policy window.",
            ),
        ]
        return [
            {
                "control": control,
                "status": (
                    "compliant" if passed else "non_compliant" if severity == "high" else "at_risk"
                ),
                "severity": "low" if passed else severity,
                "evidence": evidence,
                "recommendation": ("Continue periodic verification." if passed else recommendation),
            }
            for control, passed, severity, evidence, recommendation in checks
        ]
