from app.schemas.adaptive_agent_schema import ObservationSource, UntrustedObservation
from app.schemas.graybox_schema import TraceAdapterResult
from app.schemas.judge_schema import TargetResponse


class ObservationNormalizer:
    def __init__(self, max_summary_length: int = 2_000) -> None:
        self.max_summary_length = max_summary_length

    def normalize_target(
        self,
        *,
        observation_ref: str,
        response: TargetResponse,
        trace: TraceAdapterResult,
    ) -> UntrustedObservation:
        parts = [
            f"status_code={response.status_code}",
            f"error={response.error or 'none'}",
            f"response={response.text}",
            f"tool_events={len(trace.trace.tool_events)}",
            f"policy_events={len(trace.trace.policy_events)}",
            f"trace_complete={trace.evidence_complete}",
        ]
        return UntrustedObservation(
            observation_ref=observation_ref,
            source=ObservationSource.target,
            summary="; ".join(parts)[: self.max_summary_length],
        )
