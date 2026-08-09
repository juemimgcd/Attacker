# Attacker Architecture

> 本文描述截至 2026-08-09 的已实现架构。历史阶段计划保留在
> `docs/superpowers/plans/`，不作为当前运行时事实来源。

## 1. 系统目标与边界

Attacker 在明确授权的测试环境中评测 AI Agent，保存可追踪、可恢复、可重建报告的
Evidence 和 Finding。系统支持两种编排方式与三个观测阶段：

- Deterministic：固定 Case 顺序，不依赖 Planner 或 LangGraph；
- Adaptive：LangGraph 只编排候选选择、审批、恢复和停止；
- Black-box：观察请求、响应、预算与 Evaluator；
- Gray-box：额外观察脱敏后的 Tool/Policy/Approval Trace；
- Stateful：额外观察隔离测试 Memory、RAG、身份和清理事实。

Planner 不是授权主体，checkpoint 不是审计事实源，Job 状态也不能代替 Run 状态。

## 2. 当前组件关系

```text
FastAPI routes / CLI
        |
        v
Application services
Run / Adaptive / Replay / Equipment / Job / Report / Harness
        |
        +-----------------------+
        |                       |
        v                       v
Domain pipelines          LangGraph workflow
Policy / Evaluator        plan / gate / pause / resume / stop
GrayBoxCasePipeline              |
        |                        |
        +-----------+------------+
                    v
Repositories + EventStore
        |
        +-----------------------+
        |                       |
        v                       v
SQLAlchemy business DB     LangGraph checkpoint
SQLite (local/test)        SQLite (local/test)
PostgreSQL (production)    PostgreSQL (production)
```

依赖方向是 `api -> services/workflows -> repositories -> infrastructure`。FastAPI
`app.state` 只安装应用服务和运行时设施，不暴露 Repository；Worker CLI 可从
`AppRuntime` 取得 `JobRepository`，因为租约领取本身就是 Worker 基础设施职责。

## 3. 模块职责

| 模块 | 责任 | 不负责 |
|---|---|---|
| `app/api` | HTTP 参数、状态码、应用服务调用 | SQL、租约、策略推断 |
| `app/services` | Use case 编排、Policy、Evaluator、共享 Pipeline | FastAPI 响应细节 |
| `app/workflows` | Adaptive 控制流与 checkpoint 投影 | 业务事实存储 |
| `app/repositories` | 事务、幂等、查询和事实持久化 | Case 选择、HTTP 路由 |
| `app/infrastructure` | 数据库、checkpoint、Secret、模型 Adapter | 评测结论 |
| `app/equipment` | Catalog、Schema、Runner、安全执行辅助 | 绕过 Core Policy |
| `app/schemas` | 跨层输入、输出与持久快照契约 | I/O 副作用 |

`app/runtime.py` 是 composition root：按依赖顺序创建数据库、共享 `EventStore`、
Repository、应用服务、工作流与装备运行时。

## 4. 运行路径

### 4.1 Deterministic black-box

```text
load and freeze dataset
  -> create Run
  -> freeze equipment binding
  -> iterate Cases
  -> validate preconditions and budget
  -> call Target
  -> evaluate
  -> persist Event / Finding
  -> finalize Run
```

它不经过 LangGraph。`DeterministicRunService` 负责数据集路径边界、预算、目标绑定、
中断终态和逐 Case 恢复事实。

### 4.2 Gray-box shared pipeline

确定性灰盒和自适应灰盒共用 `GrayBoxCasePipeline`：

```text
ensure Step
  -> execute Target with stable operation_id
  -> redact request and response
  -> parse Tool/Policy/Approval Trace
  -> persist target execution
  -> normalize untrusted observation
  -> deterministic evaluation
  -> persist case and optional Finding
```

两种模式只在“Case 如何被选中、是否暂停审批、何时停止”上不同。连接器、脱敏、
Trace 解析、评估和落库只有一份实现，避免基线与自适应语义漂移。

### 4.3 Adaptive gray-box

当前图节点为：

```text
START
  -> initialize_run
  -> build_candidates
  -> plan_next_case
       | execute candidate
       v
     policy_gate
       | approval required -> prepare_human_review -> human_review --+
       | deny -> skip                                           |
       | allow                                                  |
       v                                                        |
     execute -> normalize_observation -> evaluate -> persist    |
       -> update_facts -> decide_next -> build_candidates -------+

plan_next_case -> finish_gate -> finalize -> END
plan_next_case -> planner_pause -> build_candidates
```

Policy Gate 在所有 Target 副作用之前运行。Human Review 恢复后再次经过 Policy Gate；
审批不是跨 Run 或跨 Case 的永久授权。

### 4.4 Stateful

`StatefulRunService` 使用测试专用的 `MemoryAdapter` 和 `RAGAdapter`，所有数据按
run/tenant/user/session 隔离。正常结束、异常和协程取消都会尝试清理夹具，并记录清理
成功或失败事实。脆弱 Profile 只用于显式沙箱测试，不能解释为生产后端已被验证。

### 4.5 Durable Job

```text
enqueue -> lease -> running -> create Run -> bind job.run_id
    |                                  |
    | cancel_requested                 v
    +------------------------> cancel dispatch task
                                      -> Run persists cancelled
                                      -> Job becomes cancelled
```

`JobRepository` 负责 request 幂等、租约 owner/token/expiry、重试和恢复；
`JobDispatcher` 只把已校验 payload 路由到 Run Service。Run 创建钩子会立即写入
`run_jobs.run_id`。Worker 在轮询取消信号的同时续租，并通过 `asyncio` 协作式取消中断
正在等待的 Target 调用。

取消的硬边界：Connector 必须在可取消的 async await 点执行 I/O。无法协作取消的本地
阻塞代码仍需要进程隔离或 Harness 超时，不能仅依赖协程取消。

## 5. 事实、幂等与并发

### 5.1 EventStore

所有 Run Event 通过共享 `EventStore` 追加：

- `operation_id` 全局唯一，重复提交返回已有 Event；
- `runs.event_sequence` 是 Run 内序号分配器；
- 单条原子 `UPDATE ... RETURNING` 递增序号；
- Event 与同一领域变更可在一个数据库事务中提交；
- 不使用并发不安全的 `MAX(sequence) + 1`。

该设计保证同一 Run 的并发写入得到唯一、单调序号。序号表达已提交顺序，不表达不同
外部系统副作用之间的全局因果关系。

### 5.2 Stable operation IDs

Target、Evaluation、Approval、Memory/RAG 与清理操作使用稳定 `operation_id`。恢复时先查
业务事实：已完成的物理调用被复用，不重复执行；Finding 和 Event 同样按稳定标识幂等。

### 5.3 Two stores

SQLAlchemy 业务库保存“实际发生了什么”：

- Run、Step、Event、Evidence、Finding；
- Dataset、Target、Policy、Evaluator 和 Equipment 快照；
- Approval、Replay、Memory、Retrieval 和 Cleanup；
- Durable Job、租约和 Worker heartbeat。

LangGraph checkpoint 只保存“控制流从哪里继续”：当前节点、interrupt 和有界 State。
报告和 Replay 只从业务库重建；checkpoint 不能覆盖已提交业务事实。

## 6. Graph State 契约

`AttackGraphState` 只保存恢复所需的引用、摘要、计数和停止状态，主要分组如下：

- identity：`run_id`、`target_id`、`thread_id`、`checkpoint_ref`；
- candidate/control：候选快照、已完成/拒绝 Case、当前 operation/step；
- facts：coverage、hypothesis/observation/finding/information-gain refs；
- policy/review：decision、reason、policy Event、approval；
- bounded usage：Planner/Provider/Target 调用、token、cost、duration 派生计数；
- loop control：重复决策、重复状态、无收益步数、transport failure；
- terminal：`next_action`、`status`、`terminal_reason`、`stop_reason`。

完整响应、原始 Trace、Secret 和权威预算事实不进入 checkpoint。恢复时先读取 SQL 事实，
再重建运行时对象并重新校验 Policy。

## 7. Policy、Planner 与 Evidence

Planner 只接收候选快照、脱敏 Observation、Evidence 引用、Coverage/Hypothesis 摘要和剩余
步数。它只能返回结构化 `PlannerDecision`，不能创建任意工具调用、Target 或攻击动作。

Policy Gate 校验：

- Target、Case、Capability Contract 和 Provider Instance allowlist；
- Target 调用、Provider 调用、Graph step、duration 与 cost 预算；
- Case 风险等级和审批状态；
- 身份引用、重复次数和停止规则。

Finding 必须引用已持久化 Evidence。Trace 不完整时灰盒结果显式降为 inconclusive/error，
不能根据最终文本猜测内部工具越权。Planner 与可选 Model Judge 使用不同 Adapter、快照和
用量统计。

## 8. Secret 与执行安全

- Target Secret 只保存在请求期/运行时内存或受控 Secret Broker lease 中；
- Durable Job payload 拒绝 password、token、API key 等明文字段；
- Request、Response、Trace、日志和报告在持久化前脱敏；
- checkpoint 不保存 Header、Token 或完整未信任输出；
- 默认目标是本地、测试或沙箱；公共/不可解析目标需要显式授权开关；
- Planner 没有 Shell、文件系统、浏览器或任意 HTTP 能力；
- Equipment 不能绕过 Core Policy、审批、预算、Evidence 和清理边界。

OIDC/JWT、用户体系、RBAC 和审批人组织权限仍不属于当前已交付范围；部署方必须在入口层
提供这些控制，不能把 API key 等同于完整身份治理。

## 9. 启动与关闭

`create_runtime` 的关键顺序：

1. 创建并初始化业务数据库；
2. 在锁保护下准备 checkpoint schema；
3. 创建共享 EventStore 与 Repository；
4. 构建 Equipment/Harness，并重载 Catalog；
5. 恢复待清理资源；
6. 构建 Run、Replay、Report、Job 应用服务和 LangGraph；
7. 将非 Repository 组件安装到 FastAPI `app.state`。

关闭时停止接收请求/任务，Worker 排空或取消在途任务，关闭 checkpoint，最后 dispose
数据库。模块 import 阶段不得连接数据库、模型或 Target。

## 10. 架构决策记录

| 决策 | 选择 | 原因 |
|---|---|---|
| ADR-001 | SQL 事实与 graph checkpoint 分离 | 审计事实和控制流恢复的生命周期不同 |
| ADR-002 | Deterministic 不进入 LangGraph | 普通批处理不需要额外恢复状态机 |
| ADR-003 | Planner 只选批准候选 | 自主性不能扩大授权边界 |
| ADR-004 | GrayBoxCasePipeline 跨模式复用 | 防止连接器、脱敏、Trace 和评估语义漂移 |
| ADR-005 | EventStore 原子分配 Run sequence | 消除 `MAX + 1` 并发竞争 |
| ADR-006 | Job 显式绑定 Run 并协作取消 | Job 生命周期必须能定位并终止真实执行 |
| ADR-007 | API 只依赖应用服务 | HTTP 层不拥有事务和持久化规则 |

新增能力应先判断属于 Application、Workflow、Pipeline、Repository 还是 Infrastructure，
避免在 Router 或 Graph Node 中堆叠 SQL、HTTP 模板和 Evaluator 规则。

## 11. 验证基线

合入架构变更前至少执行：

```bash
uv sync --locked --python 3.12
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
uv run python -m compileall -q app conf alembic
```

持久化变更还必须验证 Alembic 从空库升级到 head。并发/恢复类变更需要包含行为测试，
例如同一 Run 并发追加 Event、运行中 Job 取消、Target 调用幂等和 checkpoint 恢复。

## 12. 相关文档

- [Equipment Development](equipment-development.md)：Provider、Skill、Case Pack 和 Contract；
- [Production Runbook](operations/production-runbook.md)：生产部署、迁移、排空和灾备；
- `target/summary.md`：V1 验收范围；
- `TECH_STACK.md`：技术栈与版本边界。
