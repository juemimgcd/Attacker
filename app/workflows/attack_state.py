from typing import TypedDict


class AttackGraphState(TypedDict):
    run_id: str
    goal_id: str
    target_id: str
    thread_id: str
    checkpoint_ref: str | None
    allowed_case_ids: list[str]
    completed_case_ids: list[str]
    denied_action_ids: list[str]
    candidate_snapshot_id: str | None
    candidate_action_ids: list[str]
    coverage: dict[str, str]
    hypothesis_refs: list[str]
    observation_refs: list[str]
    finding_refs: list[str]
    evidence_gaps: list[str]
    action_repeat_counts: dict[str, int]
    recent_similarity_keys: list[str]
    information_gain_refs: list[str]
    expected_information_gain: str | None
    last_coverage_delta: int
    last_evidence_delta: int
    last_finding_delta: int
    last_target_transport_failed: bool
    test_principal_refs: list[str]
    finding_summaries: list[dict[str, str]]
    current_case_id: str | None
    current_operation_id: str | None
    current_step_started_at: str | None
    current_step_id: str | None
    evaluation_event_id: str | None
    policy_decision: str | None
    policy_reason: str | None
    policy_event_ids: list[str]
    approval_id: str | None
    approval_status: str | None
    planner_call_count: int
    provider_call_count: int
    planner_token_count: int
    planner_latency_ms: int
    planner_estimated_cost: float
    planner_failures: int
    planner_fallback_snapshot: dict[str, str] | None
    decision_history: list[str]
    target_call_count: int
    target_transport_failure_count: int
    graph_step_count: int
    last_state_fingerprint: str | None
    repeated_state_count: int
    consecutive_no_gain_steps: int
    next_action: str
    status: str
    terminal_reason: str | None
    stop_reason: str | None
    recovery_pending: bool
