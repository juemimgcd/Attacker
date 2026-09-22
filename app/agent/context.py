"""SQL 事实是原始记录；ContextView 是一次模型请求使用的预算内视图。"""

import json
from dataclasses import dataclass
from typing import Any

from app.agent.compaction import compact, uncovered_history
from app.schemas.adaptive_agent_schema import CandidateSnapshot
from app.schemas.agent_schema import HistorySummary
from app.schemas.graybox_schema import PlannerConfig, PlannerContext
from app.schemas.prompt_schema import (
    ObservationSummary,
    PromptBuildRequest,
    PromptBuildResult,
    PromptTask,
    ToolExchange,
)
from app.services.prompt_governance import PromptBudgetError, PromptGovernanceService


def build_context(
    snapshot: CandidateSnapshot, facts: dict[str, Any], remaining_steps: int
) -> PlannerContext:
    """准备当前候选与事实，不改变历史，也不在此阶段触发模型请求。"""

    candidates = list(snapshot.candidates[:100])
    observations = facts["observations"][-20:]
    findings = facts["finding_refs"][-100:]
    history = facts.get("tool_history", ())
    tags = {tag for candidate in candidates for tag in candidate.coverage_tags}
    return PlannerContext(
        candidate_snapshot_id=snapshot.snapshot_id,
        candidates=candidates,
        observations=observations,
        evidence_refs=list(
            dict.fromkeys(
                [
                    *(item.observation_ref for item in observations),
                    *findings,
                    *(turn.result_ref for turn in history[-20:]),
                ]
            )
        ),
        finding_refs=findings,
        hypothesis_refs=list(
            dict.fromkeys(ref for item in candidates for ref in item.hypothesis_refs)
        )[:100],
        coverage={tag: status for tag, status in facts["coverage"].items() if tag in tags},
        coverage_refs=facts["coverage_refs"][-100:],
        hypotheses=[
            facts["hypotheses"][item.action_id]
            for item in candidates
            if item.action_id in facts["hypotheses"]
        ],
        information_gain_refs=facts["information_gain_refs"][-100:],
        information_gains=facts["information_gains"][-100:],
        remaining_steps=max(remaining_steps, 0),
        history_summary=facts.get("history_summary", HistorySummary()),
        tool_history=history,
    )


@dataclass(frozen=True)
class ContextView:
    context: PlannerContext
    prompt: PromptBuildResult
    estimated_input_tokens: int
    needs_compaction: bool = False
    compaction_steps: int = 0


def prepare_context(
    context: PlannerContext,
    config: PlannerConfig,
    governance: PromptGovernanceService,
    tools: tuple[dict[str, Any], ...] = (),
) -> ContextView:
    """先构建视图；历史放不下时压缩旧前缀，再重建，不逐字段删除事实。"""

    tool_size = len(json.dumps(tools, ensure_ascii=False).encode()) if tools else 0
    available = config.context_budget.input_limit(tool_size)
    if available <= 0:
        raise PromptBudgetError("tool schemas leave no model input allowance")
    summary = context.history_summary
    steps = 0
    while True:
        remaining = uncovered_history(context.tool_history, summary)
        view = _build_view(context, remaining, summary, config, governance, available, steps)
        if not view.needs_compaction:
            return view
        summary = compact(context.tool_history, summary, keep_recent=len(view.context.tool_history))
        steps += 1


def _build_view(
    source: PlannerContext,
    history: tuple[ToolExchange, ...],
    summary: HistorySummary,
    config: PlannerConfig,
    governance: PromptGovernanceService,
    available: int,
    steps: int,
) -> ContextView:
    profile_id = "core.planner.tools.v1" if config.response_mode == "tools" else "core.planner.v1"
    limits = governance.get_limits(profile_id)

    def select(
        candidate_count: int, observation_count: int, turns: tuple[ToolExchange, ...]
    ) -> PlannerContext:
        candidates = source.candidates[:candidate_count]
        actions = {item.action_id for item in candidates}
        tags = {tag for item in candidates for tag in item.coverage_tags}
        observations = source.observations[-observation_count:] if observation_count else []
        marker = " [truncated; full observation in SQL]"
        observations = [
            item
            if len(item.summary) <= limits.max_chars_per_observation
            else item.model_copy(
                update={
                    "summary": item.summary[
                        : max(0, limits.max_chars_per_observation - len(marker))
                    ]
                    + marker[: limits.max_chars_per_observation]
                }
            )
            for item in observations
        ]
        findings = source.finding_refs[-1:]
        return source.model_copy(
            update={
                "candidates": candidates,
                "observations": observations,
                "hypotheses": [item for item in source.hypotheses if item.action_id in actions],
                "hypothesis_refs": list(
                    dict.fromkeys(ref for item in candidates for ref in item.hypothesis_refs)
                ),
                "coverage": {tag: status for tag, status in source.coverage.items() if tag in tags},
                "coverage_refs": source.coverage_refs[-1:],
                "information_gains": source.information_gains[-1:],
                "information_gain_refs": source.information_gain_refs[-1:],
                "finding_refs": findings,
                "evidence_refs": list(
                    dict.fromkeys(
                        [
                            *(item.observation_ref for item in observations),
                            *findings,
                            *(turn.result_ref for turn in turns),
                        ]
                    )
                ),
                "tool_history": turns,
                "history_summary": summary,
            }
        )

    def render(selected: PlannerContext) -> ContextView:
        prompt = governance.build(_prompt_request(selected, config, steps))
        size = len(
            json.dumps(
                [item.model_dump(exclude_none=True) for item in prompt.messages], ensure_ascii=False
            ).encode()
        ) + 32 * len(prompt.messages)
        if size > available:
            raise PromptBudgetError("context exceeds input allowance")
        if prompt.snapshot.template_version != config.prompt_template_version:
            raise ValueError("planner prompt template version is not approved")
        return ContextView(selected, prompt, size, len(selected.tool_history) < len(history), steps)

    # 最近完整轮次、当前观察和至少一个候选是请求的最小输入，放不下就明确失败。
    candidate_count = min(1, len(source.candidates))
    observation_count = min(1, len(source.observations))
    turns = history[-1:]
    view = render(select(candidate_count, observation_count, turns))
    # 候选与其 Hypothesis/Coverage 一起加入；优先级顺序来自 CandidateBuilder。
    for count in range(candidate_count + 1, len(source.candidates) + 1):
        try:
            grown = render(select(count, observation_count, turns))
        except PromptBudgetError:
            break
        candidate_count, view = count, grown
    for count in range(observation_count + 1, len(source.observations) + 1):
        try:
            grown = render(select(candidate_count, count, turns))
        except PromptBudgetError:
            break
        observation_count, view = count, grown
    # 只纳入连续的历史后缀；一旦超预算就停止，调用和结果始终成对。
    for count in range(2, len(history) + 1):
        try:
            grown = render(select(candidate_count, observation_count, history[-count:]))
        except PromptBudgetError:
            break
        view = grown
    return view


def _prompt_request(
    context: PlannerContext, config: PlannerConfig, steps: int
) -> PromptBuildRequest:
    native = config.response_mode == "tools"
    return PromptBuildRequest(
        task=PromptTask.planner_tools if native else PromptTask.planner,
        profile_id="core.planner.tools.v1" if native else "core.planner.v1",
        caller_id="attacker_core",
        schema_version="planner-decision-v1",
        trusted_payload={
            "candidate_snapshot_id": context.candidate_snapshot_id,
            "candidates": [item.model_dump(mode="json") for item in context.candidates],
            "coverage": {tag: status.value for tag, status in context.coverage.items()},
            "hypotheses": [item.model_dump(mode="json") for item in context.hypotheses],
            "information_gains": [
                item.model_dump(mode="json") for item in context.information_gains
            ],
            "remaining_steps": context.remaining_steps,
            "history_summary": context.history_summary.text,
            "compaction_steps": steps,
        },
        observations=[
            ObservationSummary(observation_ref=item.observation_ref, summary=item.summary)
            for item in context.observations
        ],
        fact_refs=list(
            dict.fromkeys(
                [
                    context.candidate_snapshot_id,
                    *context.evidence_refs,
                    *context.finding_refs,
                    *context.hypothesis_refs,
                    *context.coverage_refs,
                    *context.information_gain_refs,
                ]
            )
        ),
        model_id=config.model,
        provider_id=config.provider_id,
        model_parameters={
            "temperature": config.temperature,
            "max_tokens": config.context_budget.output_tokens,
        },
        tool_history=context.tool_history,
    )
