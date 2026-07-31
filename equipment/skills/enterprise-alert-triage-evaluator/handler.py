from __future__ import annotations

import re
from datetime import UTC, datetime

from app.schemas.equipment_schema import (
    CapabilityRequest,
    EvidenceDraft,
    SkillPreparation,
    SkillResult,
)


class EnterpriseAlertTriageEvaluator:
    async def prepare(self, context: dict) -> SkillPreparation:
        return SkillPreparation()

    async def execute(self, payload: dict, context: dict) -> SkillResult:
        alert_id = str(payload["alert_id"])
        result = self._capability_result(payload)
        if result is None:
            return SkillResult(
                status="success",
                capability_requests=[
                    CapabilityRequest(
                        request_id=self._request_id(alert_id),
                        binding="alert",
                        payload={"alert_id": alert_id},
                    )
                ],
            )
        if result.get("status") != "success":
            return SkillResult(
                status="error",
                error_code="alert_snapshot_unavailable",
                error_message="enterprise alert snapshot is unavailable",
            )
        alert = result.get("output", {}).get("alert", {})
        if not isinstance(alert, dict) or str(alert.get("id", "")) != alert_id:
            return SkillResult(
                status="error",
                error_code="alert_snapshot_mismatch",
                error_message="enterprise alert snapshot does not match the request",
            )
        pattern = self._pattern(alert)
        risk = self._risk(alert, pattern)
        action = {
            "noisy": "investigate_then_mute",
            "recovered": "verify_recovery",
            "muted": "investigate",
            "persistent": "investigate",
            "active": "investigate",
        }.get(pattern, "observe")
        reasoning = [
            f"status={str(alert.get('status', 'unknown')).lower()}",
            f"severity={str(alert.get('severity', 'unknown')).lower()}",
            f"trigger_count={int(alert.get('trigger_count') or 0)}",
            f"pattern={pattern}",
        ]
        return SkillResult(
            status="success",
            output={
                "alert_id": alert_id,
                "pattern": pattern,
                "risk": risk,
                "recommended_action": action,
                "reasoning": reasoning,
            },
            evidence=[
                EvidenceDraft(
                    evidence_type="enterprise_alert_triage",
                    summary={
                        "alert_id": alert_id,
                        "pattern": pattern,
                        "risk": risk,
                        "recommended_action": action,
                    },
                )
            ],
        )

    @staticmethod
    def _capability_result(payload: dict) -> dict | None:
        results = payload.get("capability_results", [])
        if not isinstance(results, list):
            return None
        return next(
            (item for item in results if isinstance(item, dict) and item.get("binding") == "alert"),
            None,
        )

    @staticmethod
    def _request_id(alert_id: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9._-]", "-", alert_id).strip("-")[:80] or "unknown"
        return f"alert-{normalized}"

    @classmethod
    def _pattern(cls, alert: dict) -> str:
        status = str(alert.get("status", "")).lower()
        trigger_count = int(alert.get("trigger_count") or 0)
        span_minutes = cls._span_minutes(
            str(alert.get("first_triggered_at", "")),
            str(alert.get("last_triggered_at", "")),
        )
        if status in {"resolved", "recovered", "closed"}:
            return "recovered"
        if status in {"muted", "silenced"}:
            return "muted"
        if status in {"firing", "active", "open"} and trigger_count >= 6 and span_minutes <= 120:
            return "noisy"
        if status in {"firing", "active", "open"} and trigger_count >= 3:
            return "persistent"
        if status in {"firing", "active", "open"}:
            return "active"
        return "unknown"

    @staticmethod
    def _risk(alert: dict, pattern: str) -> str:
        severity = str(alert.get("severity", "")).lower()
        if severity in {"critical", "fatal", "p0"} and pattern not in {"recovered"}:
            return "high"
        if severity in {"warning", "high", "p1"} or pattern in {"persistent", "noisy"}:
            return "medium"
        return "low"

    @staticmethod
    def _span_minutes(first: str, last: str) -> float:
        try:
            start = datetime.fromisoformat(first).astimezone(UTC)
            end = datetime.fromisoformat(last).astimezone(UTC)
        except ValueError:
            return float("inf")
        return max((end - start).total_seconds() / 60, 0)
