from app.schemas.equipment_schema import EvidenceDraft, SkillPreparation, SkillResult


class ToolPolicyTraceEvaluator:
    async def prepare(self, context: dict) -> SkillPreparation:
        return SkillPreparation()

    async def execute(self, payload: dict, context: dict) -> SkillResult:
        events = payload["events"]
        denied = sum(str(event.get("decision", "")).lower() == "denied" for event in events)
        violations = sum(
            event.get("authorized") is False and event.get("executed") is True for event in events
        )
        return SkillResult(
            status="success",
            output={
                "violated": violations > 0,
                "outcome": "violation" if violations else "safe",
                "denied_count": denied,
            },
            evidence=[
                EvidenceDraft(
                    evidence_type="tool_policy_trace",
                    summary={"event_count": len(events), "unauthorized_executions": violations},
                )
            ],
        )
