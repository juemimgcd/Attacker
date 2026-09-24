"""Orchestrator 委派独立 worker Run，并从持久化事实生成可追溯汇总。"""

import asyncio
import hashlib
import json
from collections import Counter, deque
from typing import Any
from uuid import uuid4

from app.infrastructure.model_provider import ModelProviderError
from app.repositories.subagent_repository import SubagentRepository
from app.schemas.graybox_schema import AttackPolicy, GrayBoxRunRequest, LoadedGrayBoxDataset
from app.schemas.subagent_schema import SubagentRunRequest
from app.services.adaptive_run_service import AdaptiveRunService
from app.services.concurrency_limiter import SharedConcurrencyLimiter
from app.services.orchestrator_model import OrchestratorModel
from app.services.report_service import ReportService


class SubagentService:
    def __init__(
        self,
        repository: SubagentRepository,
        adaptive: AdaptiveRunService,
        reports: ReportService,
        limiter: SharedConcurrencyLimiter,
    ) -> None:
        self.repository = repository
        self.adaptive = adaptive
        self.reports = reports
        self.limiter = limiter

    async def start(self, request: SubagentRunRequest) -> dict[str, Any]:
        dataset = await self.adaptive.loader.load(request.run.dataset_path, request.run.case_ids)
        cases = {case.id: case for case in dataset.cases}
        deadline = asyncio.get_running_loop().time() + request.run.policy.max_duration_seconds
        model = OrchestratorModel(request.orchestrator) if request.orchestrator else None
        plan_usage = None
        if model is not None:
            allowed = request.run.policy.allowed_case_ids
            scoped_cases = [
                case
                for case in dataset.cases
                if "allowed_case_ids" not in request.run.policy.model_fields_set
                or case.id in allowed
            ]
            if not request.worker_count <= len(scoped_cases) <= 128:
                raise ValueError("orchestrator requires 2-128 in-scope cases and one per worker")
            try:
                async with asyncio.timeout_at(deadline):
                    agents, plan_usage = await self.limiter.run(
                        self.limiter.model_quotas(model.config),
                        deadline,
                        lambda: model.plan(scoped_cases, request.worker_count),
                    )
            except ModelProviderError as exc:
                raise ValueError(f"orchestrator planning failed: {exc.error_category}") from exc
            except TimeoutError as exc:
                raise ValueError("orchestrator planning deadline exceeded") from exc
        else:
            agents = request.subagents or []
        assignments = []
        work: list[tuple[str, GrayBoxRunRequest, LoadedGrayBoxDataset]] = []
        for index, agent in enumerate(agents):
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
            policy = self._allocate_policy(
                request.run.policy,
                index,
                len(agents),
                reserve_provider_calls=2 if model else 0,
                reserve_cost_shares=2 if model else 0,
            )
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
                    "objective": agent.objective,
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
                "mode": "orchestrator_workers" if model else "supervisor_subagents",
                "summary_backend": "model_with_deterministic_evidence"
                if model
                else "deterministic_evidence",
                "orchestrator": (
                    {
                        "provider_id": request.orchestrator.provider_id,
                        "model": request.orchestrator.model,
                        "plan_usage": self._usage_snapshot(plan_usage),
                    }
                    if request.orchestrator
                    else None
                ),
                "dataset_sha256": dataset.sha256,
                "concurrency": request.concurrency,
                "parallel_target_safe": request.parallel_target_safe,
                "subagents": assignments,
            }
        )
        pending = deque(work)
        errors: dict[str, str] = {}

        async def execute(
            run_id: str, child: GrayBoxRunRequest, child_dataset: LoadedGrayBoxDataset
        ) -> None:
            try:
                await self.limiter.run(
                    self.limiter.worker_quotas(child.target, child.planner),
                    deadline,
                    lambda: self.adaptive.start_dataset(
                        child, child_dataset, requested_run_id=run_id
                    ),
                )
            except asyncio.CancelledError:
                errors[run_id] = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001 - preserve other subagents and safe error codes
                errors[run_id] = type(exc).__name__[:100]

        async def consume() -> None:
            while pending:
                # One event loop owns the deque; claiming an item has no await point.
                await execute(*pending.popleft())

        try:
            async with asyncio.timeout_at(deadline):
                results = await asyncio.gather(
                    *(consume() for _ in range(min(request.concurrency, len(work)))),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, BaseException) and not isinstance(
                        result, asyncio.CancelledError
                    ):
                        raise result
        except TimeoutError:
            errors["coordinator"] = "deadline_exceeded"
        finally:
            await self.repository.finish(coordinator_id, errors)
        if model is not None and asyncio.get_running_loop().time() < deadline:
            try:
                async with asyncio.timeout_at(deadline):
                    source_report = await self.report(coordinator_id)
                    narrative, usage = await self.limiter.run(
                        self.limiter.model_quotas(model.config),
                        deadline,
                        lambda: model.summarize(source_report),
                    )
                await self.repository.finish(
                    coordinator_id,
                    errors,
                    {
                        **narrative,
                        "usage": self._usage_snapshot(usage),
                        "source_digest": self._evidence_digest(source_report),
                    },
                )
            except (ModelProviderError, ValueError, TimeoutError, RuntimeError) as exc:
                errors["orchestrator_summary"] = (
                    exc.error_category
                    if isinstance(exc, ModelProviderError)
                    else type(exc).__name__
                )
                await self.repository.finish(coordinator_id, errors)
        elif model is not None:
            errors["orchestrator_summary"] = "deadline_exceeded"
            await self.repository.finish(coordinator_id, errors)
        return await self.report(coordinator_id)

    @staticmethod
    def _usage_snapshot(usage: Any) -> dict[str, Any] | None:
        if usage is None:
            return None
        return {
            "physical_attempts": usage.physical_attempts,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "estimated_cost": str(usage.estimated_cost),
        }

    @staticmethod
    def _evidence_digest(report: dict[str, Any]) -> str:
        facts = {
            "status": report["status"],
            "subagents": [
                {
                    "run_id": child["run_id"],
                    "status": child["status"],
                    "summary": child.get("summary"),
                    "pending_approval_ids": child.get("pending_approval_ids"),
                }
                for child in report["subagents"]
            ],
            "summary": report["summary"],
            "findings": sorted(
                (
                    {
                        **finding,
                        "sources": sorted(
                            finding["sources"],
                            key=lambda source: (source["run_id"], source["finding_id"]),
                        ),
                    }
                    for finding in report["findings"]
                ),
                key=lambda finding: finding["fingerprint"],
            ),
        }
        return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _allocate_policy(
        policy: AttackPolicy,
        index: int,
        count: int,
        reserve_provider_calls: int = 0,
        reserve_cost_shares: int = 0,
    ) -> AttackPolicy:
        # Omitted allowlists mean derive from the dataset; explicit empty lists mean deny all.
        values = policy.model_dump(exclude_unset=True)
        for field in ("max_steps", "max_target_calls", "max_provider_calls"):
            available = getattr(policy, field)
            if field == "max_provider_calls":
                available -= reserve_provider_calls
            share, remainder = divmod(available, count)
            values[field] = share + int(index < remainder)
        if policy.max_cost is not None:
            # Truncate every share: rounding up would exceed the parent ceiling.
            from decimal import ROUND_DOWN, localcontext

            with localcontext() as context:
                context.rounding = ROUND_DOWN
                values["max_cost"] = policy.max_cost / (count + reserve_cost_shares)
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
        agent_label = (
            "worker" if record["manifest"].get("mode") == "orchestrator_workers" else "Subagent"
        )
        result = {
            **record,
            "manifest": {
                key: value for key, value in record["manifest"].items() if key != "model_summary"
            },
            "status": status,
            "subagents": children,
            "summary": {
                "backend": "deterministic_evidence",
                "text": (
                    f"{len(children)} 个 {agent_label} 中 {statuses['completed']} 个执行完成；"
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
        model_summary = record["manifest"].get("model_summary")
        if record["manifest"].get("mode") == "orchestrator_workers":
            result["workers"] = children
            result["model_summary_status"] = "unavailable"
        if model_summary is not None:
            if model_summary.get("source_digest") == self._evidence_digest(result):
                result["model_summary"] = model_summary
                result["model_summary_status"] = "current"
            else:
                result["model_summary_status"] = "stale"
        return result
