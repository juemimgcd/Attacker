# Attacker Architecture

> 本文描述当前已实现架构；代码、迁移和运行时验证是事实来源。

## 1. 系统目标与边界

Attacker 在明确授权的测试环境中评测 AI Agent，保存可追踪、可恢复、可重建报告的
Evidence 和 Finding。系统支持两种编排方式与三个观测阶段：

- Deterministic：固定 Case 顺序，不依赖 Planner；
- Adaptive：手写循环编排候选选择、工具执行、审批、恢复和停止；
- Black-box：观察请求、响应、预算与 Evaluator；
- Gray-box：额外观察脱敏后的 Tool/Policy/Approval Trace；
- Stateful：额外观察隔离测试 Memory、RAG、身份和清理事实。

Planner 不是授权主体，Session 不是审计事实源，Job 状态也不能代替 Run 状态。

## 2. 当前组件关系

```text
FastAPI routes / Console / CLI
        |
        v
Application services
Run / Adaptive / Replay / Equipment / Job / Report / Harness
        |
        +-----------------------+
        |                       |
        v                       v
Domain pipelines          Handwritten Agent loop
Policy / Evaluator        plan / gate / pause / resume / stop
GrayBoxCasePipeline              |
        |                        |
        +-----------+------------+
                    v
Repositories + EventStore
        |
        v
SQLAlchemy DB: business facts + Agent Session
SQLite (local/test) / PostgreSQL (production)
```

主要依赖方向是 `api -> services/agent -> repositories -> infrastructure`；
Agent 请求上下文使用 Prompt Governance，模型 Adapter 使用独立 Context/Tool 协议模块。FastAPI
`app.state` 只安装应用服务和运行时设施，不暴露 Repository；Worker CLI 可从
`AppRuntime` 取得 `JobRepository`，因为租约领取本身就是 Worker 基础设施职责。

## 3. 模块职责

| 模块 | 责任 | 不负责 |
|---|---|---|
| `app/api` | HTTP 参数、状态码、应用服务调用 | SQL、租约、策略推断 |
| `app/services` | Use case 编排、Policy、Evaluator、共享 Pipeline | FastAPI 响应细节 |
| `app/agent` | 手写循环、规划、Context、Compaction、工具、SQL Session | 绕过 Gate 的副作用 |
| `app/repositories` | 事务、幂等、查询和事实持久化 | Case 选择、HTTP 路由 |
| `app/infrastructure` | 数据库、Secret、模型 Adapter | 评测结论 |
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

它直接执行确定性 Pipeline。`DeterministicRunService` 负责数据集路径边界、预算、目标绑定、
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

执行顺序由 `app/agent/loop.py:run_loop()` 中的一个 `while` 明确表达：

```text
prepare context -> request model -> validate and persist decision
  -> execute_candidate: Policy -> Approval / Skip / Pipeline -> update facts -> stop check
  -> finish_run: Finish Gate
  -> save SQL Session -> next turn / waiting / terminal
```

`AgentRuntime` 调用候选、预算、Policy、Finish Gate 和共享 Pipeline 服务；
`TOOLS` 是绑定普通异步函数的字典。审批返回等待状态，后续请求从 SQL Session 读取已选
Case，重新经过 Policy Gate；Planner 暂停恢复则重新准备输入。审批不是永久授权。

移除了 Graph 拓扑、节点路由、interrupt、Saver 和运行时 Registry。Context/Compaction/Tool
的输入输出、配置和旧运行迁移边界见 [Agent Runtime](agent-runtime.md)。

### 4.4 Stateful

`StatefulRunService` 使用测试专用的 `MemoryAdapter` 和 `RAGAdapter`，所有数据按
run/tenant/user/session 隔离。正常结束、异常和协程取消都会尝试清理夹具，并记录清理
成功或失败事实。脆弱 Profile 只用于显式沙箱测试，不能解释为生产后端已被验证。

### 4.5 Durable Job

```text
enqueue (allocate Job.id) -> lease -> running -> create Run(id=Job.id) -> bind job.run_id
    |                                  |
    | cancel_requested                 v
    +------------------------> cancel dispatch task
                                      -> Run persists cancelled
                                      -> Job becomes cancelled
```

`JobRepository` 负责 request 幂等、租约 owner/token/expiry、重试和恢复；
`JobDispatcher` 只把已校验 payload 路由到 Run Service。Job 分发会把预分配的 `Job.id` 作为
`Run.id`，Run 创建钩子随即写入 `run_jobs.run_id` 并在外部执行前重新检查取消状态。Worker
在轮询取消信号的同时续租，并通过 `asyncio` 协作式取消中断正在等待的 Target 调用。

只有租约过期前从未进入 `running` 且未创建 Run 的 Job 才能自动重试；一旦进入 `running`，
即使绑定尚未写回，也不能证明旧 Worker 没有提交 Run。恢复、重新领取或人工重试时还会按相同 ID
回查并补齐遗漏绑定；
Run 一旦存在，租约过期或执行失败会进入
`job_run_recovery_required`，不能再次分发同一 payload；操作者必须查看已绑定 Run，并选择
Replay 或使用新的 request 创建新任务，避免重复外部副作用。

取消的硬边界：Connector 必须在可取消的 async await 点执行 I/O。无法协作取消的本地
阻塞代码仍需要进程隔离或 Harness 超时，不能仅依赖协程取消。

### 4.6 Console 与黄金路径

`/console` 是由 FastAPI 同源提供的静态操作台，直接复用受保护的 Job、Run、Approval、Replay
和 Report API，不拥有独立事实或鉴权模型。`attacker demo` 使用内置隔离数据运行 vulnerable
基线与 hardened Replay，并生成 Markdown 差异报告；它不连接外部 Target。

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

### 5.3 One database, separate facts and Session

SQLAlchemy 业务库保存“实际发生了什么”：

- Run、Step、Event、Evidence、Finding；
- Dataset、Target、Policy、Evaluator 和 Equipment 快照；
- Approval、Replay、Memory、Retrieval 和 Cleanup；
- Durable Job、租约和 Worker heartbeat；
- `agent_session_saved` 事件保存 Agent Session。

Session 保存运行状态、当前绑定工具及计数，事件 ID 标识暂停位置。报告和 Replay
读取领域事实，Session 不能覆盖已提交 Evidence。只有等待审批或 Planner 暂停可恢复；
执行中结果不明的状态不自动重放。旧 LangGraph 暂停记录需要旧版本完成或新建运行。

## 6. Session State 契约

`RunState` 只保存恢复所需的引用、摘要、计数和停止状态，主要分组如下：

- identity：`run_id`、`target_id`、`thread_id`、`checkpoint_ref`；
- candidate/control：候选快照、已完成/拒绝 Case、当前 operation/step；
- facts：coverage、hypothesis/observation/finding/information-gain refs；
- policy/review：decision、reason、policy Event、approval；
- bounded usage：Planner/Provider/Target 调用、token、cost、duration 派生计数；
- loop control：重复决策、重复状态、无收益步数、transport failure；
- terminal：`next_action`、`status`、`terminal_reason`、`stop_reason`。

完整响应、原始 Trace 和 Secret 不进入 Session；权威事实保留在领域事件中。恢复时先读取 SQL 事实，
再重建运行时对象并重新校验 Policy。

## 7. Policy、Planner 与 Evidence

Planner 只接收候选快照、脱敏 Observation、Evidence 引用、Coverage/Hypothesis 摘要和剩余
步数。默认返回 JSON `PlannerDecision`；`planner.response_mode="tools"` 使用
`execute_candidate` / `finish_run` 原生函数调用，并转换为同一个 `PlannerDecision`。
每轮只接受一个调用，不能创建任意 Target 或攻击动作。原生工具的执行结果从 SQL 重建，
下一轮带上完整的 assistant/tool 消息对。

`build_context()` 从 SQL 重建当前事实和完整工具历史，`prepare_context()` 计算指令、
消息、工具 Schema、输出预留和余量。视图按优先级加入完整候选资源，并保留连续的历史
后缀；放不下的旧轮次交给 `compact()` 聚合。摘要只能覆盖连续前缀，最近完整调用/结果
不能拆开。预算使用 UTF-8 字节估计，最小输入仍超限则走已有 Planner 失败策略。

模型调用 Event 的 `call_snapshot.context_snapshot` 保存实际使用的受治理输入，
`tool_schemas` 保存工具定义，`history_summary` 保存摘要覆盖 ID 和版本；模板、输入 checksum 和引用一同保留。报告仍以业务 Evidence
为准，历史摘要本身不产生 Finding。

Policy Gate 校验：

- Target、Case、Capability Contract 和 Provider Instance allowlist；
- Target 调用、Provider 调用、Agent step、duration 与 cost 预算；
- Case 风险等级和审批状态；
- 身份引用、重复次数和停止规则。

Finding 必须引用已持久化 Evidence。Trace 不完整时灰盒结果显式降为 inconclusive/error，
不能根据最终文本猜测内部工具越权。Planner 与可选 Model Judge 使用不同 Adapter、快照和
用量统计。

Equipment 快照只证明某个版本和 checksum 被声明并冻结。报告仅在存在匹配且已结束、
`physical_attempts > 0` 的 Harness execution 时标记为 `executed`；其余一律标记为
`declared_only`，不能把绑定事实冒充执行事实。

## 8. Secret 与执行安全

- Target Secret 只保存在请求期/运行时内存或受控 Secret Broker lease 中；
- Durable Job payload 拒绝 password、token、API key 和 URL 凭据等明文字段；Worker 只在分发
  带 `provider_instance_id` 的 Target 时按 Instance Secret 引用取得短租约；
- Request、Response、Trace、日志和报告在持久化前脱敏；
- Session 不保存 Header、Token 或完整未信任输出；
- Adaptive 运行时只在一次循环调用期间保留，调用完成、暂停或进入审批后立即销毁；恢复时重新校验
  同绑定非 Secret 行为，手工凭据由调用方重供，Provider 凭据则按冻结的 exact revision 短租；
- 默认目标是本地、测试或沙箱；公网目标还需要已启用 Provider Instance 的服务端 host
  allowlist、`agent.invoke.v1` Capability 和与 Instance 配置精确匹配的 endpoint，客户端
  `allow_public_target` 不能单独授权；连接使用本次已验证的数字地址并保留原 Host/SNI，避免
  校验后再次解析；DNS、所有候选地址和有界响应读取共享单次调用 deadline；
- Planner 没有 Shell、文件系统、浏览器或任意 HTTP 能力；
- Equipment 不能绕过 Core Policy、审批、预算、Evidence 和清理边界。

OIDC/JWT、用户体系、RBAC 和审批人组织权限仍不属于当前已交付范围；部署方必须在入口层
提供这些控制，不能把 API key 等同于完整身份治理。

### 8.1 威胁模型

| 资产或边界 | 主要威胁 | 已实现控制 | 剩余责任 |
|---|---|---|---|
| Target 与凭据 | 未授权公网调用、凭据持久化 | 私网默认、显式公网授权、Secret Broker、持久化前脱敏 | 部署方提供目标授权与密钥轮换 |
| Planner 与高风险动作 | 自主扩大范围、绕过审批 | 固定候选、Policy Gate、预算、人工审批后重新校验 | 部署方配置最小 allowlist |
| Equipment | 被篡改或不受信任代码执行 | checksum、Manifest/Contract 校验、信任级别、默认禁用不受信任包 | 强隔离执行需 Linux 容器后端 |
| Evidence 与 Finding | 无证据结论、历史被覆盖 | 追加式 Event、稳定 operation ID、Finding 证据引用、SQL 事实源 | 数据库访问控制与备份 |
| Job 与 Replay | 重试重复执行、范围漂移 | request 幂等、租约、Run 绑定、冻结输入与装备快照 | 外部 Target 仍须支持幂等语义 |
| API 控制面 | 越权读取或操作其他运行 | 可选 API key、生产配置门禁 | 多租户部署前必须增加 OIDC/RBAC 与入口隔离 |

## 9. 启动与关闭

`create_runtime` 的关键顺序：

1. 创建并初始化业务数据库；
2. 创建共享 EventStore 与 Repository；
3. 构建 Equipment/Harness，并重载 Catalog；
4. 恢复待清理资源；
5. 构建 Run、Replay、Report、Job 应用服务；
6. 将非 Repository 组件安装到 FastAPI `app.state`。

关闭时停止接收请求/任务，Worker 排空或取消在途任务，最后 dispose 数据库。
模块 import 阶段不得连接数据库、模型或 Target。Session 共用业务库，无独立建表或连接。

## 10. 架构决策记录

| 决策 | 选择 | 原因 |
|---|---|---|
| ADR-001 | 事实和 Session 同库、职责分开 | 删除独立 checkpoint 设施，报告仍只依赖领域事实 |
| ADR-002 | Adaptive 使用手写循环 | 顺序和分支直接可读，复用确定性领域服务 |
| ADR-003 | Planner 只选批准候选 | 自主性不能扩大授权边界 |
| ADR-004 | GrayBoxCasePipeline 跨模式复用 | 防止连接器、脱敏、Trace 和评估语义漂移 |
| ADR-005 | EventStore 原子分配 Run sequence | 消除 `MAX + 1` 并发竞争 |
| ADR-006 | Job 显式绑定 Run 并协作取消 | Job 生命周期必须能定位并终止真实执行 |
| ADR-007 | API 只依赖应用服务 | HTTP 层不拥有事务和持久化规则 |
| ADR-008 | Equipment 声明与执行证据分离 | 冻结 Manifest 不能证明物理调用发生 |

新增能力应先判断属于 Application、Agent、Pipeline、Repository 还是 Infrastructure，
避免在 Router 或循环体 中堆叠 SQL、HTTP 模板和 Evaluator 规则。

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
例如同一 Run 并发追加 Event、运行中 Job 取消、Target 调用幂等和 Session 恢复。

## 12. 相关文档

- [Equipment Development](equipment-development.md)：Provider、Skill、Case Pack 和 Contract；
- [Production Runbook](operations/production-runbook.md)：生产部署、迁移、排空和灾备；
- [`SECURITY.md`](../SECURITY.md)：漏洞报告方式、支持范围与安全研究边界。
