"""模型可见工具只提出 Case/结束请求；显式工具表把请求交给 Policy Gate 和共享 Pipeline。"""

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from app.schemas.adaptive_agent_schema import PlannerReasonCode
from app.schemas.graybox_schema import PlannerContext, PlannerDecision
from app.schemas.prompt_schema import ModelToolCall, ModelToolFunction

if TYPE_CHECKING:
    from app.agent.runtime import AgentRuntime


async def execute_candidate(runtime: "AgentRuntime") -> None:
    state = runtime.state
    state.update(**await runtime.policy_gate())
    if state["next_action"] == "review":
        state.update(**await runtime.wait_for_approval())
        return
    if state["next_action"] == "skip":
        state.update(**await runtime.skip())
    elif state["next_action"] == "execute":
        state.update(**await runtime.execute_case())
        if state["status"] == "running":
            state.update(**await runtime.update_facts())
    if state["status"] == "running":
        state.update(**await runtime.decide_next())


async def finish_run(runtime: "AgentRuntime") -> None:
    runtime.state.update(**await runtime.finish_gate())


# 工具名 -> (决策 action, 模型说明, 执行函数)。JSON 与原生 tool calling 共用同一入口。
TOOLS = {
    "execute_candidate": (
        "execute",
        (
            "Request execution of one candidate from the current snapshot. Core checks policy and "
            "approval, executes the frozen case, evaluates it, and returns persisted observations."
        ),
        execute_candidate,
    ),
    "finish_run": (
        "finish",
        (
            "Request completion using persisted evidence. Core checks coverage and evidence gaps "
            "before accepting completion; rejected requests continue the evaluation loop."
        ),
        finish_run,
    ),
}


async def execute_tool(name: str, runtime: "AgentRuntime") -> None:
    await TOOLS[name][2](runtime)


def decision_to_tool_call(decision: PlannerDecision, call_id: str) -> ModelToolCall:
    arguments = decision.model_dump(mode="json", exclude={"action"}, exclude_none=True)
    return ModelToolCall(
        id=call_id,
        function=ModelToolFunction(
            name="execute_candidate" if decision.action == "execute" else "finish_run",
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )


def tool_schemas() -> tuple[dict[str, Any], ...]:
    schemas = []
    for name, (action, description, _) in TOOLS.items():
        parameters = deepcopy(PlannerDecision.model_json_schema())
        properties = parameters["properties"]
        properties.pop("action")
        required = ["candidate_snapshot_id", "reason_code", "evidence_refs", "hypothesis_refs"]
        if action == "execute":
            properties["candidate_id"] = {"type": "string", "minLength": 1}
            properties["expected_information_gain"] = {"$ref": "#/$defs/InformationGain"}
            required.extend(["candidate_id", "expected_information_gain"])
            reasons = [
                "deterministic_order",
                "coverage_gap",
                "evidence_gap",
                "hypothesis_validation",
                "control_pair",
            ]
        else:
            properties.pop("candidate_id")
            properties.pop("expected_information_gain")
            reasons = ["all_requirements_satisfied", "no_candidates", "budget_exhausted"]
        properties["reason_code"] = {"type": "string", "enum": reasons}
        parameters["required"] = required
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            }
        )
    return tuple(schemas)


def resolve_tool_call(call: ModelToolCall) -> PlannerDecision:
    """把原生 function call 转成现有决策契约；不执行模型提供的任意参数。"""

    definition = TOOLS.get(call.function.name)
    if definition is None:
        raise ValueError("unknown planner tool")
    arguments = json.loads(call.function.arguments)
    if not isinstance(arguments, dict) or "action" in arguments:
        raise ValueError("tool arguments must be an object without an action override")
    if (
        definition[0] == "finish"
        and {"candidate_id", "expected_information_gain"} & arguments.keys()
    ):
        raise ValueError("finish tool cannot select a candidate")
    return PlannerDecision.model_validate({"action": definition[0], **arguments})


def validate_decision(
    context: PlannerContext,
    decision: PlannerDecision,
) -> str | None:
    if decision.candidate_snapshot_id != context.candidate_snapshot_id:
        return "planner referenced an expired or unknown candidate snapshot"
    empty_run_finish = (
        decision.action == "finish"
        and decision.reason_code == PlannerReasonCode.no_candidates
        and not context.candidates
    )
    if not decision.evidence_refs and not empty_run_finish:
        return "planner decision must cite persisted evidence"
    current_evidence_refs = {
        *context.evidence_refs,
        *context.coverage_refs,
        *context.information_gain_refs,
    }
    if set(decision.evidence_refs) - current_evidence_refs:
        return "planner cited evidence outside the current context"
    if set(decision.hypothesis_refs) - set(context.hypothesis_refs):
        return "planner cited hypotheses outside the current context"
    if (
        decision.action == "execute"
        and sum(candidate.candidate_id == decision.candidate_id for candidate in context.candidates)
        != 1
    ):
        return "planner selected a candidate outside the current snapshot"
    return None
