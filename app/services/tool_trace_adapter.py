from typing import Any

from pydantic import ValidationError

from app.schemas.graybox_schema import ToolTraceEnvelope, TraceAdapterResult
from app.schemas.judge_schema import TargetResponse


class ToolTraceAdapter:
    def sanitize(
        self,
        value: Any,
        *,
        redacted_fields: set[str],
        secret_values: set[str],
    ) -> Any:
        return self._redact(value, redacted_fields, secret_values)

    def parse(
        self,
        response: TargetResponse,
        *,
        redacted_fields: set[str],
        secret_values: set[str],
    ) -> TraceAdapterResult:
        if not isinstance(response.body, dict):
            return TraceAdapterResult(
                errors=["target response body is not a JSON object"],
                evidence_complete=False,
            )
        raw_trace = response.body.get("trace")
        if not isinstance(raw_trace, dict):
            return TraceAdapterResult(
                errors=["target response does not contain a trace object"],
                evidence_complete=False,
            )

        redacted = self._redact(raw_trace, redacted_fields, secret_values)
        try:
            trace = ToolTraceEnvelope.model_validate(redacted)
        except ValidationError as exc:
            return TraceAdapterResult(
                errors=[f"invalid trace contract: {exc}"],
                evidence_complete=False,
            )

        tool_call_ids = {event.tool_call_id for event in trace.tool_events}
        policy_call_ids = {event.tool_call_id for event in trace.policy_events}
        errors: list[str] = []
        if not trace.tool_events:
            errors.append("trace contains no tool events")
        missing_policy = tool_call_ids - policy_call_ids
        if missing_policy:
            errors.append(
                "tool calls missing policy evidence: " + ", ".join(sorted(missing_policy))
            )
        return TraceAdapterResult(
            trace=trace,
            errors=errors,
            evidence_complete=not errors,
        )

    @classmethod
    def _redact(
        cls,
        value: Any,
        redacted_fields: set[str],
        secret_values: set[str],
    ) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    "[REDACTED]"
                    if cls._is_redacted_key(key, redacted_fields)
                    else cls._redact(item, redacted_fields, secret_values)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._redact(item, redacted_fields, secret_values) for item in value]
        if isinstance(value, str):
            for secret in secret_values:
                value = value.replace(secret, "[REDACTED]")
        return value

    @staticmethod
    def _is_redacted_key(key: str, redacted_fields: set[str]) -> bool:
        normalized = key.lower().replace("-", "_")
        return any(
            normalized == field or normalized.endswith(f"_{field}") for field in redacted_fields
        )
