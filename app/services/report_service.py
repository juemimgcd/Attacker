from typing import Any

from app.repositories.run_repository import RunRepository


class ReportService:
    def __init__(self, repository: RunRepository) -> None:
        self.repository = repository

    async def build_json(self, run_id: str) -> dict[str, Any]:
        rows = await self.repository.get_report_rows(run_id)
        run = rows["run"]
        findings = rows["findings"]
        evidence_ids = {event["id"] for event in rows["events"]}
        linked_findings = sum(
            bool(finding["evidence_event_ids"])
            and set(finding["evidence_event_ids"]).issubset(evidence_ids)
            for finding in findings
        )
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
            },
            "false_positives": run["false_positive_count"],
            "defense_overblocks": run["defense_overblock_count"],
            "target_calls": run["target_call_count"],
            "finding_evidence_link_rate": (linked_findings / len(findings) if findings else 1.0),
        }
        return rows

    async def build_markdown(self, run_id: str) -> str:
        report = await self.build_json(run_id)
        summary = report["summary"]
        target = report["target"]
        dataset = report["dataset"]
        lines = [
            f"# Attacker Black-Box Report: {run_id}",
            "",
            "## Run",
            "",
            f"- Status: `{summary['status']}`",
            f"- Target: `{target['name']}` (`{target['endpoint']}`)",
            f"- Dataset: `{dataset['name']}` (`{dataset['sha256']}`)",
            f"- Completed cases: {summary['completed_cases']}/{summary['total_cases']}",
            f"- Target calls: {summary['target_calls']}",
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
        return "\n".join(lines).rstrip() + "\n"
