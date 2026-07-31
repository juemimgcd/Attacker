from app.schemas.equipment_schema import EvidenceDraft, SkillPreparation, SkillResult


class EnterpriseChangeRiskEvaluator:
    async def prepare(self, context: dict) -> SkillPreparation:
        return SkillPreparation()

    async def execute(self, payload: dict, context: dict) -> SkillResult:
        change = payload["change"]
        missing = [
            field
            for field in (
                "owner",
                "approval_reference",
                "rollback_plan",
                "maintenance_window",
            )
            if not str(change.get(field, "")).strip()
        ]
        blast_radius = int(change.get("blast_radius", 0))
        if blast_radius > 10:
            missing.append("bounded_blast_radius")
        criticality = str(change["resource_criticality"])
        action = str(change["action"]).lower()
        destructive = action in {"delete", "destroy", "terminate", "wipe"}
        if destructive and criticality in {"high", "critical"}:
            risk = "critical"
        elif destructive or criticality == "critical" or blast_radius > 10:
            risk = "high"
        elif criticality == "high" or blast_radius > 1:
            risk = "medium"
        else:
            risk = "low"
        decision = "blocked" if missing else "allowed_for_approval"
        reasoning = [
            f"environment={change['environment']}",
            f"resource_criticality={criticality}",
            f"action={action}",
            f"blast_radius={blast_radius}",
            f"control_gaps={len(missing)}",
        ]
        return SkillResult(
            status="success",
            output={
                "change_id": str(change["id"]),
                "decision": decision,
                "risk": risk,
                "missing_controls": missing,
                "reasoning": reasoning,
            },
            evidence=[
                EvidenceDraft(
                    evidence_type="enterprise_change_risk",
                    summary={
                        "change_id": str(change["id"]),
                        "decision": decision,
                        "risk": risk,
                        "missing_control_count": len(missing),
                    },
                )
            ],
        )
