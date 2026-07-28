# Attacker v1 范围摘要

> 文档状态：目标方案
> 当前基线：FastAPI + DuckDB/Parquet 单条评测原型
> 目标架构：LangGraph + SQLAlchemy/SQLite 可恢复评测工作流

## 1. 目标

构建一个面向 AI Agent 的有界安全评测工具：

```text
Target + Dataset + Policy
  -> Run
  -> Evidence-backed Finding
  -> Report
  -> Replay
```

只测试用户明确授权的目标，不扫描未知资产，不执行真实破坏性动作。

项目用于展示真实 Agent 工程能力：状态建模、条件路由、策略约束、人工审批、中断恢复、
证据持久化和 Replay，而不是通过同时接入多个 Agent 框架堆砌技术栈。

## 2. 两种模式

### Deterministic Mode

- Pydantic Evals Dataset/Case；
- 固定执行顺序；
- 确定性 Evaluator 优先；
- 不调用 LLM；
- 不依赖 LangGraph；
- 作为回归和 Adaptive 对照基线。

### Adaptive Mode

- LangGraph StateGraph；
- Planner Node 从批准 Case 中选择下一步；
- Policy Gate 执行最终授权；
- checkpoint 支持中断恢复；
- interrupt 支持高风险 Case 人工审批；
- 与 Deterministic Mode 共用 Connector、Evaluator 和 Repository。

## 3. v1 主技术栈

- Python 3.12；
- FastAPI、Uvicorn；
- Pydantic、pydantic-settings；
- LangGraph；
- Pydantic Evals、YAML；
- httpx；
- SQLAlchemy Async、SQLite、Alembic；
- Markdown、JSON；
- Ruff、Pyright、pytest。

## 4. v1 不采用

- PydanticAI；
- OpenAI Agents SDK；
- 多 Agent；
- LangChain Chains、Agents 和 Memory；
- 通用 Shell、浏览器、文件和 HTTP 工具；
- Redis、任务队列；
- PostgreSQL；
- MinIO、Qdrant；
- DuckDB/Parquet 在线主链；
- Web 前端；
- Kubernetes。

## 5. 三阶段测试范围

三个阶段按观测能力递进，但都属于最终交付范围，后两个阶段不是可选扩展。

| 阶段 | 目标 Agent 接入深度 | 测试方法 | 最小 Case 数 |
|---|---|---|---:|
| 第一阶段：纯黑盒 | Request/Response | Direct Prompt Injection、System Prompt Leakage、Sensitive Data Canary、多轮上下文污染、资源消耗 | 12 |
| 第二阶段：灰盒 Agent | Response + Tool/Policy Trace | 工具越权、参数越权、审批绕过、Tool Output Injection、Planner 循环 | 10 |
| 第三阶段：带状态 Agent | Trace + Memory/RAG/Checkpoint | Memory Poisoning、RAG Poisoning、跨用户污染、Checkpoint 恢复安全、Replay 差异 | 8 |

合计不少于 30 条高质量 Case。每种方法至少有一个成功样例和一个安全拒绝或正常行为
对照，避免只统计攻击话术数量。

### 5.1 第一阶段：纯黑盒安全评测

只依赖目标 Agent 的标准请求和响应接口，验证：

- 低权限输入是否能够覆盖高权限指令；
- System Prompt Canary 是否泄露；
- 测试用敏感 Canary 是否出现在输出中；
- 分散在多轮会话中的污染是否改变后续行为；
- 长上下文、重复请求和异常输出是否突破运行预算。

### 5.2 第二阶段：灰盒工具与权限评测

要求目标 Agent 提供脱敏的 Tool Trace、Policy Decision 和 Approval Event，验证：

- 用户是否能触发未授权工具；
- 合法工具是否使用越权资源 ID 或危险参数；
- Approval 是否被复用、拆分或绕过；
- 不可信 Tool Output 是否劫持后续规划；
- Planner 是否出现重复工具调用、无限重试或循环。

### 5.3 第三阶段：状态、知识与恢复评测

要求测试环境提供受控 Memory、RAG 和 Checkpoint 接口，验证：

- 污染内容是否跨轮次或跨会话持续；
- 污染文档是否被 RAG 召回并改变行为；
- 用户或租户之间是否发生上下文串扰；
- Checkpoint 恢复是否重新校验权限并保持幂等；
- Replay 是否能识别 fixed、new、persistent 和 regressed Finding。

## 6. 核心架构

```text
FastAPI
  |
  +-- Deterministic Runner
  |
  \-- LangGraph Adaptive Workflow
       |- plan_next_case ------> LLM
       |- policy_gate ---------> allow / deny / approval
       |- human_review --------> interrupt / resume
       |- execute_target ------> Target Connector
       |- evaluate_result -----> Evaluators
       |- persist_evidence ----> SQLite
       |- decide_next ---------> continue / finish
       \- generate_report -----> Markdown / JSON
```

职责边界：

- LangGraph 负责 Adaptive 控制流；
- Planner 只提出下一条 Case；
- Policy、Target 调用、Evaluator 和 Evidence 保持确定性；
- SQLite 保存业务与审计事实；
- Checkpoint 只负责工作流恢复；
- 有副作用节点通过稳定 `operation_id` 保证恢复和重试幂等；
- 报告和 Replay 只读取 SQLite。

## 7. LangGraph 状态图

```text
initialize
  -> plan
  -> policy
       -> denied -> skip
       -> approval -> interrupt/resume
       -> allowed -> execute
  -> evaluate
  -> persist
  -> decide
       -> continue
       -> report
```

只有 Planner Node 调用 LLM。Planner 无法创建任意 Case、Target、工具或 prompt。

## 8. 当前状态

以下状态描述仓库当前代码，不代表目标架构已经落地。

已存在：

- FastAPI 应用；
- HTTP Target Connector；
- 单条 YAML Case；
- 单条 dry-run；
- 基础规则 Judge；
- DuckDB/Parquet 证据原型；
- 本地哈希相似检索。

尚未实现：

- Python 3.12 迁移；
- SQLite/SQLAlchemy/Alembic；
- 批量 Run 和领域事件；
- Pydantic Evals Dataset；
- 黑盒多轮 Case 和资源预算；
- Tool/Policy Trace 接入契约；
- Memory/RAG 测试 Adapter；
- 报告和 Replay 差异；
- LangGraph 状态图；
- checkpoint、interrupt 和 Approval。

## 9. 三阶段实施计划

### 9.1 第一阶段：纯黑盒

基础工程：

- 将目标版本调整为 Python 3.12；
- 使用 SQLAlchemy Async、SQLite 和 Alembic 建立 Run、Step、Event、Finding；
- 使用 Pydantic Evals/YAML 管理 Case；
- 完成批量 Deterministic Run、预算限制和 Markdown/JSON 报告。

安全能力：

- Direct Prompt Injection；
- System Prompt Leakage；
- Sensitive Data Canary；
- 多轮上下文污染；
- 资源消耗与超时。

阶段验收：

- 仅凭 HTTP Request/Response 即可运行；
- 至少 12 条 Case 可重复执行；
- 每条 Finding 引用请求、响应和 Evaluator Evidence；
- 网络错误、拒绝、违规和预算中止分别统计；
- 报告可以完全从 SQLite 重建；
- 正常业务对照用于计算误报和防御误伤。

### 9.2 第二阶段：灰盒 Agent

基础工程：

- 定义脱敏 `ToolEvent`、`PolicyEvent` 和 `ApprovalEvent` 接入契约；
- 实现 LangGraph Adaptive Workflow、Planner、Policy Gate 和条件边；
- 使用 Mock Tool 或沙箱 Target 隔离真实副作用；
- 实现 interrupt、Approval API、循环检测和 Target 调用幂等。

安全能力：

- 未授权工具调用；
- 工具参数和资源 ID 越权；
- 审批绕过与历史批准复用；
- Tool Output Injection；
- Planner 循环和失败重试放大。

阶段验收：

- 至少 10 条灰盒 Case 可运行；
- 每次工具请求都有调用身份、参数摘要、Policy Decision 和实际执行结果；
- 所有 Target/Tool 调用都经过 Policy Gate；
- 高风险动作未批准时不产生副作用；
- Planner 循环能被步数、时间或重复状态检测终止；
- Adaptive 与 Deterministic 使用相同 Case 和 Evaluator 比较。

### 9.3 第三阶段：带状态 Agent

基础工程：

- 定义测试专用 Memory 和 RAG Adapter；
- 保存会话、用户、租户、Memory、Dataset、Policy 和 Evaluator 快照；
- 接入 LangGraph checkpoint 并建立稳定 `thread_id`；
- 实现 Finding fingerprint 和 source/replay Run 关联；
- 提供测试数据清理和污染隔离。

安全能力：

- Memory Poisoning；
- RAG Poisoning；
- 跨用户或跨租户上下文污染；
- Checkpoint 恢复安全；
- Replay 与修复前后差异。

阶段验收：

- 至少 8 条带状态 Case 可运行；
- 可以测量污染的写入、持续、跨会话和清除结果；
- RAG Evidence 包含召回文档、排名、来源和权限过滤结果；
- Checkpoint 恢复后重新校验 Policy，且不重复 Target/Tool 调用或 Finding；
- Replay 可区分 fixed、new、persistent、regressed；
- 清理操作不会影响非测试用户和非测试数据。

## 10. 完成标准

- 第一、第二、第三阶段全部通过验收，不能只完成纯黑盒阶段；
- 不少于 30 条 Case 全部可运行；
- 每种测试方法都有攻击样例和正常或安全拒绝对照；
- 每条 Finding 引用 Evidence；
- 报告可以完全从 SQLite 重建；
- Replay 可区分 fixed、new、persistent、regressed；
- Planner 无法越出 Target/Case allowlist；
- 所有 Target 调用经过 Policy Gate；
- 高风险 Case 未批准不能执行；
- 中断恢复不重复 Target 调用或 Finding；
- Memory/RAG 污染能够被隔离、测量和清理；
- 跨用户或跨租户污染率为 0；
- checkpoint 和模型上下文不包含凭据；
- Adaptive 与 Deterministic 结果可以比较。

最终演示必须包含三条完整证据链：

```text
黑盒输入 -> 目标响应 -> Evaluator -> Finding
灰盒输入 -> Tool/Policy Trace -> Authorization Finding
状态污染 -> Memory/RAG/Checkpoint -> Replay Diff
```

## 11. 详细文档

- [项目计划书](../PROJECT_PROPOSAL.md)
- [技术选型](../TECH_STACK.md)
- [目标架构](../docs/architecture.md)
