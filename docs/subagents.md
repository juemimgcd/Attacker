# Orchestrator 与 worker 测试

Orchestrator 模型读取数据集内的 Case 元数据，自主拆分测试范围并分配给多个 worker。
服务端校验完整覆盖、Case 范围、前置依赖与预算后，启动独立的 Adaptive Run。
每个 worker 运行自己的 Planner → Policy Gate → Execute → Evaluate 循环，拥有独立
run_id、thread_id、候选、假设和 SQL Session。结束后 Orchestrator 根据持久化证据
生成模型总结；原始确定性汇总、每个 worker 的报告和 Finding 来源同时保留。
现有单 Run API 保持可用。

模型仅选择现有 Case，不生成新攻击用例或修改 Policy。worker 可使用现有
`deterministic` 或 `openai_compatible` Planner。旧的显式 `subagents` 配置仍可使用，
此时分工与汇总保持确定性；模型模式使用 `orchestrator` 配置，二者不能同时提交。
没有递归委派或根据总结再次追加测试。

## 使用

生产数据库先按项目既有流程执行 `uv run attacker migrate`，升级到
`20260923_0012`。以下配置可保存为 `subagents.json`，Target 地址应替换为已授权的
灰盒 Target。下面是模型自主拆分的配置，Orchestrator 的 endpoint 是兼容
OpenAI Chat Completions 的模型接口。示例 worker 使用确定性 Planner；如需 worker
也使用模型，把 `run.planner` 改成自己的 `openai_compatible` 配置。

```json
{
  "run": {
    "target": {
      "name": "graybox-sandbox",
      "endpoint": "http://127.0.0.1:9000/chat"
    },
    "dataset_path": "samples/graybox/phase2.yaml",
    "policy": {
      "max_steps": 40,
      "max_target_calls": 20,
      "max_duration_seconds": 300,
      "max_provider_calls": 4
    },
    "planner": {"backend": "deterministic"}
  },
  "orchestrator": {
    "backend": "openai_compatible",
    "endpoint": "http://127.0.0.1:8001/v1/chat/completions",
    "model": "your-model"
  },
  "worker_count": 2,
  "concurrency": 1
}
```

```sh
uv run attacker subagents run --config subagents.json
uv run attacker subagents list
uv run attacker subagents report <coordinator_id>
```

HTTP 接口（沿用部署的 `X-API-Key` 门禁）：

- `POST /runs/subagents`：接收上述 JSON，等待本轮委派和总结收敛后返回主报告。
- `GET /runs/subagents?limit=20`：查看最近的主任务与预分配子 Run ID。
- `GET /runs/subagents/{coordinator_id}`：从子 Run 的最新 SQL 事实重建主报告。
- 子 Run 继续使用 `/runs/{run_id}`、`/runs/{run_id}/control`、
  `/runs/{run_id}/resume` 和现有审批接口。

## 范围、预算与并发

每组最多 16 个 worker；模型模式要求至少每个 worker 一个 Case，最多读取 128 个
范围内 Case。模型输出必须覆盖所有范围内 Case，只能引用已有 ID，并自行包含前置
Case；无效分工会被拒绝，不启动 Target 调用。显式分工模式保留原有边界。
同一个 Case 可以分配给不同 Subagent 做独立复核，调用消耗和执行数会分别计入，
不会把重复执行数量描述为唯一 Case 覆盖率。分工列表定义本组实际评测范围。

`max_steps`、`max_target_calls` 的整组预算按 worker 顺序均分，余数分给靠前的
worker。模型模式先为 Orchestrator 规划与总结各保留一次 Provider 调用，剩余
`max_provider_calls` 再均分；`max_cost` 为 Orchestrator 两次调用预留两个均分份额，
其余份额分给 worker。显式分工模式保持原有均分规则。单次模型调用的费用只能在
响应后获知，`max_cost` 无法作为模型提供商的预扣费上限。
所有其他授权和停止条件继承父 Policy。分配出去的闲置额度不会自动转给其他
Subagent，子 Run 恢复后仍使用其原预算。`stop_on_critical` 作用于各子 Run。

`max_duration_seconds` 同时限制本次主调度（包含排队）和各子 Run；主调度超时
会取消在途任务，保留已落库证据并返回 partial 报告。取消可能无法撤回 Target
已经接收的请求，协调器不会自动重试。审批后手动恢复是后续操作，不延长原调度。

默认串行。需要并发时设置 `concurrency` 和 `parallel_target_safe: true`；这个
字段表示调用方确认 Target 允许并发，**不会创建 Target 的隔离环境**。子 Run
隔离的是 Planner 和测试执行上下文；有共享外部状态的 Target 应保持串行，或由
Target 适配层实现隔离。调度器创建至多 `concurrency` 个进程内执行者，每个执行者
完成当前 Run 后领取下一个待执行任务；并发名额空出后不会等待同批其他 Run 完成。

跨进程额度由同一业务数据库的 `concurrency_quotas` / `concurrency_leases` 管理。
每个 worker 在开始前同时领取全局、部署 realm、Target 三类名额；使用模型 Planner
时还领取模型接口名额。名额覆盖整个子 Run，即使其中暂时没有调用 Target 或模型。
Orchestrator 规划和总结也占用模型接口名额。默认额度通过
`ORCHESTRATOR_CONCURRENCY__GLOBAL_WORKERS=16`、
`ORCHESTRATOR_CONCURRENCY__REALM_WORKERS=16`、
`ORCHESTRATOR_CONCURRENCY__TARGET_WORKERS=1`、
`ORCHESTRATOR_CONCURRENCY__MODEL_WORKERS=4` 配置。
Target 默认串行；只有管理员确认共享 Target 可并发后才提高服务端 Target 额度。
当前 API Key 是部署级门禁，没有用户/租户身份，`realm:default` 仅表示该部署的
统一额度，不能宣称已经实现独立租户配额。所有进程必须使用同一数据库和相同额度；
已有资源的额度不一致时直接拒绝领取，避免配置漂移绕过限制。

名额定期续租，失租后取消当前 Run；正常结束按持有者释放。进程崩溃后名额在租约
到期时可被重新领取，但这**不会自动恢复或重放**中断的子 Run。额度是同时执行数，
不是每秒请求数或 Token 速率；模型与 Target 的单次调用仍使用各自超时。
现有 Durable Job Worker 不调度多 Agent 请求；共享额度对调用 `SubagentService`
的 API、CLI 等进程生效。修改额度前须先排空任务，再按生产运维手册清理旧额度行。

## 主报告和恢复边界

`summary` 包括 worker 状态、实际调用量、执行结果分布及证据缺口。模型模式额外返回
`workers` 列表和 `model_summary`（总结文本、引用的 Finding 指纹及模型用量）；
原有 `subagents` 字段继续提供兼容。模型总结失败时保留
确定性汇总，并在 `errors.orchestrator_summary` 记录错误类别。审批或恢复使证据变化后，
旧模型总结会标记 `model_summary_status: stale`，不再作为当前总结返回；确定性汇总
始终重新读取最新 SQL 事实。
`findings` 按已有稳定指纹分组，每条来源保留 agent_id、run_id、finding_id、
evidence_event_ids 和证据链接完整性；不同 outcome 会标记冲突，不以投票覆盖。
完整性只表示引用的事件存在，不是对证据语义充分性的额外判定。详细原因、轨迹
和控制结果从各子报告读取。`completed` 表示执行完成，不代表全部安全测试通过。

一个 Subagent 失败不会取消其他 Subagent；失败类型会保留，异常原文和凭据不写入
主任务。等待审批/Planner 恢复显示 `waiting`，其他未完整执行显示 `partial`。
审批与恢复后重新 GET 主报告，无须重跑已完成子任务。

主任务关系在开始 Target 执行前保存。若请求中断，可通过 list 找到子 Run 并检查。
本版主调度是请求内固定数量的执行者和内存任务队列，没有子任务的持久队列或自动
重启调度；进程崩溃后可能显示 `running_or_interrupted` /
`pending_or_interrupted`，应核对子 Run 事实，
不会把它们标成已完成，也不会自动重放未知执行状态的任务。
