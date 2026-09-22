# Adaptive Agent Runtime

Adaptive Agent 参考 Zeta 的手写 loop、请求上下文、完整轮次压缩和普通工具表，使用 Python
函数与 SQL Session。没有 StateGraph、interrupt、Saver 或第二个 checkpoint 数据库。

## 调用关系

```text
AdaptiveRunService.start / resume
  -> RunResources（本轮使用的 Target、Planner、凭据）+ RunState
  -> run_loop(runtime)
     -> runtime.prepare()：候选快照 + SQL 当前事实 + 已完成工具历史
     -> before_model
     -> runtime.request(context)
        -> planner.plan(context)
           -> prepare_context：构建视图，必要时 compact，再构建
           -> ModelProvider.infer
           -> 解析 JSON / 原生 tool call，校验实际展示的候选与引用
        -> 校验同 Run 的 SQL 引用，保存决策、usage 和稳定 operation_id
     -> after_model
     -> execute_tool(name, runtime)
        -> before_tool：可拒绝本次工具
        -> execute_candidate：Policy → Approval / Skip / Pipeline → 更新事实与停止判断
        -> finish_run：Finish Gate 接受或拒绝
        -> after_tool（已完成或拒绝，等待审批时不触发）
     -> after_turn：可中止仍在运行的 Run
     -> save_session → 下一轮或返回等待状态
     -> runtime.finish()：保存终态或等待审批/Planner 的 Session
```

审批恢复从已经绑定的 Case 继续，先重新执行 Policy Gate，不重复请求模型。Planner 暂停
恢复则重新准备候选和上下文。循环本身只处理顺序，预算、审批、停止及持久化的业务规则
由 `AgentRuntime` 调用现有领域服务处理。

| 文件 | 责任 |
|---|---|
| `app/agent/loop.py` | 一个显式 `while`，串起 prepare、request、tool 和 save |
| `app/agent/runtime.py` | 候选、规划、预算与降级，调用 Policy/Finish Gate 和共享 Pipeline |
| `app/agent/tools.py` | 普通 `TOOLS` 字典、Schema、参数校验和执行函数 |
| `app/agent/hooks.py` | 五个生命周期阶段、回调注册、有界执行和控制决定 |
| `app/agent/context.py` | 生成一次请求的 `ContextView`，按预算选择候选资源与历史后缀 |
| `app/agent/compaction.py` | 校验摘要覆盖范围，压缩连续的旧完整轮次 |
| `app/agent/session.py` / `state.py` | SQL Session 的版本、身份和可恢复状态 |
| `app/services/adaptive_run_service.py` | 创建/恢复入口、瞬时资源、跨进程恢复租约 |

## Hooks

`Hooks` 默认不注册任何回调，不改变原有执行行为。同一阶段的异步回调按注册顺序执行，
整组回调共用 `timeout`，默认 5 秒；第一个 `Decision(stop=True, reason=...)` 结束该组。
超时、异常及取消向外传播，由已有 Service 错误处理保存失败或取消终态，再向调用方抛出。

| 阶段 | 触发位置 | 允许返回 |
|---|---|---|
| `before_model` | 上下文准备完成、进入 `runtime.request()` 前 | `None` |
| `after_model` | `runtime.request()` 返回并更新状态后 | `None` |
| `before_tool` | 进入工具的 Policy / Finish Gate 前 | `None` 或 `Decision`；`stop=True` 拒绝当前工具 |
| `after_tool` | 工具执行或拒绝处理完成后 | `None` |
| `after_turn` | 工具轮次完成、保存 Session 前 | `None` 或 `Decision`；`stop=True` 将仍在运行的 Run 置为 `aborted` |

模型阶段包围的是一次规划请求处理，可能走已保存结果、预算停止或降级分支，不代表每次
都有新的 Provider 调用。若请求直接结束或暂停，未进入工具轮次，不触发工具及轮末回调。
工具等待审批时不触发 `after_tool` / `after_turn`；审批恢复重新进入 `before_tool`，
通过后继续检查 Policy，不重复请求模型。终态不会被 `after_turn` 的决定覆盖。

每个回调接收独立深拷贝的 `HookContext`：包含 `state`，模型阶段另有 `planner_context`，
工具及轮末阶段另有 `tool_name`。这些是只读观察快照，修改副本不影响真实运行或后续回调，
也不提供 Runtime、Repository 或运行时凭据。三个观察阶段必须返回 `None`，控制阶段的
拒绝或停止必须附带非空原因；原因脱敏后记录为 `agent_hook_decision`。

`before_tool` 拒绝 Case 时走已有跳过与结果落库流程，拒绝结束请求时记录 Finish 拒绝；
均不退回已消耗的预算。允许工具继续后，Policy、审批、预算和 Finish Gate 仍负责原有检查，
Hook 只能增加限制。`after_turn` 的中止同样经现有收尾和 Session 保存流程处理。

例如，在某轮产生新 Finding 后提前中止后续评测：

```python
from app.agent.hooks import Decision, HookContext, Hooks
from app.runtime import create_runtime
from app.schemas.graybox_schema import GrayBoxRunRequest


async def stop_after_finding(context: HookContext) -> Decision | None:
    if context.state["last_finding_delta"] > 0:
        return Decision(stop=True, reason="new finding requires review")
    return None


async def run_with_hooks(request: GrayBoxRunRequest):
    hooks = Hooks(timeout=5.0)
    hooks.register("after_turn", stop_after_finding)
    async with create_runtime(agent_hooks=hooks) as runtime:
        return await runtime.adaptive_run_service.start(request)
```

也可直接使用 `AdaptiveRunService(repository=repository, hooks=hooks)` 注入。启动、审批/
Planner 恢复及 Subagent 都使用同一组回调；跨 Run 可能并发调用，有状态回调应按
`context.state["run_id"]` 区分并处理并发。进程重启后需重新注册；Session 不保存回调代码。

## Context 和 Compact

`PlannerContext` 保存当前事实与完整工具历史。`ContextView` 是这一轮实际给模型的视图，
原始 SQL 事实不会被裁剪。

1. `uncovered_history()` 校验摘要的 `covered_ids` 必须恰好覆盖历史前缀。
2. 最小输入保留最近完整调用/结果、当前 Observation，以及存在候选时至少一个候选。
3. 按优先级加入候选与关联的 Hypothesis/Coverage，再加入最近观察。
4. 从新到旧纳入连续的完整工具轮次；放不下时停止，不能只保留 call 或 result。
5. 若有旧历史被排除，`compact()` 覆盖该旧前缀，递增摘要版本，再重建请求视图。

摘要采用确定性的结果聚合：完成轮次数、各 outcome/status 数量和 Finding 数量。这里
借鉴 Zeta 的覆盖范围与生命周期，但不增加 LLM 总结请求；摘要不保存旧响应的全部语义。
候选筛选、Policy、Finish Gate 和报告继续使用 SQL 权威事实，摘要不作为新 Evidence。

```text
可用输入 = window_tokens - output_tokens - margin_tokens - 工具 Schema 估算大小
```

预算使用 UTF-8 字节数加消息余量作保守估计，不是精确 tokenizer。输入同时受模型窗口
和 Prompt Profile 限制：JSON Profile 默认 4096，工具 Profile 默认 8192。Observation
超过单条限制时明确标记截断。最小输入仍超限则明确失败，交给已有 Planner 失败策略；
不会丢弃最近完整工具轮次来伪装成功。

模型调用记录的 `call_snapshot.context_snapshot` 保存真实消息输入与 checksum，
`tool_schemas` 保存工具定义，`history_summary` 单独记录摘要文本、版本及完整覆盖 ID。
覆盖 ID 不重复注入模型，避免摘要元数据随轮次增长占满窗口。`PromptGovernance.rebuild()`
可校验快照并重建相同消息；空历史不会改变旧的无工具 Prompt checksum。

## 工具

| 工具 | 模型参数 | Core 执行 |
|---|---|---|
| `execute_candidate` | 当前快照/候选 ID、理由码、引用、预期收益 | Policy → 审批或执行固定 Case → 评估、落库 |
| `finish_run` | 当前快照 ID、结束理由码和引用 | Finish Gate 检查覆盖、对照及证据缺口 |

`TOOLS` 将名称映射到 action、说明和普通异步函数；Schema 复用 `PlannerDecision`。
模型不能覆盖 action、传入任意 URL/命令，或选择请求视图外的候选。每轮最多一个工具。
JSON、确定性 Planner 与原生 tool calling 都进入相同执行入口。

原生请求使用 `tool_choice=required`、`parallel_tool_calls=false`；返回值仍做本地校验。
JSON/确定性决策转换为统一的调用记录。历史根据 `planner_decided`、`decision_bound` 和
`case_persisted` 配对；拒绝记录成为明确的失败结果。审批中的请求没有完成结果，确定性
降级执行不伪装成被拒绝工具的成功结果。只有完整、已提交的结果进入下一轮历史。

工具结果中的 `result_ref` 可以作为下一次决策的事实引用。Core 同时校验它存在于当前
请求视图和同 Run 的 SQL 记录；允许的结果类型为 Case 落库、Planner 拒绝和 Finish Gate
拒绝。拒绝/跳过只说明过去的处理结果，不产生 Finding，也不会绕过 Policy 或 Finish Gate。

## 配置

默认保留 `response_mode="json"`。支持函数调用的 Planner 可以使用以下 `planner` 子对象：

```json
{
  "backend": "openai_compatible",
  "endpoint": "http://localhost:9001/v1/chat/completions",
  "model": "your-tool-capable-model",
  "response_mode": "tools",
  "context_budget": {
    "window_tokens": 32768,
    "output_tokens": 2048,
    "margin_tokens": 1024
  }
}
```

外层请求仍需 Target、Dataset 和 Policy；`output_tokens` 通过 `max_tokens` 传给 Provider。
Subagent 复用同一手写循环，每个 Run 独立持有状态、上下文、Session 与预算。

Provider 先读取并校验响应中的用量，再校验 HTTP 状态、完成原因和输出内容。响应截断、
工具参数格式错误或 JSON 解析失败时，已报告的 token/cost 仍随错误写入 Planner 事件和
Run 预算；发生重试时按物理尝试累加。没有报告的用量沿用零值，不推算供应商计费。

## Session 与恢复

Session 作为 `agent_session_saved` 事件写入现有业务数据库，无需新表或 Alembic 迁移。
暂停、等待审批和恢复时，Run 状态、状态转换事件与 Session 在一个 SQL 事务中提交。
审批决议独立落库，恢复事务提交前仍保留等待状态；中断后可用相同决议继续恢复。
事件 ID 是暂停位置的身份。进程内锁、SQL owner token 与定期续租共同保护恢复操作；
丢失租约就取消在途循环。

只有 `waiting_approval` 与 `paused` 可恢复。恢复前校验 Session 身份、冻结输入、凭据和
运行状态，在任何工具副作用前保存 `running` Session，消耗原暂停点。`running` 状态不
自动重放，因为无法据此证明外部调用没有发生。手工凭据恢复时重供；Provider 凭据按冻结
revision 在执行时短租，Session 不保存 Header、Token 或运行时对象。

本次移除 `CHECKPOINT__DATABASE_PATH`、`CHECKPOINT__URL`、LangGraph/Checkpoint 包及
psycopg。部署只需要业务数据库连接。已有 SQL Evidence、Finding、Report 与 Replay 保留；
旧 LangGraph 暂停记录不能直接在新循环中恢复，需要在旧版本完成，或基于冻结输入创建
新的运行。旧 checkpoint 文件不自动删除。历史数据库列/报告中的 `checkpoint_ref` 等名字
保留兼容，新的值指向 Session，不再代表图节点。

现有测试只迁移了审批恢复和灰盒加固的两个文件，未扩展场景。真实模型函数调用兼容性、
长历史压缩行为及精确 token 使用仍未通过真实模型服务验证。
