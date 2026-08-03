"""仅从 SQL 业务事实构建 JSON/Markdown 报告，不读取 checkpoint 推断结果。"""

from collections import Counter
from datetime import datetime
from typing import Any

from app.repositories.equipment_repository import EquipmentRepository
from app.repositories.run_repository import RunRepository
from app.services.adaptive_observability import AdaptiveObservabilityService


class ReportService:
    """聚合 Run、Evidence、Finding、装备和自适应可观测指标。"""

    def __init__(
        self,
        repository: RunRepository,
        equipment_repository: EquipmentRepository | None = None,
    ) -> None:
        self.repository = repository
        self.equipment_repository = equipment_repository
        self.adaptive_observability = AdaptiveObservabilityService()

    async def build_json(self, run_id: str) -> dict[str, Any]:
        """生成机器可读报告，并保留 Evidence 缺口和清理失败等负面事实。"""

        rows = await self.repository.get_report_rows(run_id)
        run = rows["run"]
        findings = rows["findings"]
        evidence_ids = {event["id"] for event in rows["events"]}
        linked_findings = sum(
            bool(finding["evidence_event_ids"])
            and set(finding["evidence_event_ids"]).issubset(evidence_ids)
            for finding in findings
        )
        step_outcomes = Counter(step["outcome"] for step in rows["steps"])
        rows["summary"] = {
            "status": run["status"],
            "total_cases": run["total_cases"],
            "completed_cases": run["completed_cases"],
            "outcomes": {
                "violation": run["violation_count"],
                "refused": run["refused_count"],
                "safe": run["safe_count"],
                "error": run["error_count"],
                "budget_aborted": run["budget_aborted_count"],
                "not_evaluable": step_outcomes.get("not_evaluable", 0),
            },
            "false_positives": run["false_positive_count"],
            "defense_overblocks": run["defense_overblock_count"],
            "target_calls": run["target_call_count"],
            "tool_calls": run.get("tool_call_count", 0),
            "planner_calls": run.get("planner_call_count", 0),
            "planner_tokens": run.get("planner_token_count", 0),
            "policy_denials": run.get("policy_denied_count", 0),
            "approval_requests": len(rows["approvals"]),
            "finding_evidence_link_rate": (linked_findings / len(findings) if findings else 1.0),
        }
        rows["react_summary"] = self._react_metrics(rows)
        if self.equipment_repository is not None:
            rows["equipment_snapshots"] = await self.equipment_repository.list_snapshots(run_id)
        rows["summary"]["step_outcomes"] = dict(step_outcomes)
        if "stateful" in run["mode"]:
            retrieval_documents = [
                document for retrieval in rows["retrievals"] for document in retrieval["documents"]
            ]
            cleanup_events = [
                event
                for event in rows["events"]
                if event["event_type"] == "state_cleanup_completed"
            ]
            recovery_events = [
                event
                for event in rows["events"]
                if event["event_type"] == "recovery_policy_revalidated"
            ]
            rows["stateful_summary"] = {
                "state_writes": len(rows["state_fixtures"]),
                "remaining_active_fixtures": sum(
                    fixture["active"] for fixture in rows["state_fixtures"]
                ),
                "cleanup_operations": len(cleanup_events),
                "outside_scope_cleanup_effects": sum(
                    event["evidence"]["cleanup"]["outside_scope_affected_count"]
                    for event in cleanup_events
                ),
                "retrieval_documents": len(retrieval_documents),
                "retrieval_allowed": sum(document["allowed"] for document in retrieval_documents),
                "retrieval_filtered": sum(
                    not document["allowed"] for document in retrieval_documents
                ),
                "recovery_checks": len(recovery_events),
                "recovery_policy_revalidated": sum(
                    bool(event["evidence"]["policy_revalidated"]) for event in recovery_events
                ),
                "identity_isolation_findings": sum(
                    finding["category"] == "identity_isolation" for finding in findings
                ),
            }
        baseline_run_id = run.get("baseline_run_id")
        if str(run["mode"]).startswith("adaptive"):
            rows["adaptive_observability"] = self.adaptive_observability.build(rows)
        if baseline_run_id:
            baseline_rows = await self.repository.get_report_rows(baseline_run_id)
            baseline_metrics = self._react_metrics(baseline_rows)
            baseline_cases = {finding["case_id"] for finding in baseline_rows["findings"]}
            adaptive_cases = {finding["case_id"] for finding in findings}
            rows["comparison"] = {
                "baseline_run_id": baseline_run_id,
                "adaptive_only_finding_case_ids": sorted(adaptive_cases - baseline_cases),
                "baseline_only_finding_case_ids": sorted(baseline_cases - adaptive_cases),
                "shared_finding_case_ids": sorted(adaptive_cases & baseline_cases),
                "quality": {
                    "adaptive": {
                        "duration_seconds": rows["react_summary"]["duration_seconds"],
                        "finding_count": len(findings),
                        "finding_evidence_link_rate": rows["summary"]["finding_evidence_link_rate"],
                        "covered_tags": rows["react_summary"]["covered_tags"],
                        "actual_information_gain": rows["react_summary"]["actual_information_gain"],
                    },
                    "baseline": {
                        "duration_seconds": baseline_metrics["duration_seconds"],
                        "finding_count": len(baseline_rows["findings"]),
                        "finding_evidence_link_rate": self._finding_evidence_link_rate(
                            baseline_rows
                        ),
                        "covered_tags": baseline_metrics["covered_tags"],
                        "actual_information_gain": baseline_metrics["actual_information_gain"],
                    },
                },
                "usage": {
                    "adaptive": {
                        "planner_calls": run.get("planner_call_count", 0),
                        "planner_tokens": run.get("planner_token_count", 0),
                        "target_calls": run["target_call_count"],
                        "tool_calls": run.get("tool_call_count", 0),
                    },
                    "baseline": {
                        "planner_calls": baseline_rows["run"].get("planner_call_count", 0),
                        "planner_tokens": baseline_rows["run"].get("planner_token_count", 0),
                        "target_calls": baseline_rows["run"]["target_call_count"],
                        "tool_calls": baseline_rows["run"].get("tool_call_count", 0),
                    },
                },
                **self.adaptive_observability.compare(rows, baseline_rows),
            }
        return rows

    @staticmethod
    def _finding_evidence_link_rate(rows: dict[str, Any]) -> float:
        evidence_ids = {event["id"] for event in rows["events"]}
        findings = rows["findings"]
        linked = sum(
            bool(finding["evidence_event_ids"])
            and set(finding["evidence_event_ids"]).issubset(evidence_ids)
            for finding in findings
        )
        return linked / len(findings) if findings else 1.0

    @classmethod
    def _react_metrics(cls, rows: dict[str, Any]) -> dict[str, Any]:
        events = rows["events"]
        planner_decisions = [
            event
            for event in events
            if event["event_type"] in {"planner_decided", "planner_rejected"}
        ]
        traceable_decisions = sum(
            bool(event["evidence"].get("call_snapshot", {}).get("input_fact_refs"))
            and bool(event["evidence"].get("call_snapshot", {}).get("prompt_checksum"))
            for event in planner_decisions
        )
        latest_coverage: dict[str, str] = {}
        latest_evidence_complete: dict[str, bool] = {}
        seen_covered_tags: set[str] = set()
        seen_conclusive_cases: set[str] = set()
        seen_finding_cases: set[str] = set()
        no_gain_steps = 0
        for step in rows["steps"]:
            result = step.get("result") or {}
            case = result.get("case") or {}
            evaluation = result.get("evaluation") or {}
            case_id = str(step["case_id"])
            evidence_complete = bool(evaluation.get("evidence_complete", False))
            latest_evidence_complete[case_id] = evidence_complete
            coverage_tags = case.get("coverage_tags") or [case.get("category")]
            new_coverage = False
            for tag in coverage_tags:
                if tag:
                    normalized_tag = str(tag)
                    latest_coverage[normalized_tag] = (
                        "covered" if evidence_complete else "inconclusive"
                    )
                    if evidence_complete and normalized_tag not in seen_covered_tags:
                        seen_covered_tags.add(normalized_tag)
                        new_coverage = True
            new_evidence = evidence_complete and case_id not in seen_conclusive_cases
            if new_evidence:
                seen_conclusive_cases.add(case_id)
            new_finding = (
                evidence_complete
                and bool(evaluation.get("violated", False))
                and case_id not in seen_finding_cases
            )
            if new_finding:
                seen_finding_cases.add(case_id)
            if not any((new_coverage, new_evidence, new_finding)):
                no_gain_steps += 1
        verified_derived_case_ids = {
            str(event["evidence"]["derived_case"]["derived_case_id"])
            for event in events
            if event["event_type"] == "derived_case_verified"
        }
        frozen_derived_case_ids = {
            str(event["evidence"]["derived_case"]["derived_case_id"])
            for event in events
            if event["event_type"] == "derived_case_frozen"
        }
        covered_tags = sorted(tag for tag, status in latest_coverage.items() if status == "covered")
        conclusive_hypotheses = {
            case_id
            for case_id, evidence_complete in latest_evidence_complete.items()
            if evidence_complete
        }
        confirmed_finding_fingerprints = {
            finding["fingerprint"]
            for finding in rows["findings"]
            if not finding.get("is_control", False)
        }
        run = rows["run"]
        duration_seconds = cls._duration_seconds(
            run.get("started_at"),
            run.get("completed_at"),
        )
        return {
            "planner_decisions": len(planner_decisions),
            "planner_acceptances": sum(
                event["event_type"] == "planner_decided" for event in planner_decisions
            ),
            "planner_rejections": sum(
                event["event_type"] == "planner_rejected" for event in planner_decisions
            ),
            "planner_errors": sum(event["event_type"] == "planner_error" for event in events),
            "finish_gate_rejections": sum(
                event["event_type"] == "planner_finish_rejected" for event in events
            ),
            "traceable_decision_rate": (
                traceable_decisions / len(planner_decisions) if planner_decisions else 1.0
            ),
            "actual_information_gain": {
                "coverage_delta": len(covered_tags),
                "evidence_completeness_delta": len(conclusive_hypotheses),
                "confirmed_finding_delta": len(confirmed_finding_fingerprints),
                "no_gain_steps": no_gain_steps,
            },
            "covered_tags": covered_tags,
            "verified_derived_case_ids": sorted(verified_derived_case_ids),
            "unverified_derived_case_ids": sorted(
                frozen_derived_case_ids - verified_derived_case_ids
            ),
            "duration_seconds": duration_seconds,
        }

    @staticmethod
    def _duration_seconds(started_at: str | None, completed_at: str | None) -> float | None:
        if not started_at or not completed_at:
            return None
        return max(
            (
                datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)
            ).total_seconds(),
            0,
        )

    async def build_markdown(self, run_id: str) -> str:
        """把同一 JSON 事实投影为面向人工审阅的 Markdown。"""

        report = await self.build_json(run_id)
        summary = report["summary"]
        target = report["target"]
        dataset = report["dataset"]
        lines = [
            f"# Attacker Evaluation Report: {run_id}",
            "",
            "## Run",
            "",
            f"- Status: `{summary['status']}`",
            f"- Target: `{target['name']}` (`{target['endpoint']}`)",
            f"- Dataset: `{dataset['name']}` (`{dataset['sha256']}`)",
            f"- Completed cases: {summary['completed_cases']}/{summary['total_cases']}",
            f"- Target calls: {summary['target_calls']}",
            f"- Tool calls: {summary['tool_calls']}",
            f"- Planner calls/tokens: {summary['planner_calls']}/{summary['planner_tokens']}",
            f"- Finding evidence link rate: {summary['finding_evidence_link_rate']:.0%}",
            "",
            "## Outcomes",
            "",
            "| Outcome | Count |",
            "|---|---:|",
        ]
        lines.extend(f"| {outcome} | {count} |" for outcome, count in summary["outcomes"].items())
        lines.extend(
            [
                f"| false_positive | {summary['false_positives']} |",
                f"| defense_overblock | {summary['defense_overblocks']} |",
                "",
                "## Case Results",
                "",
                "| # | Case | Category | Kind | Outcome |",
                "|---:|---|---|---|---|",
            ]
        )
        for step in report["steps"]:
            case = step["result"]["case"]
            lines.append(
                f"| {step['sequence']} | `{step['case_id']}` | {case['category']} | "
                f"{case['kind']} | **{step['outcome']}** |"
            )

        react = report["react_summary"]
        lines.extend(
            [
                "",
                "## ReAct",
                "",
                (
                    f"- Planner decisions/rejections: "
                    f"{react['planner_decisions']}/{react['planner_rejections']}"
                ),
                f"- Traceable decision rate: {react['traceable_decision_rate']:.0%}",
                f"- Finish Gate rejections: {react['finish_gate_rejections']}",
                f"- Covered tags: {len(react['covered_tags'])}",
                (
                    "- Actual information gain: "
                    f"{react['actual_information_gain']['coverage_delta']} coverage, "
                    f"{react['actual_information_gain']['evidence_completeness_delta']} evidence, "
                    f"{react['actual_information_gain']['confirmed_finding_delta']} findings"
                ),
                (
                    "- Deterministically verified DerivedCases: "
                    f"{len(react['verified_derived_case_ids'])}"
                ),
                (f"- Frozen, unverified DerivedCases: {len(react['unverified_derived_case_ids'])}"),
            ]
        )
        lines.extend(["", "## Findings", ""])
        if not report["findings"]:
            lines.append("No violations produced a Finding.")
        for finding in report["findings"]:
            evidence = ", ".join(f"`{event_id}`" for event_id in finding["evidence_event_ids"])
            lines.extend(
                [
                    f"### {finding['case_id']}",
                    "",
                    f"- Risk: `{finding['risk_level']}`",
                    f"- Reason: {finding['reason']}",
                    f"- Evidence events: {evidence}",
                    "",
                ]
            )
        comparison = report.get("comparison")
        if comparison:
            lines.extend(
                [
                    "## Adaptive / Deterministic Comparison",
                    "",
                    f"- Baseline run: `{comparison['baseline_run_id']}`",
                    "- Adaptive-only findings: "
                    + ", ".join(
                        f"`{case_id}`" for case_id in comparison["adaptive_only_finding_case_ids"]
                    ),
                    "- Baseline-only findings: "
                    + ", ".join(
                        f"`{case_id}`" for case_id in comparison["baseline_only_finding_case_ids"]
                    ),
                    (f"- Comparison eligible: `{comparison['comparison_eligible']}`"),
                    (f"- Adaptive recommended: `{comparison['adaptive_recommended']}`"),
                    f"- Recommendation: {comparison['recommendation_reason']}",
                    "",
                ]
            )
        observability = report.get("adaptive_observability")
        if observability:
            planner = observability["planner"]
            candidates = observability["candidates"]
            gain = observability["information_gain"]
            loops = observability["loops"]
            run_metrics = observability["run"]
            evidence = observability["finding_evidence"]
            lines.extend(
                [
                    "## Adaptive Observability",
                    "",
                    (
                        "- Planner decisions/rejections/fallbacks/errors: "
                        f"{planner['decision_count']}/{planner['rejection_count']}/"
                        f"{planner['fallback_count']}/{planner['error_count']}"
                    ),
                    (
                        "- Provider physical attempts/tokens/latency: "
                        f"{planner['physical_attempts']}/"
                        f"{planner['input_tokens'] + planner['output_tokens']}/"
                        f"{planner['latency_ms']} ms"
                    ),
                    (
                        "- Candidate generated/filtered/snapshots expired: "
                        f"{candidates['generated_total']}/{candidates['filtered_total']}/"
                        f"{candidates['snapshot_expired_count']}"
                    ),
                    (
                        "- Actual coverage/evidence/finding gain: "
                        f"{gain['coverage_delta']}/{gain['evidence_delta']}/"
                        f"{gain['confirmed_finding_delta']}"
                    ),
                    (f"- Prediction mismatches: {gain['prediction_mismatch_count']}"),
                    (
                        "- Repeated actions/max repeated state/max no-gain: "
                        f"{loops['repeated_action_count']}/"
                        f"{loops['max_repeated_state_count']}/"
                        f"{loops['max_consecutive_no_gain_steps']}"
                    ),
                    (
                        "- Complete executed loops: "
                        f"{run_metrics['complete_loop_count']}/"
                        f"{run_metrics['executed_step_count']}"
                    ),
                    f"- Stop reason: `{run_metrics['stop_reason']}`",
                    (f"- Checkpoint/runtime resumes: {run_metrics['checkpoint_resume_count']}"),
                    (f"- Minimum Finding evidence path: {evidence['minimum_path_length']}"),
                    "",
                ]
            )
        stateful = report.get("stateful_summary")
        if stateful:
            lines.extend(
                [
                    "## Stateful Evidence",
                    "",
                    f"- State writes: {stateful['state_writes']}",
                    f"- Remaining active fixtures: {stateful['remaining_active_fixtures']}",
                    f"- Cleanup operations: {stateful['cleanup_operations']}",
                    f"- Outside-scope cleanup effects: {stateful['outside_scope_cleanup_effects']}",
                    (
                        "- Retrieval allowed/filtered: "
                        f"{stateful['retrieval_allowed']}/"
                        f"{stateful['retrieval_filtered']}"
                    ),
                    (
                        "- Recovery policy revalidated: "
                        f"{stateful['recovery_policy_revalidated']}/"
                        f"{stateful['recovery_checks']}"
                    ),
                    "",
                ]
            )
        equipment_snapshots = report.get("equipment_snapshots", [])
        if equipment_snapshots:
            lines.extend(
                [
                    "## Equipment Bindings",
                    "",
                    "| Type | Package | Version | Checksum | Instance | Config revision |",
                    "|---|---|---|---|---|---|",
                ]
            )
            for snapshot in equipment_snapshots:
                lines.append(
                    f"| {snapshot['package_type']} | `{snapshot['package_id']}` | "
                    f"`{snapshot['version']}` | `{snapshot['checksum']}` | "
                    f"`{snapshot.get('provider_instance_id') or ''}` | "
                    f"`{snapshot.get('config_revision') or ''}` |"
                )
            lines.append("")
        replay = report.get("replay")
        if replay:
            diff = replay["diff"]
            lines.extend(
                [
                    "## Replay Differences",
                    "",
                    f"- Source run: `{replay['source_run_id']}`",
                    f"- Replay run: `{replay['replay_run_id']}`",
                    "- Fixed: " + ", ".join(f"`{case}`" for case in diff["fixed"]),
                    "- New: " + ", ".join(f"`{case}`" for case in diff["new"]),
                    "- Persistent: " + ", ".join(f"`{case}`" for case in diff["persistent"]),
                    "- Regressed: " + ", ".join(f"`{case}`" for case in diff["regressed"]),
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"
