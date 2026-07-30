from typing import TypedDict


class AttackGraphState(TypedDict):
    run_id: str
    target_id: str
    thread_id: str
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
    last_state_fingerprint: str | None
    consecutive_no_gain_steps: int
    repeated_state_count: int
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
    planner_token_count: int
    planner_failures: int
    decision_history: list[str]
    target_call_count: int
    graph_step_count: int
    next_action: str
    status: str
    terminal_reason: str | None
    recovery_pending: bool
