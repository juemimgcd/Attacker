"""显式串起上下文准备、模型请求、工具执行与 Session 保存。"""

from app.agent.runtime import AgentRuntime
from app.agent.session import save_session
from app.agent.state import RunState
from app.agent.tools import execute_tool


async def run_loop(runtime: AgentRuntime) -> RunState:
    state = runtime.state
    # 审批恢复继续已经绑定的工具；重新检查 Policy，不重复请求模型。
    if state["next_action"] == "policy":
        await execute_tool("execute_candidate", runtime)
        await runtime.after_turn("execute_candidate")
    while state["status"] == "running":
        context = await runtime.prepare()
        await runtime.invoke_hook("before_model", planner_context=context)
        state.update(**await runtime.request(context))
        await runtime.invoke_hook("after_model", planner_context=context)
        if state["next_action"] in {"finalize", "pause"}:
            break
        name = "finish_run" if state["next_action"] == "finish" else "execute_candidate"
        await execute_tool(name, runtime)
        await runtime.after_turn(name)
        if state["status"] == "running":
            await save_session(runtime.repository, state)
    await runtime.finish()
    return state
