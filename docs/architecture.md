# Attacker Architecture

> 本文描述 Attacker v1 的目标架构。当前代码仍是 FastAPI + DuckDB/Parquet 原型；本文
> 不代表所有能力已经实现。

## 1. 架构目标

Attacker 在明确授权的测试环境中评测 AI Agent，并输出可解释、可追踪、可回放的风险
证据。系统同时支持：

- **Deterministic Mode**：固定 Dataset 和执行顺序，不调用 LLM；
- **Adaptive Mode**：由 LangGraph 编排，使用 LLM 规划下一条已批准 Case；
- **Human Review**：高风险步骤执行前暂停并等待批准；
- **Recovery**：进程中断后从 checkpoint 恢复工作流；
- **Replay**：使用历史快照重新评测并比较 Finding。
- **Black-box Evaluation**：仅通过标准 Request/Response 测试输入和输出边界；
- **Gray-box Evaluation**：通过脱敏 Tool/Policy Trace 验证授权和副作用；
- **Stateful Evaluation**：通过测试专用 Memory/RAG 接口验证持久污染和隔离。

```text
+-------------------------------------------------------+
| API / Application                                    |
| FastAPI / RunService / ApprovalService / ReportService|
+---------------------------+---------------------------+
                            |
              +-------------+-------------+
              |                           |
              v                           v
+---------------------------+  +------------------------+
| Deterministic Runner      |  | LangGraph Workflow     |
| fixed case order          |  | plan / route / resume  |
+-------------+-------------+  +-----------+------------+
              |                            |
              +-------------+--------------+
                            v
+-------------------------------------------------------+
| Deterministic Domain Core                             |
| Policy / Connector / Evaluator / Evidence / Finding  |
+---------------------------+---------------------------+
                            |
              +-------------+-------------+
              |                           |
              v                           v
+---------------------------+  +------------------------+
| SQLite Business Facts     |  | Graph Checkpoint       |
| Run/Event/Finding/Replay  |  | control-flow recovery  |
+---------------------------+  +------------------------+
```

## 2. 设计原则

### 2.1 Deterministic core, agentic orchestration

Policy、Target 调用、Evaluator、Evidence、Finding 和报告都是确定性核心。LLM 只在
Adaptive Mode 的 Planner Node 中提出下一步。

### 2.2 Policy before execution

Planner 输出不是授权。每次 Target 调用前必须重新校验：

- Target 是否在授权范围；
- Case 是否属于当前 Dataset 和 allowlist；
- 风险等级是否需要审批；
- 运行时间、步骤和 Target 调用预算是否充足。

### 2.3 Evidence before claims

Finding 必须引用已保存的 Evidence Event。报告不能根据临时 Graph State 或日志推断
安全结论。

### 2.4 Bounded autonomy

Adaptive Mode 只能：

- 从批准 Case 中选择下一条；
- 给出选择理由；
- 根据已脱敏 Finding 摘要调整顺序；
- 在预算内继续或结束。

它不能创建任意攻击、修改 Target、绕过审批或覆盖 Judge。

### 2.5 Two stores, two responsibilities

- SQLite 保存产品和审计事实；
- LangGraph Checkpoint 保存控制流恢复状态。

两者不得互相替代。

## 3. 运行模式

### 3.1 Deterministic Mode

```text
create run
  -> load dataset snapshot
  -> iterate cases
  -> policy gate
  -> execute target
  -> evaluate
  -> persist event/finding
  -> generate report
```

用途：

- 回归评测；
- Judge 校准；
- 演示可复现结果；
- 为 Adaptive Mode 提供比较基线。

Deterministic Mode 不经过 LangGraph，避免把普通批处理包装成无意义的状态图。

### 3.2 Adaptive Mode

```text
START
  |
  v
initialize_run
  |
  v
plan_next_case <---------------------------+
  |                                        |
  v                                        |
policy_gate                                |
  |                                        |
  +-- denied ----------> record_skip -------+
  |                                        |
  +-- approval_required -> human_review     |
  |                         |               |
  |                         +-- rejected ---+
  |                         |
  |                         +-- approved
  v
execute_target
  |
  v
evaluate_result
  |
  v
persist_evidence
  |
  v
decide_next
  |
  +-- continue -----------------------------+
  |
  +-- finish -> generate_report -> END
```

### 3.3 三阶段安全评测

运行模式描述“如何调度 Case”，三个安全阶段描述“可以观测目标 Agent 的哪些边界”。
两者是正交关系：

| 阶段 | Target 契约 | 主要证据 | 安全能力 |
|---|---|---|---|
| 纯黑盒 | Request/Response | 请求、响应、Evaluator、预算 | 注入、泄露、多轮上下文污染、资源消耗 |
| 灰盒 Agent | Tool/Policy/Approval Trace | 工具、参数、身份、授权和副作用 | 工具越权、参数越权、审批绕过、工具输出污染、循环 |
| 带状态 Agent | Memory/RAG/Checkpoint | Memory 快照、Retrieval Trace、隔离标识、恢复事件 | Memory/RAG 污染、跨用户污染、恢复安全、Replay |

三个阶段全部属于最终架构。第一阶段主要使用 Deterministic Mode；第二阶段引入完整
LangGraph Adaptive Workflow；第三阶段在同一工作流上增加状态、知识和恢复观测能力。

#### 纯黑盒边界

- 不要求访问目标内部实现；
- 不根据猜测宣称工具越权；
- 使用 Canary 检测泄露；
- 使用多轮 Session 检测短期上下文污染；
- 使用运行预算检测资源消耗。

#### 灰盒边界

- Target 通过 Adapter 提供脱敏 `ToolEvent`、`PolicyEvent` 和 `ApprovalEvent`；
- Tool Trace 必须包含调用身份、参数摘要、Policy Decision 和实际副作用；
- 测试使用 Mock Tool 或沙箱资源；
- 最终文本和 Tool Trace 共同构成 Finding Evidence。

#### 带状态边界

- Memory 和 RAG 只能连接隔离测试数据；
- Retrieval Trace 包含文档 ID、排名、来源和权限过滤结果；
- Session、User 和 Tenant ID 必须进入隔离校验；
- Checkpoint 恢复时重新校验 Policy；
- 测试结束后清理污染数据并记录清理结果。

## 4. Graph State

Graph State 是控制流投影，不是完整业务对象：

```python
class AttackGraphState(TypedDict):
    run_id: str
    target_id: str
    dataset_version: str
    policy_version: str
    allowed_case_ids: list[str]
    completed_case_ids: list[str]
    current_case_id: str | None
    finding_summaries: list[dict]
    target_call_count: int
    remaining_steps: int
    next_action: str
    event_sequence: int
    status: str
```

约束：

- 使用 ID 引用数据库对象；
- 不保存 Target 凭据；
- 不保存未经脱敏的完整响应；
- Finding 只传递 Planner 所需摘要；
- `remaining_steps` 只是控制流缓存，真实预算以数据库和 Policy 为准；
- 恢复时先读取业务数据库，再校验 checkpoint。

## 5. 节点职责

| 节点 | 是否调用 LLM | 输入 | 输出 |
|---|---:|---|---|
| `initialize_run` | 否 | `run_id` | 快照、预算、已完成 Case |
| `plan_next_case` | 是 | allowlist、脱敏摘要 | `PlannerDecision` |
| `policy_gate` | 否 | Case、Target、Policy、预算 | allow/deny/approval |
| `human_review` | 否 | Approval Request | interrupt/resume |
| `execute_target` | 否 | 已批准 Case | Target Response Event |
| `evaluate_result` | 否 | Case、响应引用 | Evaluator Results |
| `persist_evidence` | 否 | Evaluator Results | Event/Finding 引用 |
| `record_skip` | 否 | Policy/审批结果 | Skip Event |
| `decide_next` | 否 | 预算、状态、停止规则 | continue/finish |
| `generate_report` | 否 | `run_id` | Markdown/JSON 报告 |

Node 不直接包含复杂 SQL、HTTP 模板或 Judge 规则；这些逻辑属于领域服务和 Adapter。

## 6. Planner 契约

Planner 使用窄输入：

```python
class PlannerContext(BaseModel):
    allowed_cases: list[CaseSummary]
    completed_case_ids: list[str]
    finding_summaries: list[FindingSummary]
    remaining_steps: int


class PlannerDecision(BaseModel):
    action: Literal["execute_case", "finish_run"]
    case_id: str | None
    reason: str
```

验证规则：

- `execute_case` 必须提供 `case_id`；
- `case_id` 必须存在于 allowlist 且尚未完成；
- `finish_run` 不允许携带 `case_id`；
- 非法或无法解析的输出生成 `planner_rejected` Event；
- 连续失败达到上限后，Run 进入 `failed`，不回退到任意工具调用。

## 7. Policy 与预算

```python
class AttackPolicy(BaseModel):
    allowed_target_ids: set[str]
    allowed_case_ids: set[str]
    max_steps: int
    max_target_calls: int
    max_duration_seconds: int
    approval_required_severities: set[RiskLevel]
    stop_on_critical: bool
```

预算分为：

- Graph step 上限；
- Planner 模型请求上限；
- Target 调用上限；
- 总持续时间；
- 单次 Target 超时；
- Case 重试上限。

Policy Gate 在 Target 调用前执行最终校验。Graph 条件边只负责路由，不能替代安全校验。

## 8. Human Review

高风险 Case 触发 `interrupt`，创建 Approval Request：

```text
approval_id
run_id
case_id
requested_at
risk_summary
status: pending | approved | rejected | expired
resolved_at
resolved_by
reason
```

恢复规则：

1. API 使用 `run_id` 和 `approval_id` 提交决定；
2. ApprovalService 校验请求仍为 pending；
3. 决定以 Event 写入业务数据库；
4. 通过相同 LangGraph `thread_id` 恢复；
5. `policy_gate` 再次确认 Target、Case 和预算；
6. 只有批准且仍满足 Policy 时才能执行。

审批不是永久授权，不能复用于其他 Run 或 Case。

## 9. Checkpoint 与业务事实

### 9.1 Checkpoint

Checkpoint 保存：

- `thread_id`；
- 当前节点；
- Graph State；
- 条件边；
- interrupt；
- 恢复所需元数据。

其用途是“从哪里继续执行”。

### 9.2 SQLite

SQLite 保存：

- Target 和授权快照；
- Dataset、Case、Policy、Evaluator 版本；
- Run 和 Step；
- Event 和 Evidence；
- Finding；
- Approval Decision；
- Tool/Policy/Approval Trace；
- Memory Snapshot 和 Retrieval Event；
- Session、User 和 Tenant 隔离标识；
- Replay；
- 报告索引。

其用途是“实际发生了什么”。

### 9.3 一致性

- 每个有副作用节点生成稳定 `operation_id`；
- Repository 对 `operation_id` 建唯一约束；
- 节点重试不会重复调用 Target 或重复创建 Finding；
- Tool 调用和 Memory 写入同样使用稳定 `operation_id`；
- Event 使用 Run 内单调 `sequence`；
- 完成领域事务后才推进 checkpoint；
- checkpoint 与数据库冲突时，以数据库最后提交事件为准；
- 报告完全从数据库重建。

## 10. Evaluator Pipeline

```text
Target Response
  -> transport validation
  -> deterministic rules
  -> structured response checks
  -> optional model judge
  -> finding aggregator
```

不同阶段要求不同 Evidence：

| 阶段 | Finding 最低 Evidence |
|---|---|
| 纯黑盒 | Target Request、Target Response、Evaluator Result |
| 灰盒 Agent | 黑盒 Evidence + Tool/Policy/Approval Trace |
| 带状态 Agent | 灰盒 Evidence + Memory/Retrieval/Checkpoint Event |

Evidence 不足时结果标记为 `inconclusive`，不能根据最终文本推断内部工具、权限或
Memory 行为。

优先级：

1. 连接错误和超时单独记录；
2. 确定性规则优先；
3. 结构化响应检查其次；
4. 只有模糊语义场景才使用 Model Judge；
5. Model Judge 输出结构化结果和理由；
6. Finding 保存实际命中的 Evidence 引用。

Planner 和 Model Judge 使用不同 Adapter 配置和调用统计，避免职责混淆。

## 11. Connector 契约

```python
class TargetConnector(Protocol):
    async def execute(
        self,
        *,
        target: TargetSnapshot,
        case: AttackCase,
        operation_id: str,
    ) -> TargetExecutionResult:
        ...
```

Connector 负责：

- 按 Target 模板构造请求；
- 在最后时刻注入凭据；
- 超时和响应大小限制；
- Redirect 目标复核；
- 解析 JSON/text；
- 返回统一结果。

Connector 不负责 Case 选择、Policy 决策、Finding 判定和报告。

## 12. 安全模型

### 12.1 Target allowlist

- Target 必须显式配置；
- 默认面向本地、测试和沙箱；
- 不扫描未知资产；
- 不从目标响应发现新 endpoint。

### 12.2 Action allowlist

- Planner 只能选择已批准 Case；
- 不提供 Shell、浏览器、文件系统和通用 HTTP；
- 不允许 Planner 直接生成并执行任意 prompt；
- 高风险 Case 必须审批。

### 12.3 Secret separation

- 凭据不进入 Graph State；
- 凭据不发送给 Planner；
- 日志、Evidence 和报告写入前脱敏；
- checkpoint 不保存 Headers 和 Token；
- tracing 默认只记录 ID、状态和用量。

## 13. Bootstrap 与 Shutdown

启动顺序：

1. 读取并校验配置；
2. 初始化日志与脱敏；
3. 创建 SQLAlchemy Engine；
4. 校验 Schema 版本；
5. 创建 Repository；
6. 创建 Target Connector；
7. 创建 Planner Model Adapter；
8. 创建 LangGraph checkpointer；
9. 编译状态图；
10. 注册 FastAPI 路由。

关闭顺序：

1. 停止接收新 Run；
2. 标记或等待正在执行的节点；
3. flush 领域事件；
4. 关闭 Model/HTTP Client；
5. 关闭数据库与 checkpointer。

模块导入阶段不得连接模型、数据库或外部服务。

## 14. 与 AtlasClaw 的取舍

### 借鉴

- 类型化依赖和明确的运行上下文；
- 外层 Policy、预算、中止与 Evidence；
- async-first；
- 模型、Connector 和 Repository Adapter；
- 启动与关闭生命周期；
- 结构化事件和敏感数据边界。

### 针对 Attacker 的调整

- 使用 LangGraph 表达明确的评测状态机；
- 仅 Planner Node 使用模型；
- 将审批和恢复作为安全评测的一等能力；
- Checkpoint 与审计事实严格分离。

### 不复制

- 公共 Providers/Skills 市场或从未知 URL 自助安装代码；
- 通用 Agent Skills、Hooks、Channels；
- 通用 Memory；
- 多租户与 Token Pool；
- 任意工具发现；
- 与 v1 无关的基础设施。

Attacker vNext 仅增加面向安全评测的受控装备契约：Core 拥有 Capability Contract、
Policy Gate、预算、审批、Evidence/Finding、持久化快照、Replay 和清理边界；部署方可从
本地目录或离线包提供 Provider/Skill/Case Pack。企业代码不能通过装备扩展绕过 Core，
`trusted_enterprise` 子进程只提供故障隔离，`untrusted` 代码必须在平台支持的强沙箱中
运行。详细边界见 [Equipment Development](equipment-development.md)。

## 15. 架构验收

- 纯黑盒、灰盒 Agent 和带状态 Agent 三个阶段全部通过；
- 不少于 30 条 Case，其中三个阶段分别不少于 12、10、8 条；
- Deterministic Mode 不依赖 LLM 和 LangGraph；
- Adaptive Mode 的所有路径都经过 Policy Gate；
- Planner 无法选择 allowlist 外的 Case；
- 高风险 Case 未批准时不能执行；
- 进程中断后能通过相同 `thread_id` 恢复；
- 节点重试不重复调用 Target/Tool、写入 Memory 或创建 Finding；
- 每个 Finding 都能追溯到 Event 和 Evidence；
- 灰盒 Finding 包含 Tool/Policy Trace，不根据最终文本推断工具越权；
- Memory/RAG 污染可以被隔离、测量和清理；
- 跨用户和跨租户污染率为 0；
- 报告能完全从 SQLite 重建；
- checkpoint 丢失不会导致业务证据丢失；
- 模型上下文、日志和 checkpoint 不包含 Target 凭据；
- Adaptive 与 Deterministic 使用相同 Dataset 和 Evaluator 比较。
