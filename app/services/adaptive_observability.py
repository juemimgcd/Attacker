from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, ClassVar


class AdaptiveObservabilityService:
    _gain_rank: ClassVar[dict[str, int]] = {
        "low": 1,
        "medium": 2,
        "high": 3,
    }

    def build(self, rows: dict[str, Any]) -> dict[str, Any]:
        events = rows["events"]
        started = self._first_event(events, "run_started")
        planner_events = [
            event
            for event in events
            if event["event_type"]
            in {
                "planner_decided",
                "planner_rejected",
                "planner_fallback",
                "planner_error",
            }
        ]
        planner_usage = [event["evidence"].get("usage", {}) for event in planner_events]
        snapshots = [
            event["evidence"]["snapshot"]
            for event in events
            if event["event_type"] == "candidate_snapshot_created"
        ]
        filter_reasons = Counter(
            rejection["reason"]
            for snapshot in snapshots
            for rejection in snapshot.get("rejected", [])
        )
        gains = [
            event["evidence"]
            for event in events
            if event["event_type"] == "information_gain_measured"
        ]
        predicted_actual = [self._gain_comparison(gain) for gain in gains]
        coverage_latest = {
            event["evidence"]["tag"]: event["evidence"]["status"]
            for event in events
            if event["event_type"] == "coverage_updated"
        }
        if not coverage_latest:
            for step in rows["steps"]:
                case = step.get("result", {}).get("case", {})
                evaluation = step.get("result", {}).get("evaluation", {})
                status = "covered" if evaluation.get("evidence_complete") else "inconclusive"
                for tag in case.get("coverage_tags") or [case.get("category")]:
                    if tag:
                        coverage_latest[str(tag)] = status
        evaluations = [
            event["evidence"].get("evaluation", {})
            for event in events
            if event["event_type"] == "evaluation_completed"
        ]
        control_events = [
            event["evidence"] for event in events if event["event_type"] == "run_control_evaluated"
        ]
        terminal = next(
            (
                event["evidence"]
                for event in reversed(events)
                if event["event_type"]
                in {
                    "run_completed",
                    "run_failed",
                    "run_aborted",
                    "run_cancelled",
                    "run_paused",
                }
            ),
            {},
        )
        scope = self._scope_metrics(rows, started)
        evidence_paths = {
            finding["id"]: len(finding["evidence_event_ids"]) for finding in rows["findings"]
        }
        complete_loop_count, executed_step_count = self._complete_loop_count(rows)
        derived_frozen = [event for event in events if event["event_type"] == "derived_case_frozen"]
        derived_verified = [
            event for event in events if event["event_type"] == "derived_case_verified"
        ]
        return {
            "planner": {
                "decision_count": sum(
                    event["event_type"] == "planner_decided" for event in planner_events
                ),
                "rejection_count": sum(
                    event["event_type"] == "planner_rejected" for event in planner_events
                ),
                "fallback_count": sum(
                    event["event_type"] == "planner_fallback" for event in planner_events
                ),
                "error_count": sum(
                    event["event_type"] == "planner_error" for event in planner_events
                ),
                "physical_attempts": sum(
                    int(usage.get("physical_attempts", 0)) for usage in planner_usage
                ),
                "input_tokens": sum(int(usage.get("input_tokens", 0)) for usage in planner_usage),
                "output_tokens": sum(int(usage.get("output_tokens", 0)) for usage in planner_usage),
                "latency_ms": sum(int(usage.get("latency_ms", 0)) for usage in planner_usage),
                "estimated_cost": sum(
                    float(usage.get("estimated_cost", 0)) for usage in planner_usage
                ),
                "error_categories": dict(
                    Counter(
                        error
                        for event in planner_events
                        for error in self._planner_error_categories(event)
                    )
                ),
            },
            "model_judge": self._model_judge_metrics(events),
            "candidates": {
                "generated_total": sum(
                    len(snapshot.get("candidates", [])) for snapshot in snapshots
                ),
                "latest_count": (len(snapshots[-1].get("candidates", [])) if snapshots else 0),
                "filtered_total": sum(filter_reasons.values()),
                "filter_reasons": dict(sorted(filter_reasons.items())),
                "snapshot_generated_count": len(snapshots),
                "snapshot_expired_count": sum(
                    event["event_type"] == "candidate_snapshot_expired" for event in events
                ),
                "snapshot_rejected_count": sum(
                    event["event_type"] == "planner_rejected"
                    and "snapshot" in str(event["evidence"].get("rejection_reason", ""))
                    for event in planner_events
                ),
            },
            "scope": scope,
            "coverage": {
                "by_status": dict(Counter(coverage_latest.values())),
                "by_risk": self._risk_coverage(rows["steps"]),
                "covered_tags": sorted(
                    tag for tag, status in coverage_latest.items() if status == "covered"
                ),
            },
            "information_gain": {
                "step_count": len(gains),
                "coverage_delta": sum(int(gain.get("coverage_delta", 0)) for gain in gains),
                "evidence_delta": sum(
                    int(gain.get("evidence_completeness_delta", 0)) for gain in gains
                ),
                "confirmed_finding_delta": sum(
                    int(gain.get("confirmed_finding_delta", 0)) for gain in gains
                ),
                "no_gain_step_count": sum(
                    not any(
                        int(gain.get(field, 0))
                        for field in (
                            "coverage_delta",
                            "evidence_completeness_delta",
                            "confirmed_finding_delta",
                        )
                    )
                    for gain in gains
                ),
                "predicted_actual": predicted_actual,
                "prediction_mismatch_count": sum(
                    item["predicted"] != item["actual"]
                    for item in predicted_actual
                    if item["predicted"] is not None
                ),
            },
            "derived_cases": {
                "frozen_count": len(derived_frozen),
                "deterministic_verified_count": len(derived_verified),
                "effective_discovery_count": sum(
                    bool(
                        event["evidence"].get("derived_case", {}).get("deterministic_evidence_refs")
                    )
                    for event in derived_verified
                ),
            },
            "loops": {
                "repeated_action_count": self._repeated_action_count(planner_events),
                "max_repeated_state_count": max(
                    (int(event.get("repeated_state_count", 0)) for event in control_events),
                    default=0,
                ),
                "max_consecutive_no_gain_steps": max(
                    (int(event.get("consecutive_no_gain_steps", 0)) for event in control_events),
                    default=0,
                ),
            },
            "evaluator": {
                "agreement_count": sum(
                    self._evaluator_outcome(evaluation) == "agreement" for evaluation in evaluations
                ),
                "conflict_count": sum(
                    self._evaluator_outcome(evaluation) == "conflict" for evaluation in evaluations
                ),
                "inconclusive_count": sum(
                    evaluation.get("outcome") == "inconclusive" for evaluation in evaluations
                ),
            },
            "run": {
                "stop_reason": terminal.get("stop_reason"),
                "checkpoint_resume_count": sum(
                    event["event_type"] in {"recovery_policy_revalidated", "planner_resumed"}
                    for event in events
                ),
                "target_calls": rows["run"].get("target_call_count", 0),
                "duration_seconds": self._duration_seconds(rows["run"]),
                "complete_loop_count": complete_loop_count,
                "executed_step_count": executed_step_count,
            },
            "finding_evidence": {
                "shortest_path_by_finding": evidence_paths,
                "minimum_path_length": min(evidence_paths.values(), default=0),
            },
        }

    def compare(
        self,
        adaptive_rows: dict[str, Any],
        baseline_rows: dict[str, Any],
    ) -> dict[str, Any]:
        adaptive_started = self._first_event(adaptive_rows["events"], "run_started")
        baseline_started = self._first_event(baseline_rows["events"], "run_started")
        mismatches: list[str] = []
        comparisons = {
            "dataset_checksum": (
                adaptive_rows["dataset"]["sha256"],
                baseline_rows["dataset"]["sha256"],
            ),
            "target_snapshot": (
                self._target_snapshot(adaptive_rows["target"]),
                self._target_snapshot(baseline_rows["target"]),
            ),
            "test_principal_refs": (
                sorted(adaptive_started.get("test_principal_refs", [])),
                sorted(baseline_started.get("test_principal_refs", [])),
            ),
            "policy": (
                self._normalized_policy(adaptive_rows["policy"]),
                self._normalized_policy(baseline_rows["policy"]),
            ),
            "evaluator_snapshot": (
                adaptive_started.get("evaluator_snapshot"),
                baseline_started.get("evaluator_snapshot"),
            ),
            "candidate_universe_checksum": (
                adaptive_started.get("candidate_universe_checksum"),
                baseline_started.get("candidate_universe_checksum"),
            ),
            "equipment_snapshot": (
                adaptive_started.get("equipment_snapshot", []),
                baseline_started.get("equipment_snapshot", []),
            ),
        }
        for field, (adaptive_value, baseline_value) in comparisons.items():
            if adaptive_value != baseline_value:
                mismatches.append(field)
        if not str(adaptive_rows["run"]["mode"]).startswith("adaptive"):
            mismatches.append("adaptive_mode")
        if "deterministic" not in str(baseline_rows["run"]["mode"]):
            mismatches.append("baseline_mode")

        adaptive_metrics = self.build(adaptive_rows)
        baseline_metrics = self.build(baseline_rows)
        adaptive_evidence = self._evaluation_completeness(adaptive_rows["events"])
        baseline_evidence = self._evaluation_completeness(baseline_rows["events"])
        shared_cases = sorted(adaptive_evidence.keys() & baseline_evidence.keys())
        evidence_regressions = [
            case_id
            for case_id in shared_cases
            if baseline_evidence[case_id] and not adaptive_evidence[case_id]
        ]
        adaptive_covered = len(adaptive_metrics["coverage"]["covered_tags"])
        baseline_covered = len(baseline_metrics["coverage"]["covered_tags"])
        target_call_savings = int(baseline_metrics["run"]["target_calls"]) - int(
            adaptive_metrics["run"]["target_calls"]
        )
        duration_savings = float(baseline_metrics["run"]["duration_seconds"]) - float(
            adaptive_metrics["run"]["duration_seconds"]
        )
        verified_discovery = int(adaptive_metrics["derived_cases"]["effective_discovery_count"])
        eligible = not mismatches
        has_measurable_benefit = (
            (target_call_savings > 0 and adaptive_covered >= baseline_covered)
            or verified_discovery > 0
            or adaptive_covered > baseline_covered
        )
        recommended = eligible and not evidence_regressions and has_measurable_benefit
        return {
            "comparison_eligible": eligible,
            "mismatch_reasons": mismatches,
            "efficiency": {
                "target_call_savings": target_call_savings if eligible else None,
                "duration_savings_seconds": duration_savings if eligible else None,
                "adaptive_provider_physical_attempts": adaptive_metrics["planner"][
                    "physical_attempts"
                ],
                "baseline_provider_physical_attempts": baseline_metrics["planner"][
                    "physical_attempts"
                ],
                "adaptive_estimated_model_cost": adaptive_metrics["planner"]["estimated_cost"],
                "baseline_estimated_model_cost": baseline_metrics["planner"]["estimated_cost"],
                "adaptive_covered_tags": adaptive_covered,
                "baseline_covered_tags": baseline_covered,
                "evidence_regression_case_ids": evidence_regressions,
            },
            "discovery": {
                "verified_derived_case_count": verified_discovery,
                "unverified_derived_case_count": adaptive_metrics["derived_cases"]["frozen_count"],
            },
            "adaptive_recommended": recommended,
            "recommendation_reason": self._recommendation_reason(
                eligible=eligible,
                mismatches=mismatches,
                evidence_regressions=evidence_regressions,
                has_measurable_benefit=has_measurable_benefit,
            ),
        }

    @staticmethod
    def _first_event(events: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
        return next(
            (event["evidence"] for event in events if event["event_type"] == event_type),
            {},
        )

    @staticmethod
    def _planner_error_categories(event: dict[str, Any]) -> list[str]:
        usage = event["evidence"].get("usage", {})
        errors = list(usage.get("attempt_errors", []))
        if event["event_type"] == "planner_error":
            errors.append(str(event["evidence"].get("error_type", "planner_error")))
        return errors

    @staticmethod
    def _model_judge_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
        calls = [
            event["evidence"] for event in events if event["event_type"] == "model_judge_called"
        ]
        return {
            "decision_count": len(calls),
            "physical_attempts": sum(
                int(call.get("usage", {}).get("physical_attempts", 0)) for call in calls
            ),
            "input_tokens": sum(
                int(call.get("usage", {}).get("input_tokens", 0)) for call in calls
            ),
            "output_tokens": sum(
                int(call.get("usage", {}).get("output_tokens", 0)) for call in calls
            ),
            "latency_ms": sum(int(call.get("usage", {}).get("latency_ms", 0)) for call in calls),
            "estimated_cost": sum(
                float(call.get("usage", {}).get("estimated_cost", 0)) for call in calls
            ),
        }

    @staticmethod
    def _scope_metrics(
        rows: dict[str, Any],
        started: dict[str, Any],
    ) -> dict[str, Any]:
        principal_case_counts: Counter[str] = Counter()
        candidates_by_case: dict[str, str] = {}
        for event in rows["events"]:
            if event["event_type"] != "candidate_snapshot_created":
                continue
            for candidate in event["evidence"]["snapshot"].get("candidates", []):
                candidates_by_case[str(candidate["action_id"])] = str(
                    candidate["test_principal_ref"]
                )
        for step in rows["steps"]:
            principal = candidates_by_case.get(step["case_id"])
            if principal:
                principal_case_counts[principal] += 1
        return {
            "test_principal_refs": sorted(started.get("test_principal_refs", [])),
            "executed_cases_by_principal": dict(sorted(principal_case_counts.items())),
            "tenant_ids": sorted(
                {str(fixture["tenant_id"]) for fixture in rows.get("state_fixtures", [])}
            ),
            "session_ids": sorted(
                {str(fixture["session_id"]) for fixture in rows.get("state_fixtures", [])}
            ),
        }

    @staticmethod
    def _risk_coverage(steps: list[dict[str, Any]]) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for step in steps:
            case = step.get("result", {}).get("case", {})
            risk = case.get("severity")
            if risk:
                counts[str(risk)] += 1
        return dict(sorted(counts.items()))

    def _gain_comparison(
        self,
        gain: dict[str, Any],
    ) -> dict[str, str | int | None]:
        if int(gain.get("confirmed_finding_delta", 0)) > 0:
            actual = "high"
        elif any(
            int(gain.get(field, 0)) for field in ("coverage_delta", "evidence_completeness_delta")
        ):
            actual = "medium"
        else:
            actual = "low"
        predicted = gain.get("predicted_information_gain")
        return {
            "predicted": str(predicted) if predicted is not None else None,
            "actual": actual,
            "difference": (
                self._gain_rank.get(str(predicted), 0) - self._gain_rank.get(actual, 0)
                if predicted is not None
                else None
            ),
        }

    @staticmethod
    def _repeated_action_count(planner_events: list[dict[str, Any]]) -> int:
        decisions = [
            event["evidence"].get("decision", {}).get("candidate_id")
            for event in planner_events
            if event["event_type"] == "planner_decided"
        ]
        counts = Counter(decision for decision in decisions if decision)
        return sum(max(count - 1, 0) for count in counts.values())

    @staticmethod
    def _evaluator_outcome(evaluation: dict[str, Any]) -> str:
        results = evaluation.get("evaluator_results", [])
        verdicts = {
            result.get("verdict") for result in results if result.get("verdict") != "inconclusive"
        }
        if len(verdicts) > 1:
            return "conflict"
        if len(results) > 1 and len(verdicts) == 1:
            return "agreement"
        return "single"

    @staticmethod
    def _duration_seconds(run: dict[str, Any]) -> float:
        started_at = run.get("started_at")
        completed_at = run.get("completed_at")
        if not started_at or not completed_at:
            return 0
        return max(
            (
                datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)
            ).total_seconds(),
            0,
        )

    @staticmethod
    def _complete_loop_count(rows: dict[str, Any]) -> tuple[int, int]:
        event_types_by_operation: defaultdict[str, set[str]] = defaultdict(set)
        for event in rows["events"]:
            operation_id = str(event["operation_id"])
            marker = ":case:"
            if marker not in operation_id:
                continue
            prefix, suffix = operation_id.split(marker, maxsplit=1)
            case_parts = suffix.split(":")
            if len(case_parts) < 2:
                continue
            operation = f"{prefix}{marker}{case_parts[0]}:{case_parts[1]}"
            event_types_by_operation[operation].add(str(event["event_type"]))
        required = {
            "decision_bound",
            "target_called",
            "observation_normalized",
            "evaluation_completed",
            "case_persisted",
        }
        executed = [
            types for types in event_types_by_operation.values() if "target_called" in types
        ]
        complete = sum(
            required.issubset(types)
            and any(event_type.startswith("policy_") for event_type in types)
            for types in executed
        )
        return complete, len(executed)

    @staticmethod
    def _target_snapshot(target: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": target.get("name"),
            "endpoint": target.get("endpoint"),
            "config": target.get("config"),
        }

    @staticmethod
    def _normalized_policy(policy: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, value in policy.items():
            if key == "allowed_target_ids":
                continue
            normalized[key] = sorted(value) if isinstance(value, list) else value
        return normalized

    @staticmethod
    def _evaluation_completeness(
        events: list[dict[str, Any]],
    ) -> dict[str, bool]:
        return {
            str(event["evidence"]["case_id"]): bool(
                event["evidence"].get("evaluation", {}).get("evidence_complete")
            )
            for event in events
            if event["event_type"] == "evaluation_completed"
        }

    @staticmethod
    def _recommendation_reason(
        *,
        eligible: bool,
        mismatches: list[str],
        evidence_regressions: list[str],
        has_measurable_benefit: bool,
    ) -> str:
        if not eligible:
            return f"comparison snapshots differ: {', '.join(mismatches)}"
        if evidence_regressions:
            return "adaptive evidence completeness regressed for shared cases"
        if not has_measurable_benefit:
            return "no persisted efficiency, coverage, evidence, or verified discovery benefit"
        return "persisted comparable metrics show benefit without evidence regression"
