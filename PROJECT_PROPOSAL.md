# Attacker：基于 LangGraph 的 AI Agent 安全评测平台项目计划书

> 版本：v0.4
> 状态：目标方案
> 目标：完成一个适合 Agent 岗位面试展示、同时具备真实工程边界的安全评测项目。

## 1. 项目摘要

Attacker 面向经过明确授权的 AI Agent 测试环境，执行可版本化的攻击 Case，记录目标
响应，通过可解释 Evaluator 判断风险，并保存可以审计和 Replay 的证据。

项目包含两种互补模式：

1. **Deterministic Mode**：固定 Dataset、执行顺序和 Evaluator，保证可复现基线；
2. **Adaptive Mode**：使用 LangGraph 编排有界状态图，由 Planner 在批准 Case 中选择
   下一步，支持 checkpoint、条件路由和高风险步骤人工审批。

核心产品链路：

```text
Target + Dataset + Policy
  -> Evaluation Run
  -> Evidence-backed Finding
  -> Report
  -> Replay
```

## 2. 项目价值

普通 API 测试能够验证状态码和固定字段，但难以覆盖 Agent 特有风险：

- Prompt Injection；
- System Prompt Leakage；
- Sensitive Data Exposure；
- Unauthorized Tool Behavior；
- 多轮上下文导致的策略偏移；
- Tool 调用边界和授权失效；
- 修复后安全行为是否真实改善。

Attacker 的重点不是生成大量相似攻击 prompt，而是建立：

- 可复现的 Case；
- 显式 Policy；
- 可解释 Evaluator；
- 可追溯 Evidence；
- 有界 Adaptive Workflow；
- 修复前后 Replay。

## 3. 产品定位与边界

### 3.1 对外定位

> 面向 AI Agent 的可恢复安全评测工作流，在明确授权和预算内执行测试，输出证据化
> Finding，并支持 Replay 与修复差异比较。

### 3.2 不宣称

v1 不宣称：

- 自动发现未知漏洞；
- 扫描互联网资产；
- 替代人工渗透测试；
- 对所有模型进行通用越狱；
- 自动修复目标 Agent；
- 使用多 Agent 协同攻击；
- 具备生产级多租户平台能力。

Adaptive Mode 仍然是有界评测，不拥有任意网络、Shell、浏览器和文件系统权限。

## 4. 目标用户与场景

### 4.1 用户

- Agent 应用开发者；
- AI 安全评测工程师；
- 平台和质量工程师；
- 需要验证修复效果的研发团队。

### 4.2 场景

1. 发布前运行固定安全 Dataset；
2. 对新模型或新 Prompt 版本执行回归；
3. 让 Adaptive Workflow 根据既有 Finding 调整 Case 顺序；
4. 在高风险 Case 前暂停并人工批准；
5. 服务中断后恢复同一个评测 Run；
6. 对修复前后结果执行 Replay 和差异比较；
7. 导出可审阅的 Markdown/JSON 报告。

## 5. v1 范围

### 5.1 必须包含

- HTTP JSON Target Connector；
- Target、Dataset、Policy 和 Evaluator 快照；
- Pydantic Evals Dataset/Case；
- 三个安全评测阶段、共不少于 30 条高质量 Case；
- Deterministic Mode；
- LangGraph Adaptive Workflow；
- Planner Node 的结构化输出；
- Policy Gate 与运行预算；
- checkpoint 和中断恢复；
- 高风险 Case 的 interrupt/approval；
- 脱敏 Tool/Policy/Approval Trace 接入契约；
- 测试专用 Memory/RAG Adapter；
- 跨用户和跨租户隔离验证；
- SQLite 中的 Run、Step、Event、Finding、Approval、Memory/RAG Snapshot 和 Replay；
- Markdown/JSON 报告；
- Finding fingerprint 与修复差异比较。

### 5.2 不包含

- PydanticAI；
- OpenAI Agents SDK；
- 多 Agent；
- 通用 Shell、浏览器、文件和 HTTP 工具；
- 自动公网扫描；
- Redis/任务队列；
- PostgreSQL；
- MinIO、Qdrant；
- Web 前端；
- Kubernetes。

## 6. 三阶段安全评测范围

三个阶段按目标 Agent 的可观测能力递进，并全部属于 v1 最终交付范围。

| 阶段 | 接入深度 | 测试范围 | 最小 Case 数 |
|---|---|---|---:|
| 纯黑盒 | Request/Response | Direct Prompt Injection、System Prompt Leakage、Sensitive Data Canary、多轮上下文污染、资源消耗 | 12 |
| 灰盒 Agent | Tool/Policy/Approval Trace | 工具越权、参数越权、审批绕过、Tool Output Injection、Planner 循环 | 10 |
| 带状态 Agent | Memory/RAG/Checkpoint | Memory Poisoning、RAG Poisoning、跨用户污染、恢复安全、Replay | 8 |

每条 Case 必须包含：

- 稳定 ID；
- 阶段、注入面和风险级别；
- 输入；
- 预期违规；
- 正常或安全拒绝对照；
- Evaluator；
- 适用 Target 能力；
- 是否要求人工批准；
- 所需 Evidence 类型；
- 测试数据清理步骤；
- 证据脱敏要求。

## 7. 目标架构

```text
                         FastAPI
                            |
                 +----------+----------+
                 |                     |
                 v                     v
       Deterministic Runner    LangGraph Workflow
                               | plan / route / resume
                 |                     |
                 +----------+----------+
                            |
                            v
              Deterministic Evaluation Core
       Policy / Connector / Evaluator / Evidence
                            |
              +-------------+-------------+
              |                           |
              v                           v
    SQLite Business Facts       LangGraph Checkpoint
    Run/Event/Finding/Replay     control-flow recovery
```

核心边界：

- LangGraph 只编排 Adaptive Workflow；
- Planner 只提出下一条 Case；
- Policy Gate 才能授权 Target 调用；
- Evaluator 决定 Finding，不接受 Planner 覆盖；
- SQLite 保存业务和审计事实；
- Checkpoint 只保存控制流恢复数据；
- 报告只读取 SQLite。

## 8. 关键架构决策

### 8.1 LangGraph 负责 Adaptive Workflow

Adaptive Mode 需要：

- 类型化 Graph State；
- 条件边和循环；
- 节点失败恢复；
- checkpoint；
- interrupt 与人工审批；
- 节点级运行轨迹。

Attacker 的生命周期与状态图直接对应，因此 LangGraph 有真实业务价值，不是为了增加
框架关键词。

### 8.2 不同时使用 PydanticAI

同时使用 LangGraph 和 PydanticAI 会引入两套 Agent Runtime、两套工具与消息抽象，并
让状态和 tracing 边界变得模糊。v1 只保留 LangGraph，模型通过窄接口注入 Planner Node。

### 8.3 确定性核心与 Graph 解耦

以下能力不依赖 LangGraph：

- PolicyService；
- TargetConnector；
- EvaluatorService；
- EvidenceRepository；
- FindingService；
- ReplayService；
- ReportService。

Deterministic Mode 直接调用这些服务；Adaptive Mode 的 Graph Node 也调用同一组服务。

### 8.4 Checkpoint 不是事实源

Checkpoint 回答“工作流从哪里继续”，SQLite 回答“评测实际发生了什么”。

一致性规则：

- 有副作用节点使用稳定 `operation_id`；
- Repository 幂等写入；
- 领域事务提交后再推进 checkpoint；
- 恢复时先读取数据库事实；
- 冲突时以数据库最后提交 Event 为准；
- 报告不读取 Graph State。

### 8.5 Pydantic Evals 负责 Dataset 和实验

Pydantic Evals 表达 Dataset、Case 和 Evaluator，并用于确定性实验和 Judge 校准。它不
保存 Run、Approval、Finding 和 Replay 产品状态。

### 8.6 SQLite 是 v1 唯一业务事实源

SQLite + SQLAlchemy Async 保存：

- Target、Dataset、Policy、Evaluator 快照；
- Run、Step、Event；
- Evidence、Finding；
- Approval Decision；
- Replay；
- 报告索引。

DuckDB/Parquet 只保留为未来离线导出方向，不参与在线控制链。

## 9. LangGraph 工作流

### 9.1 节点

```text
initialize_run
plan_next_case
policy_gate
human_review
execute_target
evaluate_result
persist_evidence
record_skip
decide_next
generate_report
```

只有 `plan_next_case` 调用 LLM，其他节点保持确定性。

### 9.2 Planner 输出

```python
class PlannerDecision(BaseModel):
    action: Literal["execute_case", "finish_run"]
    case_id: str | None
    reason: str
```

Planner 只能从 `allowed_case_ids` 选择。它不能生成任意 Target、任意工具和任意可执行
prompt。

### 9.3 Human Review

配置为高风险的 Case 在 Target 调用前触发 interrupt。审批决定必须：

- 绑定 `run_id`、`approval_id` 和 `case_id`；
- 记录批准人、原因和时间；
- 写入 Event；
- 恢复后再次经过 Policy Gate；
- 只对当前 Run 的当前 Case 有效。

## 10. 领域对象

| 对象 | 关键字段 |
|---|---|
| Target | `id`、endpoint、request template、auth reference、enabled |
| Dataset | `id`、version、content hash |
| Case | `id`、category、severity、input、evaluators、approval flag |
| Policy | allowlists、budgets、approval rules、stop rules |
| Run | mode、snapshots、status、budget usage、timestamps |
| Step | node、case、operation、status、duration |
| Event | sequence、type、payload、evidence reference |
| Finding | fingerprint、risk、reason、evidence ids |
| Approval | case、status、resolver、reason、timestamps |
| Replay | source run、new run、finding diff |

## 11. Judge 与 Evidence

Evaluator Pipeline：

```text
transport validation
  -> deterministic rule
  -> structured response check
  -> optional model judge
  -> finding aggregation
```

要求：

- 网络错误和超时不视为安全通过；
- 确定性规则优先；
- Model Judge 仅用于模糊语义场景；
- Planner 与 Model Judge 分开配置和计量；
- Finding 必须引用 Event/Evidence；
- 所有敏感字段在写入前脱敏。

## 12. 安全边界

- 只测试显式配置和启用的 Target；
- 不发现或扫描未知 endpoint；
- Redirect 后重新校验目标；
- Planner 只能选择 allowlist 内 Case；
- 不向 Graph 暴露通用执行工具；
- 每次 Target 调用前校验剩余预算；
- 高风险 Case 未批准时不能执行；
- Target 凭据不进入模型、Graph State、checkpoint、日志和报告；
- 目标响应不能自动扩大测试范围。

## 13. 当前代码基线

### 已有原型

- FastAPI 应用工厂和健康检查；
- HTTP Target Connector；
- YAML 单条 Case；
- 单条 dry-run；
- 基础字符串 Judge；
- DuckDB 结果写入；
- 每事件 Parquet 写入；
- 本地哈希相似检索。

### 与目标架构的差距

- 当前 Python 版本仍为 3.14；
- 尚未建立 SQLAlchemy 领域模型和 Alembic；
- 尚未形成批量 Run 生命周期；
- 尚未使用 Pydantic Evals；
- 尚未完成黑盒多轮 Case 和资源预算；
- 尚未定义 Tool/Policy/Approval Trace；
- 尚未接入测试专用 Memory/RAG Adapter；
- 尚未验证跨用户和跨租户隔离；
- 尚未实现报告与 Replay；
- 尚未接入 LangGraph；
- 尚未实现 checkpoint、interrupt 和 Approval；
- DuckDB/Parquet 仍需迁移。

## 14. 三阶段实施计划

三个阶段可以分别演示和验收，但项目只有在三个阶段全部完成后才达到最终完成定义。

### 14.1 第一阶段：纯黑盒

交付物：

- Python 3.12 项目配置；
- SQLAlchemy Async Engine 和 Session；
- Alembic 初始化迁移；
- Target、Dataset、Policy、Run、Step、Event、Finding 表；
- Repository 接口和幂等 `operation_id`；
- 现有 dry-run 写入 SQLite；
- Pydantic Evals Dataset/Case；
- 12 条黑盒 Case；
- Direct Prompt Injection、System Prompt Leakage、Sensitive Data Canary、多轮上下文污染和
  资源消耗 Evaluator；
- 批量 Deterministic Run；
- Markdown/JSON 报告。

验收：

- 单次运行产生连续 Event；
- 重复 `operation_id` 不重复写入；
- 标准 HTTP Request/Response 即可完成评测；
- 12 条黑盒 Case 可重复执行；
- 相同快照产生可复现 Case 顺序；
- 每条 Finding 有 Evidence；
- 网络错误、拒绝、违规和预算中止分别统计；
- 正常任务对照能够衡量误报和防御误伤；
- 报告完全从 SQLite 生成。

### 14.2 第二阶段：灰盒 Agent

交付物：

- 脱敏 `ToolEvent`、`PolicyEvent` 和 `ApprovalEvent`；
- Tool Trace Adapter；
- `AttackGraphState`、状态图、Planner Model Adapter 和条件边；
- Policy Gate、interrupt 和 Approval API；
- Mock Tool 或沙箱 Target；
- 循环检测、运行预算和 Target/Tool 调用幂等；
- 10 条工具、权限和规划安全 Case；
- Adaptive/Deterministic 对比报告。

验收：

- 每次工具请求都有调用身份、参数摘要、Policy Decision 和执行结果；
- Finding 同时引用模型响应和 Tool/Policy Trace；
- Planner 无法选择 allowlist 外 Case；
- 所有 Target/Tool 调用经过 Policy Gate；
- 高风险 Case 未批准不产生副作用；
- Planner 循环能被步数、时间或重复状态检测终止；
- Adaptive 额外发现和模型成本单独统计。

### 14.3 第三阶段：带状态 Agent

交付物：

- 测试专用 Memory Adapter 和 RAG Adapter；
- Session、User、Tenant、Memory、Dataset、Policy 和 Evaluator 快照；
- Retrieval Document、排名、来源和权限过滤 Evidence；
- LangGraph checkpoint 和稳定 `thread_id`；
- Finding fingerprint；
- source run 与 replay run 关联；
- fixed/new/persistent/regressed 差异；
- 8 条 Memory、RAG、隔离、恢复和 Replay Case；
- 测试污染数据清理能力。

验收：

- 可以测量污染的写入、持续、跨会话和清除结果；
- RAG Finding 可追溯到召回文档和权限过滤结果；
- 跨用户和跨租户污染率为 0；
- 进程中断后通过相同 `thread_id` 恢复并重新校验 Policy；
- 节点重试不重复调用 Target/Tool 或创建 Finding；
- Replay 不依赖旧的临时 Graph State；
- 历史报告可以从 SQLite 重新生成。

## 15. 成功指标

| 指标 | v1 目标 |
|---|---:|
| 高质量 Case | 不少于 30 |
| 完成阶段 | 3/3 |
| Finding 证据关联率 | 100% |
| Target 调用 Policy Gate 覆盖率 | 100% |
| 高风险 Case 审批覆盖率 | 100% |
| 报告数据库重建率 | 100% |
| Replay 可比较 Finding | 100% |
| 跨用户/跨租户污染率 | 0 |
| allowlist 外 Case 执行次数 | 0 |
| 恢复或重试导致的重复 Target/Tool 调用 | 0 |
| 日志/checkpoint 明文凭据 | 0 |

## 16. 项目完成定义

满足以下条件时，Attacker v1 可作为完整项目展示：

1. 新用户可以根据 README 启动项目；
2. 可以注册一个授权 Target；
3. 第一、第二、第三阶段全部通过验收；
4. 可以运行不少于 30 条 Case，每种方法都有攻击样例和安全对照；
5. 每条 Finding 都有与接入深度匹配的可追踪 Evidence；
6. 可以生成 Markdown/JSON 报告；
7. Adaptive Mode 使用 LangGraph 在预算内选择 Case；
8. 高风险 Case 支持人工审批和恢复；
9. Tool/Policy Trace 能证明工具是否越权，而不是仅根据最终文本推断；
10. Memory/RAG 污染能够被隔离、测量和清理；
11. 可以 Replay 并比较修复前后差异；
12. 中断恢复不会重复执行已提交的 Target/Tool 调用；
13. 文档明确区分当前实现与目标架构；
14. 不依赖未使用的基础设施；
15. 简历描述只包含实际完成并可演示的能力。
