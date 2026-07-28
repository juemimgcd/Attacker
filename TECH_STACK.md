# Attacker 技术选型与架构决策

> 文档状态：目标架构
> 适用范围：Attacker v1
> 决策目标：形成一个可完成、可解释、可恢复的 Agent 安全评测项目，而不是展示技术名词集合。

## 1. 最终选型

| 领域 | 技术 | 职责 |
|---|---|---|
| 语言 | Python 3.12 | 兼容主流 Agent、Web 与数据库生态 |
| API | FastAPI、Uvicorn | 控制面、运行接口、健康检查 |
| Schema 与配置 | Pydantic、pydantic-settings | API、领域对象、配置校验 |
| Agent 编排 | LangGraph | Adaptive Workflow、条件路由、checkpoint、interrupt |
| 模型接入 | 独立 Model Adapter | 仅供 Planner Node 生成结构化决策 |
| Eval Dataset | Pydantic Evals、YAML | Dataset、Case、Evaluator、Experiment |
| HTTP | httpx | Target Connector |
| ORM | SQLAlchemy Async | 领域 Repository 和事务 |
| 主数据库 | SQLite | Target、Run、Step、Event、Finding、Replay |
| 数据迁移 | Alembic | Schema 版本管理 |
| 报告 | Markdown、JSON | 人工审阅与机器处理 |
| 工程质量 | Ruff、Pyright、pytest | 格式、静态检查和行为验证 |

v1 不同时引入：

- PydanticAI；
- OpenAI Agents SDK；
- LangChain Chains、Agents 和 Memory 抽象；
- Redis 和任务队列；
- PostgreSQL；
- MinIO、Qdrant；
- DuckDB/Parquet 在线主存储；
- Web 前端和 Kubernetes。

## 2. 为什么选择 LangGraph

Attacker 的 Adaptive Mode 不是一次普通的“模型调用工具”循环，而是具有明确业务状态的
安全评测工作流：

```text
initialize
  -> plan_next_case
  -> policy_gate
       -> denied ------------> record_skip
       -> approval_required -> human_review
       -> allowed -----------> execute_target
  -> evaluate_result
  -> persist_evidence
  -> decide_next
       -> continue ----------> plan_next_case
       -> finish ------------> generate_report
```

该流程真实需要：

1. 显式且类型化的运行状态；
2. 条件分支与有界循环；
3. Target 超时或服务重启后的恢复；
4. 高风险步骤执行前的人工审批；
5. 节点级事件和运行轨迹；
6. Deterministic Mode 与 Adaptive Mode 共用同一领域核心。

这些需求与 LangGraph 的 StateGraph、checkpoint、interrupt 和 streaming 能力直接对应。
LangGraph 只承担 Adaptive Workflow 编排，不承担授权、Judge、Evidence、报告和业务事实
持久化。

## 3. 为什么不再选择 PydanticAI

PydanticAI 适合工具调用 Agent，AtlasClaw 也使用它作为模型与工具运行时。但 Attacker 和
AtlasClaw 的问题形态不同：

| 项目 | 主要问题 |
|---|---|
| AtlasClaw | 通用企业 Agent Runtime、Providers、Skills、Hooks 与多种交互入口 |
| Attacker | 固定生命周期的安全评测工作流、条件路由、审批、恢复与 Replay |

如果同时使用 LangGraph 和 PydanticAI，将出现重复编排：

```text
LangGraph 选择节点
  -> PydanticAI 决定工具
     -> Attacker Service 再维护领域状态
```

这会增加：

- 两套 Agent Runtime；
- 两套消息与工具抽象；
- 重复的状态转换；
- 更难解释的错误边界；
- 更复杂的 tracing 和敏感数据治理。

因此 v1 选择 LangGraph，删除 PydanticAI。模型能力通过一个窄接口注入 Planner Node，
其余节点保持确定性。

## 4. Pydantic Evals 的职责

攻击用例不是聊天历史，也不是 Agent Memory。它们是可版本化的评测数据：

```text
Dataset
  -> Case
      -> Input
      -> Expected outcome
      -> Metadata
      -> Evaluators
```

Pydantic Evals 用于：

- Dataset/Case 契约；
- YAML 数据集序列化；
- Evaluator 组合；
- 确定性基线实验；
- Judge 校准；
- Adaptive Mode 与固定 Dataset 的效果比较。

它不保存产品运行状态，也不替代 Attacker 的 Run、Event、Finding 和 Replay 模型。

## 5. LangGraph 与领域核心的边界

### 5.1 Graph 节点

v1 状态图包含：

| 节点 | 类型 | 职责 |
|---|---|---|
| `initialize_run` | 确定性 | 加载快照、预算和已完成 Case |
| `plan_next_case` | LLM | 从允许集合中提出下一条 Case 和理由 |
| `policy_gate` | 确定性 | 校验 Target、Case、风险级别和预算 |
| `human_review` | interrupt | 等待高风险动作批准或拒绝 |
| `execute_target` | 确定性 | 通过 Connector 调用目标 |
| `evaluate_result` | 确定性 | 执行 Evaluator Pipeline |
| `persist_evidence` | 确定性 | 原子保存 Event 和 Finding |
| `record_skip` | 确定性 | 保存拒绝原因 |
| `decide_next` | 确定性 | 判断继续、停止或失败 |
| `generate_report` | 确定性 | 从数据库事实生成报告 |

只有 `plan_next_case` 可以调用模型。

### 5.2 Planner 输出

Planner 必须返回结构化决策：

```python
class PlannerDecision(BaseModel):
    action: Literal["execute_case", "finish_run"]
    case_id: str | None
    reason: str
```

`case_id` 必须来自 `allowed_case_ids`。Planner 不接收 Target 凭据，不生成任意 URL、
任意 prompt 或任意工具名称。

### 5.3 Graph State

Graph State 只保存控制流需要的紧凑状态：

```python
class AttackGraphState(TypedDict):
    run_id: str
    target_id: str
    allowed_case_ids: list[str]
    completed_case_ids: list[str]
    current_case_id: str | None
    finding_summaries: list[dict]
    target_call_count: int
    remaining_steps: int
    next_action: str
    status: str
```

完整 Target 响应、凭据和证据正文不进入 Planner 上下文；Graph State 只保存引用和脱敏
摘要。

## 6. 两种运行模式

### 6.1 Deterministic Mode

```text
Dataset -> Case Iterator -> Policy -> Connector -> Evaluator
        -> Event/Finding -> Report
```

特点：

- 不调用 LLM；
- 不经过 LangGraph；
- 执行顺序固定；
- 可复现；
- 是 Judge 校准、回归评测和 Adaptive Mode 比较基线。

### 6.2 Adaptive Mode

```text
Dataset + Policy + prior Findings
  -> LangGraph Planner
  -> Policy Gate
  -> Target Connector
  -> Evaluator
  -> Event/Finding
  -> conditional next step
```

Adaptive Mode 只能改变批准 Case 的选择顺序和停止时机，不能：

- 创建未批准 Case；
- 修改 Target；
- 扩大网络范围；
- 绕过 Policy Gate；
- 修改 Judge 结果；
- 直接写数据库；
- 直接生成最终审计结论。

## 7. 双重状态设计

### 7.1 LangGraph Checkpoint

Checkpoint 负责：

- 当前节点；
- 节点输入输出；
- 条件边结果；
- interrupt 状态；
- 工作流恢复。

Checkpoint 是可丢弃、可重建的执行数据，不是安全报告的证据来源。

### 7.2 Attacker 业务数据库

SQLite + SQLAlchemy 保存：

- Target 和授权快照；
- Dataset/Case 版本；
- Run 与 Step；
- Evidence Event；
- Finding；
- Replay；
- Approval Decision；
- Tool/Policy Trace；
- Memory/RAG 测试快照和 Retrieval Event；
- 报告索引。

数据库是审计事实源。报告只从数据库生成，不直接读取 LangGraph State。

### 7.3 一致性规则

1. 先持久化领域事件，再允许状态图进入下一业务步骤；
2. 每个节点使用稳定 `operation_id`，重试时幂等；
3. Checkpoint 引用 `run_id` 和 `event_sequence`，不复制完整 Evidence；
4. 恢复时先读取业务数据库，再校验 checkpoint；
5. 两者冲突时以业务数据库为准，并从最后一个已提交事件恢复。

## 8. 数据库决策

Attacker 的核心数据是事务状态，而不是离线分析表：

```text
Target
  -> Run
      -> Step
          -> Event
              -> Finding
                  -> Replay
```

SQLite 满足 v1 的本地开发和单机演示：

- 零额外服务；
- 支持事务和外键；
- 易于携带和复现；
- 可通过 SQLAlchemy 保留迁移 PostgreSQL 的能力。

DuckDB 和 Parquet 适合未来批量离线分析，不再承担在线 Run 状态或每事件证据写入。

出现多实例 Worker、远程共享数据库或 SQLite 写锁成为可观测瓶颈时，再迁移 PostgreSQL。

## 9. 模块边界

目标模块结构：

```text
app/
  api/
    runs.py
    approvals.py
    reports.py
  domain/
    targets.py
    datasets.py
    runs.py
    events.py
    findings.py
    policies.py
  workflows/
    attack_state.py
    attack_graph.py
    routes.py
    nodes/
      initialize.py
      plan.py
      authorize.py
      execute.py
      evaluate.py
      persist.py
      report.py
  services/
    run_service.py
    policy_service.py
    evaluator_service.py
    replay_service.py
    report_service.py
  connectors/
    base.py
    http_json.py
  repositories/
    run_repository.py
    event_repository.py
    finding_repository.py
  infrastructure/
    database.py
    checkpoint.py
    model_adapter.py
```

依赖方向：

```text
api -> application services -> workflows/domain
workflows -> domain services
repositories/connectors/model adapter -> domain ports
domain -> Pydantic and Python only
```

Node 保持薄，只负责 Graph State 与领域服务之间的转换。

## 10. 安全约束

### Target Policy

- Target 必须由用户显式创建；
- 只允许配置的 endpoint；
- 默认拒绝公网和未知资产；
- Redirect 后重新校验目的地址；
- 目标响应不能扩展测试范围。

### Action Policy

- Planner 只能返回 `execute_case` 或 `finish_run`；
- Case 必须来自允许集合；
- 每次 Target 调用前重新校验预算；
- 高风险 Case 必须进入 interrupt；
- 不提供 Shell、浏览器、文件系统或通用 HTTP 工具。

### Secret Policy

- 凭据由 Connector 在调用时注入；
- Planner、Graph State、日志和报告不持有明文凭据；
- Evidence 写入前执行字段级脱敏；
- tracing 默认不记录完整目标输入输出。

## 11. 可观测性

日志至少包含：

- `run_id`；
- `thread_id`；
- `node_name`；
- `case_id`；
- `operation_id`；
- `event_sequence`；
- `duration_ms`；
- `outcome`。

领域事件至少包含：

- `run_started`；
- `planner_decided`；
- `policy_allowed` / `policy_denied`；
- `approval_requested` / `approval_resolved`；
- `target_called`；
- `tool_requested` / `tool_completed`；
- `memory_written` / `memory_recalled`；
- `rag_retrieved`；
- `evaluation_completed`；
- `finding_created`；
- `run_completed` / `run_failed` / `run_aborted`。

模型 token 统计和 Target 调用统计分开记录，避免把模型成本与被测接口流量混为一谈。

## 12. 三阶段交付顺序

三个阶段按目标 Agent 的可观测深度递进，并全部属于最终交付范围。

| 阶段 | 接入契约 | 核心安全面 | 最小 Case |
|---|---|---|---:|
| 纯黑盒 | HTTP Request/Response | 注入、泄露、上下文污染、资源消耗 | 12 |
| 灰盒 Agent | Tool/Policy/Approval Trace | 工具越权、参数越权、审批、循环 | 10 |
| 带状态 Agent | Memory/RAG/Checkpoint | 持久污染、隔离、恢复、Replay | 8 |

### 第一阶段：纯黑盒

- 将目标版本调整为 Python 3.12；
- 引入 SQLAlchemy Async、SQLite 和 Alembic；
- 建立 Target、Run、Step、Event、Finding 表；
- 将 YAML 样本映射为 Pydantic Evals Dataset/Case；
- 实现 Direct Prompt Injection、System Prompt Leakage、Sensitive Data Canary、多轮上下文
  污染和资源消耗 Case；
- 完成批量 Deterministic Run、Evaluator Pipeline 和 Markdown/JSON 报告；
- 将 DuckDB/Parquet 在线路径迁移到 Repository。

第一阶段只要求标准 Target Request/Response，但必须保存请求、响应、Evaluator 和预算
Evidence，并提供正常任务对照。

### 第二阶段：灰盒 Agent

- 定义脱敏 `ToolEvent`、`PolicyEvent` 和 `ApprovalEvent`；
- 定义 Tool Trace Adapter，隔离不同目标 Agent 的 trace 格式；
- 实现 LangGraph StateGraph、Planner Model Adapter 和条件路由；
- 实现 Policy Gate、interrupt、Approval 和稳定 `operation_id`；
- 覆盖未授权工具、危险参数、审批绕过、Tool Output Injection 和 Planner 循环；
- 使用 Mock Tool 或沙箱 Target 验证副作用，不接触生产资源。

第二阶段的 Finding 必须同时引用模型响应和 Tool/Policy Trace。只看到最终文本时，不宣称
已经证明工具越权。

### 第三阶段：带状态 Agent

- 定义测试专用 Memory Adapter 和 RAG Adapter；
- 保存 Session、User、Tenant、Memory、Dataset、Policy 和 Evaluator 快照；
- 记录 Retrieval Document、排名、来源和权限过滤结果；
- 接入 LangGraph checkpoint 和稳定 `thread_id`；
- 实现 Finding fingerprint、source/replay Run 和修复差异；
- 覆盖 Memory Poisoning、RAG Poisoning、跨用户污染、恢复安全和 Replay；
- 提供污染数据清理与隔离。

第三阶段要求 checkpoint 恢复后重新校验 Policy，并保证不会重复 Target/Tool 调用或
Finding。Memory/RAG 测试必须使用隔离测试数据。

## 13. 组件引入条件

| 组件 | 真实条件 |
|---|---|
| PostgreSQL | 多实例 Worker、远程共享或 SQLite 写锁成为瓶颈 |
| Redis/队列 | API 与 Worker 分离，需要后台运行和任务调度 |
| Qdrant | Case 数量达到语义检索确实优于分类筛选的规模 |
| MinIO | Evidence 体积超出数据库和本地文件管理能力 |
| DuckDB/Parquet | 需要跨大量 Run 的批量离线统计与导出 |
| Web 前端 | API 和报告闭环稳定，且真实需要审批操作台 |

PydanticAI 和 OpenAI Agents SDK 不属于后续扩容组件；除非移除 LangGraph，否则不再引入
第二套 Agent Runtime。

## 14. 结论

Attacker v1 的技术主线是：

```text
Python 3.12
FastAPI + Pydantic
LangGraph Adaptive Workflow
Pydantic Evals + YAML
SQLAlchemy Async + SQLite + Alembic
httpx Target Connector
Markdown + JSON Reports
```

LangGraph 负责可恢复的 Adaptive Workflow，确定性领域核心负责 Policy、执行、Evaluator、
Evidence、Finding 和 Replay，SQLite 保存审计事实。该边界既能体现 Agent 工程能力，又
避免双 Agent Runtime、数据库堆叠和不必要的基础设施。
