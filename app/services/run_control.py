from app.schemas.attack_state_schema import (
    AttackState,
    CoverageStatus,
    PlannerFallbackMode,
    PlannerFallbackSnapshot,
    RunStatus,
    StopReason,
)
from app.schemas.run_control_schema import (
    FallbackAction,
    FallbackDecision,
    FallbackEvent,
    PlannerUnavailableContext,
    StepProgress,
    StopAction,
    StopContext,
    StopDecision,
    StopLimits,
)


# 集中执行 Planner 无权覆盖的硬停止、软停止和降级决策。
class RunControlService:
    # 只根据实际持久化收益更新无增益、重复状态和连续失败计数。
    def record_step_progress(
        self,
        state: AttackState,
        progress: StepProgress,
    ) -> AttackState:
        has_information_gain = any(
            (
                progress.coverage_delta > 0,
                progress.evidence_delta > 0,
                progress.finding_delta > 0,
            )
        )
        repeated_state_count = (
            state.repeated_state_count + 1
            if state.last_state_fingerprint == progress.state_fingerprint
            else 1
        )
        return AttackState.model_validate(
            {
                **state.model_dump(),
                "last_state_fingerprint": progress.state_fingerprint,
                "consecutive_no_gain_steps": (
                    0 if has_information_gain else state.consecutive_no_gain_steps + 1
                ),
                "repeated_state_count": repeated_state_count,
                "planner_failure_count": (
                    state.planner_failure_count + 1 if progress.planner_failed else 0
                ),
                "target_transport_failure_count": (
                    state.target_transport_failure_count + 1
                    if progress.target_transport_failed
                    else 0
                ),
            }
        )

    # 按固定优先级评估停止条件；硬停止始终先于等待审批和软停止。
    def evaluate_stop(
        self,
        state: AttackState,
        context: StopContext,
        limits: StopLimits,
    ) -> StopDecision:
        if context.cancelled:
            return self._stop(StopReason.cancelled, "run_cancelled")
        if context.target_authorization_revoked:
            return self._stop(
                StopReason.policy_terminated,
                "target_authorization_revoked",
            )
        if context.policy_termination_requested:
            return self._stop(
                StopReason.policy_terminated,
                "policy_termination_requested",
            )
        if self._budget_exhausted(state):
            return self._stop(StopReason.budget_exhausted, "budget_exhausted")

        if context.has_pending_approval:
            return StopDecision(
                action=StopAction.wait_for_approval,
                reason_code="pending_approval",
                checkpoint_ref=context.checkpoint_ref,
            )

        if context.required_coverage_ids and all(
            state.coverage.get(coverage_id) == CoverageStatus.covered
            for coverage_id in context.required_coverage_ids
        ):
            return self._stop(StopReason.completed, "required_coverage_completed")
        if state.consecutive_no_gain_steps >= limits.max_consecutive_no_gain_steps:
            return self._stop(
                StopReason.no_information_gain,
                "consecutive_no_information_gain",
            )
        if state.repeated_state_count >= limits.max_repeated_state_count:
            return self._stop(StopReason.loop_detected, "repeated_state")
        if not context.has_executable_candidates:
            return self._stop(
                StopReason.no_information_gain,
                "no_executable_candidates",
            )
        if state.planner_failure_count >= limits.max_planner_failures:
            return self._stop(StopReason.planner_failed, "planner_failure_limit")
        if state.target_transport_failure_count >= limits.max_target_transport_failures:
            return self._stop(
                StopReason.target_unavailable,
                "target_transport_failure_limit",
            )
        return StopDecision(
            action=StopAction.continue_run,
            reason_code="conditions_not_met",
        )

    # 把停止决策应用为新的 Run 快照，不原地改写调用方持有的状态。
    def apply_stop_decision(
        self,
        state: AttackState,
        decision: StopDecision,
    ) -> AttackState:
        if decision.action == StopAction.continue_run:
            return self._update_state(
                state,
                status=RunStatus.running,
                stop_reason=None,
            )
        if decision.action == StopAction.wait_for_approval:
            return self._update_state(
                state,
                status=RunStatus.waiting_approval,
                stop_reason=None,
                checkpoint_ref=decision.checkpoint_ref,
            )

        status = self._status_for_stop_reason(decision.stop_reason)
        return self._update_state(
            state,
            status=status,
            stop_reason=decision.stop_reason,
        )

    # 严格根据 Run 快照中的降级模式决定终止、暂停或确定性候选路由。
    def handle_planner_unavailable(
        self,
        state: AttackState,
        context: PlannerUnavailableContext,
    ) -> tuple[AttackState, FallbackDecision]:
        fallback_snapshot = PlannerFallbackSnapshot(
            mode=state.planner_fallback_mode,
            reason=context.reason,
            actual_model_id=context.actual_model_id,
            actual_provider_id=context.actual_provider_id,
        )
        event = FallbackEvent(
            mode=state.planner_fallback_mode,
            reason=context.reason,
            actual_model_id=context.actual_model_id,
            actual_provider_id=context.actual_provider_id,
            candidate_snapshot_id=state.candidate_snapshot_id,
        )

        if state.planner_fallback_mode == PlannerFallbackMode.fail_closed:
            decision = FallbackDecision(
                action=FallbackAction.terminate,
                stop_reason=StopReason.planner_failed,
                event=event,
            )
            return (
                self._update_state(
                    state,
                    status=RunStatus.failed,
                    stop_reason=StopReason.planner_failed,
                    planner_fallback_snapshot=fallback_snapshot,
                ),
                decision,
            )

        if state.planner_fallback_mode == PlannerFallbackMode.pause:
            if context.checkpoint_ref is None:
                raise ValueError("pause fallback requires checkpoint_ref")
            decision = FallbackDecision(
                action=FallbackAction.pause,
                event=event,
            )
            return (
                self._update_state(
                    state,
                    status=RunStatus.paused,
                    stop_reason=None,
                    checkpoint_ref=context.checkpoint_ref,
                    planner_fallback_snapshot=fallback_snapshot,
                ),
                decision,
            )

        if self._budget_exhausted(state):
            decision = FallbackDecision(
                action=FallbackAction.terminate,
                stop_reason=StopReason.budget_exhausted,
                event=event,
            )
            return (
                self._update_state(
                    state,
                    status=RunStatus.stopped,
                    stop_reason=StopReason.budget_exhausted,
                    planner_fallback_snapshot=fallback_snapshot,
                ),
                decision,
            )

        candidate_id = next(
            (
                candidate_id
                for candidate_id in state.candidate_action_ids
                if candidate_id not in state.completed_action_ids
                and candidate_id not in state.denied_action_ids
            ),
            None,
        )
        if state.candidate_snapshot_id is None or candidate_id is None:
            decision = FallbackDecision(
                action=FallbackAction.terminate,
                stop_reason=StopReason.no_information_gain,
                event=event,
            )
            return (
                self._update_state(
                    state,
                    status=RunStatus.stopped,
                    stop_reason=StopReason.no_information_gain,
                    planner_fallback_snapshot=fallback_snapshot,
                ),
                decision,
            )

        event = event.model_copy(update={"candidate_id": candidate_id})
        decision = FallbackDecision(
            action=FallbackAction.route_to_policy_gate,
            candidate_snapshot_id=state.candidate_snapshot_id,
            candidate_id=candidate_id,
            next_route="policy_gate",
            event=event,
        )
        return (
            self._update_state(
                state,
                status=RunStatus.running,
                stop_reason=None,
                planner_fallback_snapshot=fallback_snapshot,
            ),
            decision,
        )

    # 判断任何一步、目标、Provider、时长或成本预算是否已经达到上限。
    def _budget_exhausted(self, state: AttackState) -> bool:
        budget = state.budget
        return (
            budget.steps_used >= budget.max_steps
            or budget.target_calls_used >= budget.max_target_calls
            or budget.provider_calls_used >= budget.max_provider_calls
            or budget.elapsed_seconds >= budget.max_duration_seconds
            or (budget.max_cost is not None and budget.cost_used >= budget.max_cost)
        )

    def _stop(self, reason: StopReason, reason_code: str) -> StopDecision:
        return StopDecision(
            action=StopAction.stop,
            stop_reason=reason,
            reason_code=reason_code,
        )

    def _status_for_stop_reason(
        self,
        reason: StopReason | None,
    ) -> RunStatus:
        if reason == StopReason.completed:
            return RunStatus.completed
        if reason == StopReason.cancelled:
            return RunStatus.cancelled
        if reason == StopReason.planner_failed:
            return RunStatus.failed
        return RunStatus.stopped

    def _update_state(
        self,
        state: AttackState,
        **updates: object,
    ) -> AttackState:
        return AttackState.model_validate(
            {
                **state.model_dump(),
                **updates,
            }
        )


run_control_service = RunControlService()
