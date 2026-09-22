# Attacker 技术选型与架构决策

> 文档状态：技术决策；Agent 运行时已更新为手写实现，其余交付阶段保留原规划语境
> 适用范围：Attacker v1
> 决策目标：形成一个可完成、可解释、可恢复的 Agent 安全评测项目，而不是展示技术名词集合。

## 1. 最终选型

| 领域 | 技术 | 职责 |
|---|---|---|
| 语言 | Python 3.12 | 兼容主流 Agent、Web 与数据库生态 |
| API | FastAPI、Uvicorn | 控制面、运行接口、健康检查 |
| Schema 与配置 | Pydantic、pydantic-settings | API、领域对象、配置校验 |
| Agent 编排 | Python 手写循环 | loop、tool、context、compact、SQL Session |
| 模型接入 | 独立 Model Adapter | 供 Planner 生成受约束的 JSON 或工具调用 |
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

## 2. 为什么使用手写循环

参考 Zeta，将一次请求展开为 `prepare -> request -> execute_tool -> save_session`。
Attacker 的动作限定为执行已批准候选和请求结束，普通函数与 `while` 足以表达业务分支。
Policy、审批、Evaluator、Evidence 和停止预算继续由现有领域服务处理。

审批与 Planner 暂停显式返回等待状态，恢复请求读取 SQL Session，再重新校验策略。
不再维护图节点、条件边、interrupt、Saver、独立 checkpoint 数据库或运行时 Registry。
执行中结果不明的 Session 不自动重放；旧 LangGraph 暂停记录不能直接迁移续跑。

## 3. 保持一套运行时

不引入 PydanticAI、OpenAI Agents SDK 或 LangChain 的 Agent/Memory 抽象。
模型通过窄 `PlannerModelAdapter` 接口注入，JSON 和原生 tool calling 共享工具执行入口。
Pydantic 仍负责 Schema 校验，不承担 Agent 调度。

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

## 5. Agent 与领域核心的边界

| 模块 | 职责 |
|---|---|
| loop | 显式顺序、循环和等待状态 |
| runtime | 候选、请求、usage、降级与领域服务调用 |
| tools | 普通工具表、参数约束和执行函数 |
| context | 按预算选择当前事实和连续历史后缀 |
| compaction | 连续旧轮次摘要，保留最近完整调用/结果 |
| session/state | SQL Session 身份、状态与恢复所需引用 |

Planner 返回 `PlannerDecision(action="execute" / "finish")`，或原生调用
`execute_candidate` / `finish_run`。候选 ID 必须来自实际展示的快照，引用必须属于当前
Run。工具执行仍经过 Policy/Approval，结束请求仍经过 Finish Gate。

`RunState` 保存身份、已完成 Case、当前操作、引用、计数与停止状态；
`RunResources` 仅在调用期间持有 Target/Planner 对象和凭据。完整证据留在 SQL。
请求视图和摘要不改变权威事实。详细对象流见 [Agent Runtime](docs/agent-runtime.md)。

## 6. 两种运行模式

### 6.1 Deterministic Mode

```text
Dataset -> Case Iterator -> Policy -> Connector -> Evaluator
        -> Event/Finding -> Report
```

特点：

- 不调用 LLM；
- 直接调用领域 Pipeline；
- 执行顺序固定；
- 可复现；
- 是 Judge 校准、回归评测和 Adaptive Mode 比较基线。

### 6.2 Adaptive Mode

```text
Dataset + Policy + prior Findings
  -> Handwritten Agent Loop + Planner
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

## 7. 事实与 Session 同库

SQLAlchemy 保存 Target、Dataset、Policy、Run、Step、Event、Finding、Approval、Replay
和 Job。Agent Session 通过现有 `agent_session_saved` 事件保存，不新增第二套数据库。

1. 工具结果落库后才进入下一轮；稳定 `operation_id` 防止重复提交。
2. Session 只保存引用、摘要、计数和继续所需状态，不复制完整 Evidence。
3. 恢复校验 Session 身份，并使用 SQL owner token 和续租防止并发领取。
4. 恢复先消耗原暂停点，再执行工具；失去租约时取消循环。
5. `running` 不代表工具未执行，不自动重放未知副作用。
6. 报告从领域事实生成，Session 和压缩摘要不能改写审计结论。

历史列名 `checkpoint_ref` 等为兼容既有 Schema 保留，新的内容指向 Session。

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

```text
api -> application services -> agent loop/runtime -> domain services
agent context/tools -> schemas + prompt governance
repositories -> SQLAlchemy + EventStore
model adapter -> provider HTTP transport
```

`app/agent` 包含 `loop.py`、`runtime.py`、`tools.py`、`context.py`、`compaction.py`、
`session.py` 和 `state.py`。已删除 `app/workflows/attack_graph.py`、`attack_state.py`
与 `app/infrastructure/checkpoint.py`，不保留旧图的兼容执行入口。

## 10. 安全约束

### Target Policy

- Target 必须由用户显式创建；
- 只允许配置的 endpoint；
- 默认拒绝公网和未知资产；
- Redirect 后重新校验目的地址；
- 目标响应不能扩展测试范围。

### Action Policy

- Planner 只能选择执行候选或请求结束；
- Case 必须来自允许集合；
- 每次 Target 调用前重新校验预算；
- 高风险 Case 必须返回等待审批状态；
- 不提供 Shell、浏览器、文件系统或通用 HTTP 工具。

### Secret Policy

- 凭据由 Connector 在调用时注入；
- Planner、Session、日志和报告不持有明文凭据；
- Evidence 写入前执行字段级脱敏；
- tracing 默认不记录完整目标输入输出。

## 11. 可观测性

日志至少包含：

- `run_id`；
- `thread_id`；
- `event_type`；
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
- 实现手写 Loop、Planner Model Adapter 和工具分发；
- 实现 Policy Gate、SQL Session、Approval 和稳定 `operation_id`；
- 覆盖未授权工具、危险参数、审批绕过、Tool Output Injection 和 Planner 循环；
- 使用 Mock Tool 或沙箱 Target 验证副作用，不接触生产资源。

第二阶段的 Finding 必须同时引用模型响应和 Tool/Policy Trace。只看到最终文本时，不宣称
已经证明工具越权。

### 第三阶段：带状态 Agent

- 定义测试专用 Memory Adapter 和 RAG Adapter；
- 保存 Session、User、Tenant、Memory、Dataset、Policy 和 Evaluator 快照；
- 记录 Retrieval Document、排名、来源和权限过滤结果；
- 使用 SQL Session 和稳定 `thread_id`；
- 实现 Finding fingerprint、source/replay Run 和修复差异；
- 覆盖 Memory Poisoning、RAG Poisoning、跨用户污染、恢复安全和 Replay；
- 提供污染数据清理与隔离。

第三阶段要求 Session 恢复后重新校验 Policy；已提交结果按 operation_id 复用，未知副作用
不自动重放。Memory/RAG 测试必须使用隔离测试数据。

## 13. 组件引入条件

| 组件 | 真实条件 |
|---|---|
| PostgreSQL | 多实例 Worker、远程共享或 SQLite 写锁成为瓶颈 |
| Redis/队列 | API 与 Worker 分离，需要后台运行和任务调度 |
| Qdrant | Case 数量达到语义检索确实优于分类筛选的规模 |
| MinIO | Evidence 体积超出数据库和本地文件管理能力 |
| DuckDB/Parquet | 需要跨大量 Run 的批量离线统计与导出 |
| Web 前端 | API 和报告闭环稳定，且真实需要审批操作台 |

不因扩容再次引入第二套 Agent Runtime；新增设施需要对应实际瓶颈。

## 14. 结论

Attacker v1 的技术主线是：

```text
Python 3.12
FastAPI + Pydantic
Handwritten Agent Loop + SQL Session
Pydantic Evals + YAML
SQLAlchemy Async + SQLite + Alembic
httpx Target Connector
Markdown + JSON Reports
```

手写循环负责 Adaptive 编排，确定性领域核心负责 Policy、执行、Evaluator、Evidence、
Finding 和 Replay。SQLite 用于本地，PostgreSQL 用于生产，Session 与事实共享数据库。
