"""主 Agent 委派独立自适应 Run，并从持久化事实生成可追溯汇总。"""

import asyncio
from collections import Counter
from typing import Any
from uuid import uuid4

from app.repositories.subagent_repository import SubagentRepository
from app.schemas.graybox_schema import AttackPolicy, GrayBoxRunRequest, LoadedGrayBoxDataset
from app.schemas.subagent_schema import SubagentRunRequest
from app.services.adaptive_run_service import AdaptiveRunService
from app.services.report_service import ReportService


class SubagentService:
    def __init__(
        self,
        repository: SubagentRepository,
        adaptive: AdaptiveRunService,
        reports: ReportService,
    ) -> None:
        self.repository = repository
        self.adaptive = adaptive
        self.reports = reports

    async def start(self, request: SubagentRunRequest) -> dict[str, Any]:
        dataset = await self.adaptive.loader.load(request.run.dataset_path, request.run.case_ids)
        cases = {case.id: case for case in dataset.cases}
        assignments = []
        work: list[tuple[str, GrayBoxRunRequest, LoadedGrayBoxDataset]] = []
        for index, agent in enumerate(request.subagents):
            selected = set(agent.case_ids)
            if selected - cases.keys():
                raise ValueError(f"unknown or out-of-scope cases for {agent.agent_id}")
            allowed = request.run.policy.allowed_case_ids
            if "allowed_case_ids" in request.run.policy.model_fields_set and not selected.issubset(
                allowed
            ):
                raise ValueError(f"subagent {agent.agent_id} exceeds allowed_case_ids")
            for case_id in selected:
                if not set(cases[case_id].prerequisite_case_ids).issubset(selected):
                    raise ValueError(f"subagent {agent.agent_id} is missing prerequisites")
            policy = self._allocate_policy(request.run.policy, index, len(request.subagents))
            policy.allowed_case_ids = selected
            for field, available in (
                ("allowed_capability_contracts", {cases[c].capability_contract for c in selected}),
                (
                    "allowed_provider_instance_refs",
                    {cases[c].provider_instance_ref for c in selected},
                ),
            ):
                if field in policy.model_fields_set:
                    setattr(policy, field, getattr(policy, field) & available)
            run_id = str(uuid4())
            assignments.append(
                {
                    "agent_id": agent.agent_id,
                    "run_id": run_id,
                    "case_ids": agent.case_ids,
                    "policy": policy.model_dump(mode="json"),
                }
            )
            child = request.run.model_copy(
                deep=True,
                update={
                    "case_ids": agent.case_ids,
                    "policy": policy,
                    "planner": agent.planner or request.run.planner,
                },
            )
            child_dataset = dataset.model_copy(
                deep=True, update={"cases": [cases[case_id] for case_id in agent.case_ids]}
            )
            work.append((run_id, child, child_dataset))

        coordinator_id = await self.repository.create(
            {
                "schema_version": 1,
                "mode": "supervisor_subagents",
                "summary_backend": "deterministic_evidence",
                "dataset_sha256": dataset.sha256,
                "concurrency": request.concurrency,
                "parallel_target_safe": request.parallel_target_safe,
                "subagents": assignments,
            }
        )
        semaphore = asyncio.Semaphore(request.concurrency)
        errors: dict[str, str] = {}

        async def execute(
            run_id: str, child: GrayBoxRunRequest, child_dataset: LoadedGrayBoxDataset
        ) -> None:
            try:
                async with semaphore:
                    await self.adaptive.start_dataset(child, child_dataset, requested_run_id=run_id)
            except asyncio.CancelledError:
                errors[run_id] = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001 - preserve other subagents and safe error codes
                errors[run_id] = type(exc).__name__[:100]

        try:
            async with asyncio.timeout(request.run.policy.max_duration_seconds):
                async with asyncio.TaskGroup() as group:
                    for run_id, child, child_dataset in work:
                        group.create_task(
                            execute(run_id, child, child_dataset), name=f"subagent-{run_id}"
                        )
        except TimeoutError:
            errors["coordinator"] = "deadline_exceeded"
        finally:
            await self.repository.finish(coordinator_id, errors)
        return await self.report(coordinator_id)

    @staticmethod
    def _allocate_policy(policy: AttackPolicy, index: int, count: int) -> AttackPolicy:
        # Omitted allowlists mean derive from the dataset; explicit empty lists mean deny all.
        values = policy.model_dump(exclude_unset=True)
        for field in ("max_steps", "max_target_calls", "max_provider_calls"):
            share, remainder = divmod(getattr(policy, field), count)
            values[field] = share + int(index < remainder)
        if policy.max_cost is not None:
            # Truncate every share: rounding up would exceed the parent ceiling.
            from decimal import ROUND_DOWN, localcontext

            with localcontext() as context:
                context.rounding = ROUND_DOWN
                values["max_cost"] = policy.max_cost / count
        return AttackPolicy.model_validate(values)

    async def report(self, coordinator_id: str) -> dict[str, Any]:
        """每次重新读取 SQL，使审批/恢复后的子 Run 结果进入同一个主报告。"""

        record = await self.repository.get(coordinator_id)
        children = []
        totals: Counter[str] = Counter()
        outcomes: Counter[str] = Counter()
        grouped: dict[str, dict[str, Any]] = {}
        for assignment in record["manifest"]["subagents"]:
            run_id = assignment["run_id"]
            child = {**assignment, "report_url": f"/runs/{run_id}/report.json"}
            try:
                report = await self.reports.build_json(run_id)
            except LookupError:
                child["status"] = (
                    "not_started" if record["dispatch_finished"] else "pending_or_interrupted"
                )
            else:
                summary = report["summary"]
                child.update(
                    status=summary["status"],
                    summary=summary,
                    terminal_reason=report["run"].get("terminal_reason"),
                    pending_approval_ids=[
                        approval["id"]
                        for approval in report["approvals"]
                        if approval["status"] == "pending"
                    ],
                )
                for field in (
                    "total_cases",
                    "completed_cases",
                    "target_calls",
                    "planner_calls",
                    "planner_tokens",
                    "policy_denials",
                ):
                    totals[field] += summary.get(field, 0)
                outcomes.update(summary["step_outcomes"])
                event_ids = {event["id"] for event in report["events"]}
                for finding in report["findings"]:
                    fingerprint = finding["fingerprint"]
                    group = grouped.setdefault(
                        fingerprint,
                        {
                            "fingerprint": fingerprint,
                            "case_id": finding["case_id"],
                            "category": finding["category"],
                            "is_control": finding["is_control"],
                            "sources": [],
                        },
                    )
                    evidence = finding["evidence_event_ids"]
                    group["sources"].append(
                        {
                            "agent_id": assignment["agent_id"],
                            "run_id": run_id,
                            "finding_id": finding["id"],
                            "outcome": finding["outcome"],
                            "risk_level": finding["risk_level"],
                            "evidence_event_ids": evidence,
                            "evidence_complete": bool(evidence)
                            and set(evidence).issubset(event_ids),
                        }
                    )
            if run_id in record["errors"]:
                child["dispatch_error"] = record["errors"][run_id]
            children.append(child)

        statuses = Counter(child["status"] for child in children)
        if statuses["completed"] == len(children) and not record["errors"]:
            status = "completed"
        elif statuses["waiting_approval"] or statuses["paused"]:
            status = "waiting"
        elif record["dispatch_finished"]:
            status = "partial"
        else:
            status = "running_or_interrupted"
        for finding in grouped.values():
            finding["conflicting_outcomes"] = (
                len({source["outcome"] for source in finding["sources"]}) > 1
            )
        gaps = sum(
            not source["evidence_complete"]
            for finding in grouped.values()
            for source in finding["sources"]
        )
        return {
            **record,
            "status": status,
            "subagents": children,
            "summary": {
                "backend": "deterministic_evidence",
                "text": (
                    f"{len(children)} 个 Subagent 中 {statuses['completed']} 个执行完成；"
                    f"已执行 {totals['completed_cases']} 个 Case，"
                    f"合并 {len(grouped)} 组 Finding，{gaps} 条 Finding 存在证据缺口。"
                    "执行完成不代表目标通过全部测试。"
                ),
                "agent_statuses": dict(statuses),
                "totals": dict(totals),
                "step_outcomes": dict(outcomes),
                "finding_groups": len(grouped),
                "finding_evidence_gaps": gaps,
            },
            "findings": list(grouped.values()),
        }
