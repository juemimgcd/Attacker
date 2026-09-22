"""按连续的完整工具轮次压缩历史；不改写 SQL 原始事实。"""

import json
from collections import Counter
from collections.abc import Sequence

from app.schemas.agent_schema import HistorySummary
from app.schemas.prompt_schema import ToolExchange


def uncovered_history(
    history: Sequence[ToolExchange],
    summary: HistorySummary,
) -> tuple[ToolExchange, ...]:
    ids = tuple(turn.result_ref for turn in history)
    if ids[: len(summary.covered_ids)] != summary.covered_ids:
        raise ValueError("summary must cover a continuous prefix of tool history")
    return tuple(history[len(summary.covered_ids) :])


def choose_compaction_range(
    history: Sequence[ToolExchange],
    keep_recent: int = 1,
) -> tuple[ToolExchange, ...]:
    if keep_recent < 1:
        raise ValueError("compaction must preserve at least one complete recent turn")
    return tuple(history[: max(0, len(history) - keep_recent)])


def compact(
    history: Sequence[ToolExchange],
    previous: HistorySummary,
    *,
    keep_recent: int = 1,
) -> HistorySummary:
    """业务结果已有结构化事实，用确定性摘要代替额外模型调用。"""

    covered = choose_compaction_range(uncovered_history(history, previous), keep_recent)
    if not covered:
        raise ValueError("no complete old turns available for compaction")
    ids = (*previous.covered_ids, *(turn.result_ref for turn in covered))
    prefix = history[: len(ids)]
    results = [json.loads(turn.result) for turn in prefix]
    counts = Counter(str(item.get("outcome", item.get("status", "unknown"))) for item in results)
    findings = {item["finding_ref"] for item in results if item.get("finding_ref")}
    text = json.dumps(
        {
            "completed_tool_turns": len(ids),
            "outcomes": dict(counts),
            "finding_count": len(findings),
            "details": "Full calls and results remain in SQL.",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    previous_size = len(previous.text.encode()) + sum(
        len(turn.model_dump_json().encode()) for turn in covered
    )
    if len(text.encode()) >= previous_size:
        raise ValueError("summary did not reduce model context")
    return HistorySummary(text=text, covered_ids=ids, version=previous.version + 1)
