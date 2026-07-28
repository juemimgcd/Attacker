from typing import TypedDict


class AttackGraphState(TypedDict):
    run_id: str
    target_id: str
    thread_id: str
    allowed_case_ids: list[str]
    completed_case_ids: list[str]
    finding_summaries: list[dict[str, str]]
    current_case_id: str | None
    current_operation_id: str | None
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
