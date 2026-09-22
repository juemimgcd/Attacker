# 主 Agent 与 Subagent 测试

主 Agent 接收显式测试分工，先校验范围和前置 Case，再分配预算并启动多个独立的
Adaptive Run。每个 Subagent 运行自己的 Planner → Policy Gate → Execute → Evaluate
循环，拥有独立 run_id、thread_id、候选、假设和 SQL Session。主 Agent 最后读取 SQL
中的结果和 Evidence，生成结构化汇总。现有单 Run API 保持可用。

本版的主 Agent 是确定性协调器，分工由调用方指定，汇总不调用 LLM；每个 Subagent
可使用现有 `deterministic` 或 `openai_compatible` Planner，也可在分工中单独覆盖
`planner` 配置。没有自动生成攻击任务、递归委派或根据汇总再次追加测试。

## 使用

生产数据库先按项目既有流程执行 `uv run attacker migrate`，升级到
`20260912_0011`。以下配置可保存为 `subagents.json`，Target 地址应替换为已授权的
灰盒 Target。示例中的两个 Subagent 分别负责工具授权和参数边界测试，各自包含
对应的安全对照 Case。

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
      "max_provider_calls": 20,
      "max_duration_seconds": 300
    },
    "planner": {"backend": "deterministic"}
  },
  "concurrency": 1,
  "subagents": [
    {
      "agent_id": "tool-authorization",
      "case_ids": ["gb_unauthorized_tool_attack", "gb_unauthorized_tool_control"]
    },
    {
      "agent_id": "parameter-boundary",
      "case_ids": ["gb_dangerous_parameter_attack", "gb_dangerous_parameter_control"]
    }
  ]
}
```

```sh
uv run attacker subagents run --config subagents.json
uv run attacker subagents list
uv run attacker subagents report <coordinator_id>
```

HTTP 接口（沿用部署的 `X-API-Key` 门禁）：

- `POST /runs/subagents`：接收上述 JSON，等待本轮委派收敛后返回主报告。
- `GET /runs/subagents?limit=20`：查看最近的主任务与预分配子 Run ID。
- `GET /runs/subagents/{coordinator_id}`：从子 Run 的最新 SQL 事实重建主报告。
- 子 Run 继续使用 `/runs/{run_id}`、`/runs/{run_id}/control`、
  `/runs/{run_id}/resume` 和现有审批接口。

## 范围、预算与并发

每组最多 16 个 Subagent；所有 Case 必须位于父请求的 dataset/case_ids 和 Policy
允许范围内。子任务必须自行包含前置 Case；建议将攻击及相关 control 一起委派。
同一个 Case 可以分配给不同 Subagent 做独立复核，调用消耗和执行数会分别计入，
不会把重复执行数量描述为唯一 Case 覆盖率。分工列表定义本组实际评测范围。

`max_steps`、`max_target_calls`、`max_provider_calls` 的整组预算按 Subagent 顺序
均分，余数分给靠前的 Subagent。`max_cost` 向下取整均分，避免超过父预算。
所有其他授权和停止条件继承父 Policy。分配出去的闲置额度不会自动转给其他
Subagent，子 Run 恢复后仍使用其原预算。`stop_on_critical` 作用于各子 Run。

`max_duration_seconds` 同时限制本次主调度（包含排队）和各子 Run；主调度超时
会取消在途任务，保留已落库证据并返回 partial 报告。取消可能无法撤回 Target
已经接收的请求，协调器不会自动重试。审批后手动恢复是后续操作，不延长原调度。

默认串行。需要并发时设置 `concurrency` 和 `parallel_target_safe: true`；这个
字段表示调用方确认 Target 允许并发，**不会创建 Target 的隔离环境**。子 Run
隔离的是 Planner 和测试执行上下文；有共享外部状态的 Target 应保持串行，或由
Target 适配层实现隔离。

## 主报告和恢复边界

`summary` 包括子 Agent 状态、实际调用量、执行结果分布及证据缺口。
`findings` 按已有稳定指纹分组，每条来源保留 agent_id、run_id、finding_id、
evidence_event_ids 和证据链接完整性；不同 outcome 会标记冲突，不以投票覆盖。
完整性只表示引用的事件存在，不是对证据语义充分性的额外判定。详细原因、轨迹
和控制结果从各子报告读取。`completed` 表示执行完成，不代表全部安全测试通过。

一个 Subagent 失败不会取消其他 Subagent；失败类型会保留，异常原文和凭据不写入
主任务。等待审批/Planner 恢复显示 `waiting`，其他未完整执行显示 `partial`。
审批与恢复后重新 GET 主报告，无须重跑已完成子任务。

主任务关系在开始外部执行前保存。若请求中断，可通过 list 找到子 Run 并检查。
本版主调度是请求内 TaskGroup，没有独立 Worker 租约或自动重启调度；进程崩溃后
可能显示 `running_or_interrupted` / `pending_or_interrupted`，应核对子 Run 事实，
不会把它们标成已完成，也不会自动重放未知执行状态的任务。
