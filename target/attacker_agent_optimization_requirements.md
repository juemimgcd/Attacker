# Attacker Agent 专项优化需求

## 1. 文档定位

本文只定义 Attacker 自身评测 Agent 的优化要求，包括攻击策略选择、反馈闭环、判定可信度、停止条件、模型降级和效果衡量。

本文对应前三阶段中的 Adaptive Agent 内核，不依赖第四阶段企业装备库才能成立。实现顺序必须是：

1. 先用 Core 内置 HTTP Connector、Case 和 Evaluator 跑通完整闭环；
2. 再完成灰盒和带状态评测、Approval、Replay 与效果校准；
3. 最后通过 Harness 文档定义的 Capability Contract 接入企业 Provider/Skill。

Provider、Skill、Harness、装备隔离与企业扩展机制见：

- [企业自定义评测 Harness 与 Provider/Skill 装备库需求](./harness_provider_skill_requirements.md)

两份文档的边界：

- 本文回答“Attacker 如何更有效且安全地完成评测”；
- Harness 文档回答“企业如何为 Attacker 接入和定制能力”；
- Planner、Policy Gate、Evidence/Finding 事实模型属于 Attacker Core；
- 模型、目标连接器和企业评估规则可以通过 Provider/Skill 扩展。

本文不建设 Attacker 多用户账户或 RBAC，但必须保留 `TestPrincipal`：它表示被测目标中的测试身份、租户、角色、凭据引用和 Session scope，用于验证目标 Agent 的权限与隔离，不等同于 Attacker 平台用户。

---

## 2. 当前实现差距

当前 `AttackExecutor` 的主流程仍是：

```text
单条 YAML Sample
  -> HTTP Target Connector
  -> 字符串规则 Judge
  -> 单次结果
```

它可以验证最小请求、响应和规则判断链路，但还不是完整的 Adaptive Attacker Agent。至少缺少：

- Run 级攻击目标与覆盖状态；
- 基于历史 Observation 的下一步选择；
- 攻击假设、反馈和策略切换；
- 重复、循环、无收益和预算停止；
- Planner 输出校验与确定性降级；
- Evaluator 置信度、冲突和 `inconclusive`；
- Agent Prompt、模型参数和决策输入快照；
- Adaptive 与 Deterministic 的效果、成本和稳定性对照。

目标是把 Attacker 从“单次执行器”补成“受约束、可恢复、可审计的安全评测 Agent”。

---

## 3. 设计原则

### 3.1 确定性 Core，有界 Agent

Attacker Agent 负责：

1. 根据 Run Goal、授权范围和当前覆盖状态提出下一步；
2. 只从已批准的 Case/Skill Action 候选集中选择；
3. 根据新 Evidence 更新攻击假设和覆盖状态；
4. 在继续、切换策略和建议结束之间做结构化选择；审批等待由 Core 的 Policy Gate 与 Approval Route 管理；
5. 输出可审计的选择理由和事实引用。

Attacker Agent 不负责：

- 创建授权范围外的 Target；
- 发现或扫描未知资产；
- 生成并直接执行任意 Shell、HTTP、浏览器或文件操作；
- 修改 Policy、预算、Approval 或装备权限；
- 把 Target 返回文本直接当作系统指令；
- 自行认定 Finding；
- 绕过 Evaluator、Evidence 持久化或 Policy Gate；
- 将临时模型上下文作为审计事实。

### 3.2 Evidence before claims

- Finding 必须引用已持久化 Evidence；
- Planner 的自然语言理由不是 Evidence；
- Evidence 不足或互相冲突时输出 `inconclusive`；
- 报告只能消费持久化事实，不能读取临时 Graph State 推断结论。

### 3.3 能力收益必须可测

Adaptive Mode 不能仅因为使用 LLM 就默认优于 Deterministic Mode。只有在相同快照下带来可测量的效率、覆盖或证据收益，并且成本、稳定性和误判处于可接受范围，才应推荐启用。

---

## 4. Agent 运行状态

Adaptive Run 应维护最小且类型化的 `AttackState`：

```python
class AttackState(BaseModel):
    run_id: str
    goal_id: str
    test_principal_refs: list[str]
    candidate_snapshot_id: str | None
    candidate_action_ids: list[str]
    completed_action_ids: list[str]
    denied_action_ids: list[str]
    coverage: dict[str, CoverageStatus]
    hypothesis_refs: list[str]
    observation_refs: list[str]
    finding_refs: list[str]
    budget: RunBudgetSnapshot
    consecutive_no_gain_steps: int
    repeated_state_count: int
    planner_failure_count: int
```

状态中只保存 ID、脱敏摘要、结构化统计和预算，不保存：

- Target 凭据或 Secret；
- 未脱敏的完整响应；
- Provider 客户端对象；
- Provider Instance 配置；
- 数据库 Session；
- Skill 进程内存；
- 可由 Event/Evidence 重建的重复大对象。

业务事实以数据库 Event/Evidence 为准，Graph State 只用于控制流恢复。

---

## 5. 攻击反馈闭环

每一步必须形成固定闭环：

```text
Goal + Coverage + Hypothesis
  -> Candidate Builder
  -> Planner Proposal
  -> Schema Validation
  -> Policy Gate
  -> Approval Route
  -> Harness Execution
  -> Observation Normalizer
  -> Evaluator
  -> Evidence/Finding Persist
  -> Coverage/Hypothesis Update
  -> Continue or Finish Gate
```

要求：

- Planner 只能读取脱敏、限长的 Observation 摘要；
- Target 输出、Tool Trace、RAG 文档和 Skill 文本统一标记为不可信数据；
- 不可信数据不得拼接进 system/developer instruction 区域；
- Planner Proposal 必须引用其依据的 `observation_ref` 或 `finding_ref`；
- Coverage/Hypothesis 更新必须来自已持久化事实；
- Planner 失败不能跳过执行前的确定性校验。

### 5.1 Hypothesis 生命周期

Hypothesis 是持久化评测事实，不是 Planner 的自由文本。第一版采用确定性 Hypothesis Service：

- Case Pack 声明可验证的 `hypothesis_template_id`；
- Candidate 绑定要验证的 Hypothesis；
- Evaluator 根据已持久化 Evidence 输出 `supported`、`rejected` 或 `inconclusive`；
- Hypothesis Service 根据规则更新状态并产生 Event；
- Planner 只能引用现有 `hypothesis_ref`，不能创建或改写 Hypothesis 文本；
- 新的 Hypothesis 模板只能来自版本化 Case Pack 或经过校验的 Generator Skill 输出。

如果以后允许模型提出新 Hypothesis，必须新增独立的结构化 Proposal 协议并经过 Core 校验，不得复用 Planner 的自然语言 `reason`。

---

## 6. Candidate Builder

Planner 不应面对整个装备库或任意 Action 空间。Core 先确定性生成本步候选：

1. 过滤未启用或不兼容的装备；
2. 过滤 Target/Case/Capability allowlist 外的动作；
3. 过滤预算不足的动作；
4. 过滤已完成且不允许重复的动作；
5. 标记需要审批的高风险动作；
6. 根据未覆盖风险面、前置条件和 Evidence 缺口生成候选；
7. 校验 Candidate 绑定的 Target、Test Principal、Provider Instance 和 Capability Contract；
8. 为候选附加稳定 ID、成本上界、风险等级、Hypothesis 引用和预期覆盖标签。

Planner 只能返回候选 ID，不能改写候选中的 Prompt、参数、Target、Capability 或风险等级。

企业 Skill 可以声明新的候选动作和覆盖标签，但是否进入本步候选由 Core 决定。

确定性要求：

- 相同输入状态下候选集合和顺序稳定；
- 相同优先级使用稳定 ID 决胜，不依赖集合遍历顺序；
- 每个过滤结果记录结构化原因；
- Candidate Builder 不调用 LLM。
- 每一步生成不可变的 Candidate Snapshot；Planner Decision 必须引用该 Snapshot ID，避免恢复后使用过期候选。

---

## 7. Planner 决策协议

Planner 必须输出结构化结果：

```python
class PlannerDecision(BaseModel):
    action: Literal["execute", "finish"]
    candidate_snapshot_id: str
    candidate_id: str | None
    reason_code: PlannerReasonCode
    evidence_refs: list[str]
    hypothesis_refs: list[str]
    expected_information_gain: Literal["low", "medium", "high"] | None
```

校验规则：

- `execute` 必须提供当前候选集中的唯一 `candidate_id`；
- `finish` 不得携带 `candidate_id`；
- `candidate_snapshot_id` 必须等于当前未过期的 Candidate Snapshot；
- `evidence_refs` 必须属于当前 Run 且已持久化；
- `hypothesis_refs` 必须属于当前 Run 且已持久化；
- 理由只能用于审计，不能作为授权依据；
- 非法 JSON、未知字段、越界 ID 或伪造 Evidence 引用产生 `planner_rejected` Event；
- 不允许对非法输出做宽松字符串解析后继续执行；
- Planner Prompt、模型 ID、模型参数、Schema 版本和输入事实引用必须保存快照。

Planner 不返回 `wait_for_approval`。Planner 选择一个 Candidate 后，由确定性 Policy Gate 返回 `allow`、`deny` 或 `approval_required`；只有 `approval_required` 才能创建 Approval Request。

`finish` 是建议，不是最终状态迁移。Core Finish Gate 必须校验：

- 必需覆盖项是否已经完成；
- 必需安全对照是否已经执行；
- 是否存在未解决的 Evidence 缺口；
- 是否存在仍有效的 pending Approval；
- Policy 是否允许提前结束。

Finish Gate 拒绝结束时记录 `planner_finish_rejected` Event，并重新构造候选或按停止规则结束，不能宽松接受 Planner 的结束理由。

---

## 8. 策略选择与覆盖优化

Agent 优化目标是在授权预算内提高有效风险覆盖和证据质量，而不是尽可能多发请求。

候选排序至少考虑：

- 尚未覆盖的风险类别；
- 当前假设的验证价值；
- 预期信息增益；
- 已有 Evidence 缺口；
- 执行成本和剩余预算；
- 动作风险与审批成本；
- 与最近步骤的相似度；
- 是否存在正常行为或安全拒绝对照。

约束：

- 同一 Action 的重复次数有上限；
- 新攻击变体只能来自已批准 Case Pack 或有边界的 Generator Skill；
- Generator Skill 输出仍需经过 Schema、Policy 和预算校验；
- Generator 输出必须持久化为不可变 `DerivedCase`，保存 Generator ID/version、输入事实、输出、checksum 和父 Case；
- `DerivedCase` 只有在 Deterministic Mode 中可以重放并得到充分 Evidence 后，才计入 Adaptive 的新增有效覆盖；
- 不以“更具破坏性”作为优化目标；
- Planner 不得为了提高覆盖率虚构 Finding 或 Evidence。

`expected_information_gain` 只是 Planner 的事前预测，不作为效果事实。执行后的真实收益由 Core 计算：

```text
coverage_delta
evidence_completeness_delta
confirmed_finding_delta
target_call_cost
planner_cost
duration_delta
```

---

## 9. 停止条件与循环控制

满足任一硬停止条件时，Planner 无权继续：

- `max_steps`、`max_target_calls` 或 `max_provider_calls` 达到上限；
- `max_duration_seconds` 或成本预算耗尽；
- Target 撤销授权；
- 出现 Policy 要求立即终止的事件；
- Run 被人工取消。

软停止条件：

- 所有必需覆盖项已完成；
- 连续 N 步没有新增 Coverage、Evidence 或 Finding；
- 同一标准化状态重复 N 次；
- 剩余候选均被拒绝、审批已拒绝或前置条件不满足；
- Planner 连续失败达到上限；
- Target 连续传输失败达到上限。

每次结束保存以下一种 `stop_reason`：

```text
completed
budget_exhausted
no_information_gain
loop_detected
policy_terminated
target_unavailable
planner_failed
cancelled
```

存在有效 pending Approval 时，Run 进入 `waiting_approval` 状态并保存 checkpoint，不写终止 `stop_reason`。审批过期或拒绝后重新构造候选；只有没有其他可执行候选时才按软停止规则结束。

---

## 10. 确定性降级

Adaptive Planner 不可用时，系统按照 Run 快照选择一种明确行为：

1. `fail_closed`：终止 Adaptive Run；
2. `deterministic_fallback`：按 Candidate Builder 的稳定顺序继续；
3. `pause`：保存状态并等待恢复。

禁止：

- 临时切换到未记录的模型；
- 放宽 Planner Schema；
- 绕过 Policy Gate；
- 扩大候选范围；
- 把模型超时误记为 Target 安全通过。

降级模式、原因和实际模型/Provider 必须写入 Event 和 Run 快照。

`deterministic_fallback` 仍必须使用当前 Candidate Snapshot，并逐个经过 Policy Gate、Approval Route、预算校验和 Finish Gate。

---

## 11. Evaluator 与 Judge 优化

当前字符串包含判断只能作为最小确定性规则，不能独立承担企业级判定。

```text
Transport Validation
  -> Deterministic Rules
  -> Structured Trace Checks
  -> Domain Evaluator Skill
  -> Optional Model Judge
  -> Finding Aggregator
```

要求：

- 连接错误、超时、拒绝、违规和无证据分别建模；
- 确定性规则优先于 Model Judge；
- Model Judge 只处理语义模糊场景；
- Model Judge 与 Planner 使用独立配置、预算和调用统计；
- 多个 Evaluator 冲突时不静默选择最高风险结果；
- Evidence 不足或冲突无法解决时输出 `inconclusive`；
- 每个结果保存 evaluator ID/version、规则或 Prompt 版本、Evidence 引用和置信度；
- 使用包含正常行为、安全拒绝和真实违规的校准集评估误报与漏报；
- 企业 Evaluator Skill 可以提供判断事实，但最终 Finding 聚合仍由 Core 完成。

---

## 12. Prompt 与上下文治理

Planner/Model Judge Prompt 必须：

- 在代码或受版本控制的 Core 资源中版本化；
- 明确区分可信指令与不可信 Observation；
- 对输入条数、单条长度和总 token 设置上限；
- 优先传递结构化摘要和引用，不重复发送完整历史；
- 不包含 Target Secret、认证 Header 或未脱敏 PII；
- 保存 Prompt Template 版本和内容 checksum；
- 支持根据相同快照重建模型调用输入；
- 禁止 Skill 静默修改 Core system prompt。

企业可以选择已批准的 Prompt Profile，但 Profile 必须经过版本、权限和兼容性校验。

---

## 13. Agent 模型 Provider 边界

Planner 和 Model Judge 可以通过模型 Provider 调用企业模型，但职责不能倒置：

```text
Attacker Core
  -> 构造受控 Planner/Judge 请求
  -> Policy/Budget 校验
  -> model.inference.v1 Provider
  -> 校验结构化响应
  -> Core 决定状态迁移
```

模型 Provider 只负责：

- 模型鉴权和协议适配；
- timeout 和 Contract 允许的 retry；
- usage 和 latency 归一化；
- 结构化响应传输；
- 健康检查。

模型 Provider 不负责候选构造、授权、停止条件、Finding 聚合或 Graph 路由。

每次物理模型请求都计入 Planner 或 Model Judge 的独立预算和用量。Provider 内部 retry 必须返回物理尝试次数、每次错误类别和最终用量，不能以一次逻辑调用绕过预算。

---

## 14. 可观测性

必须记录：

- planner decision count/rejection/fallback；
- candidate count 和过滤原因；
- candidate snapshot 生成、过期和拒绝次数；
- Test Principal、Tenant 和 Session scope 的覆盖统计；
- 每类风险覆盖率；
- new coverage/evidence per step；
- predicted/actual information gain difference；
- DerivedCase 生成数、确定性重放通过数和有效覆盖数；
- repeated action/state count；
- no-information-gain step count；
- adaptive/deterministic target call difference；
- planner/model-judge token、成本和延迟；
- evaluator agreement/conflict/inconclusive；
- stop reason；
- checkpoint resume count；
- 每个 Finding 的最短 Evidence 路径长度。

不得把 Prompt、Target 原始响应或 Secret 作为指标标签。

---

## 15. 实施阶段

### 15.1 Agent Core

- Run 级 `AttackState`；
- Test Principal 引用和 Candidate Snapshot；
- Candidate Builder；
- 结构化 Planner Decision；
- Finish Gate 和 Approval Route；
- Observation Normalizer；
- 确定性 Coverage/Hypothesis Service；
- 硬停止与软停止；
- 循环和无信息增益检测。

### 15.2 可信判定与降级

- 确定性 Planner 降级；
- Evaluator 冲突与 `inconclusive`；
- Prompt/模型调用快照；
- Model Judge 独立配置；
- Target 不可信内容隔离。

### 15.3 Harness 集成

- 现有 HTTP Connector 和内置 Case 先走通完整闭环；
- 第四阶段启动后，Candidate Builder 再接入已启用的 Case Pack 和 Skill Action；
- Planner/Judge 通过 `model.inference.v1` 使用模型 Provider；
- 装备输出统一进入 Observation、Evidence 和 Finding 流程。

### 15.4 效果校准

- 建立 Deterministic/Adaptive 对照运行；
- 分开衡量效率收益和发现收益；
- Generator 输出冻结为 DerivedCase，并在 Deterministic Mode 中复验；
- 建立 Judge 校准集；
- 衡量覆盖、证据、误报、漏报、成本和稳定性；
- 根据可测收益决定 Adaptive 是否可作为推荐模式。

---

## 16. 验收标准

### 16.1 安全边界

- Planner 只能选择 Candidate Builder 提供的候选 ID；
- Planner Decision 必须绑定当前未过期的 Candidate Snapshot；
- 所有 Adaptive 执行路径均经过 Policy Gate；
- Approval 只能由 Policy Gate 路由创建，Planner 不能自行请求或绕过审批；
- Planner 的 `finish` 必须经过 Finish Gate；
- Target 不可信内容不能改变 Core 指令和权限；
- Planner 非法输出不会触发 Target/Provider 调用；
- Agent 无法创建授权范围外的 Target、Test Principal、Case、Provider Instance 或 Capability。

### 16.2 闭环与恢复

- 每一步均形成 Decision、Policy、Execution、Observation、Evaluation 和 Persist 事件；
- Coverage/Hypothesis 只根据持久化事实更新；
- Planner 无法通过自然语言理由创建或改写 Hypothesis；
- 重复动作、重复状态和无信息增益均能在配置上限内停止；
- Planner 不可用时按快照配置失败、暂停或确定性降级；
- checkpoint 恢复不会重复有副作用的调用或 Finding。

### 16.3 判断可信度

- 每个 Planner Decision 可追溯到输入事实和模型快照；
- 每个 Finding 引用充分的 Evidence；
- Evaluator 冲突或 Evidence 不足时可输出 `inconclusive`；
- 校准集分别统计误报、漏报和 inconclusive；
- 模型错误、Target 错误与安全通过不会混为一类。

### 16.4 Adaptive 效果

Adaptive 效果分为两类：

1. **效率对照**：使用相同 Dataset、Target、Test Principal、Policy、Evaluator、Candidate 宇宙和装备快照；
2. **发现对照**：允许生成 DerivedCase，但新增覆盖只有在 DerivedCase 被冻结并通过 Deterministic 复验后才成立。

| 维度 | 要求 |
|---|---|
| 安全边界 | Adaptive 不得产生 allowlist 外调用 |
| 证据质量 | 对相同已执行 Case，Adaptive Evidence 完整度不得低于 Deterministic |
| 效率 | 在接近或达到同等覆盖时，衡量 Target 调用、成本和时长是否降低 |
| 发现 | 只统计通过 Deterministic 复验的 DerivedCase 新增覆盖 |
| 成本 | 保存调用数、token、时长和估算成本 |
| 稳定性 | 重复运行的差异可由模型、Prompt 和装备快照解释 |
| 降级 | 模型不可用、非法输出和超时时确定性结束或降级 |
| 循环 | 重复动作、状态和无信息增益都能触发停止 |

Adaptive 没有可测量的效率、覆盖或证据收益时，不作为默认执行模式。Planner 自报的 `expected_information_gain` 不计入收益，必须使用执行后的 Core 指标。

---

## 17. 完成标准

满足以下条件后，可认为 Attacker 已具备第一版受约束评测 Agent 能力：

1. 形成 Candidate、Plan、Policy、Approval、Execute、Observe、Evaluate、Persist、Finish Gate 和 Stop 完整闭环；
2. Planner 只能在 Core 生成的有界候选中选择，不能决定授权和事实；
3. 具备循环控制、无收益停止、确定性降级和 `inconclusive`；
4. Planner/Judge 的 Prompt、模型、参数和输入事实均有快照；
5. Target 不可信内容不能进入可信指令区域；
6. 中断恢复不重复 Target/Provider 调用或 Finding；
7. Adaptive 相对 Deterministic 的效率收益和经确定性复验的发现收益可测量；
8. Provider/Skill 可以扩展能力，但不能覆盖 Core 的授权、状态迁移和事实聚合。

---

## 18. 受约束 ReAct 架构目标

### 18.1 目标定位

Adaptive Mode 应实现受约束的 ReAct（Reasoning and Acting）闭环，使 Attacker 能够依据已持久化的 Observation、Evidence、Coverage 和 Hypothesis 事实选择下一步，并在执行后根据新增事实调整策略。

ReAct 只用于 Adaptive Mode 的战术编排。Deterministic Mode 继续保持固定数据集、固定策略和可复现执行顺序，作为回归、校准、Replay 和 Adaptive 收益对照的基线。

项目不以实现能够自由生成命令、工具调用或攻击载荷的通用 Agent 为目标。这里的 Reasoning 是对 Core 提供的有界候选进行结构化选择；Acting 是由确定性 Core 在授权、审批和预算校验通过后执行已定义的 Action。

### 18.2 闭环定义

每一轮 ReAct 必须形成以下可审计链路：

```text
Persisted Facts
  -> Build Candidate Snapshot
  -> Reason: Planner Decision
  -> Validate Decision
  -> Policy / Approval / Budget Gate
  -> Act: Deterministic Action Execution
  -> Observe: Normalize Untrusted Output
  -> Evaluate and Persist Evidence
  -> Update Coverage / Hypothesis / Information Gain
  -> Finish Gate or Next Round
```

各阶段职责如下：

- **Reason**：Planner 只能基于当前 Candidate Snapshot 和有引用的持久化事实，选择一个候选或建议结束；
- **Act**：Core 根据候选中已定义的 Capability Contract 执行动作，Planner 不直接生成或修改执行参数；
- **Observe**：Target、Tool、RAG、Skill 和 Case Pack 输出统一按不可信输入处理，经脱敏、限长和结构化后持久化；
- **Evaluate**：Evaluator 根据 Observation 和 Trace 生成判定事实；Planner 的理由和预测不构成 Evidence；
- **Adapt**：Coverage、Hypothesis 和实际信息增益只根据持久化结果更新，并作为下一轮 Planner 输入；
- **Stop**：结束建议必须经过 Finish Gate；预算耗尽、循环、重复状态、无信息增益和组件失败由 Core 的确定性规则终止或降级。

### 18.3 Reasoning 契约

ReAct 的 Reasoning 必须输出结构化 `PlannerDecision`，不得依赖、保存或展示模型的自由文本思维链。可审计信息至少包括：

- 当前 Candidate Snapshot ID；
- 选择的 Candidate ID 或 `finish`；
- 标准化 Reason Code；
- 使用的 Evidence、Observation、Finding 和 Hypothesis 引用；
- 预期信息增益；
- Prompt、模型、参数、Schema 和输入事实快照。

Planner 输出必须经过 Schema、引用、快照有效期、重复次数和候选归属校验。任何非法、过期或越界输出都不能触发 Act，并按照确定性失败、重试、暂停或降级策略处理。

### 18.4 Acting 与安全边界

ReAct 不扩大既有授权边界：

- Planner 不能创建 Candidate、Target、Test Principal、Provider Instance、Capability、Policy 或 Approval；
- 所有动作在执行前重新经过 Policy Gate、预算校验和必要的 Approval Route；
- Planner 不能直接执行 Shell、HTTP、浏览器、文件或 Provider 调用；
- Observation 中的指令性内容不能改变 Core Prompt、候选范围、权限、预算或停止规则；
- 具有副作用的动作必须使用稳定 Operation ID，并保证 checkpoint 恢复时不会重复执行；
- Finding、Coverage 和 Hypothesis 的最终状态只能由确定性 Core 根据已持久化事实聚合。

### 18.5 ReAct 收益与启用条件

受约束 ReAct 的目标是在不降低安全边界、证据质量和可恢复性的前提下，提高以下至少一项能力：

1. 以更少的 Target 调用、成本或时间达到接近或相同的有效覆盖；
2. 根据中间 Evidence 优先验证高价值 Hypothesis，减少无收益步骤；
3. 发现固定 Case 顺序难以覆盖的有效攻击路径，并通过 DerivedCase 的 Deterministic 复验确认新增覆盖；
4. 在审批拒绝、能力不可用或证据不足时切换到仍有收益的候选，而不是盲目重复。

Adaptive ReAct 不因“使用了 LLM”而默认启用。只有在相同快照和安全约束下，相对 Deterministic 基线表现出可重复、可解释的净收益，才可作为推荐模式；否则保持可选、实验或关闭状态。

### 18.6 ReAct 验收标准

- 至少两个连续步骤能够证明后一轮 Planner 输入包含前一轮持久化产生的 Observation、Evidence、Coverage 或 Hypothesis 变化；
- 每个 Planner Decision 均能追溯到有效 Candidate Snapshot、输入事实引用和模型调用快照；
- Planner 不能通过自然语言输出创建动作、修改参数、扩大权限或直接写入安全事实；
- 所有被执行动作均经过 Decision 校验、Policy Gate、预算校验和必要审批；
- 不可信 Observation 只能进入数据区域，不能改变可信指令和 Core 状态迁移规则；
- 重复动作、重复状态、无信息增益、预算耗尽和 Planner 失败均能在配置上限内确定性停止或降级；
- checkpoint 恢复后不会重复具有副作用的 Act，也不会重复生成 Evidence 或 Finding；
- Planner 建议结束但 Coverage、Control、Evidence 或 Approval 条件未满足时，Finish Gate 能拒绝结束并继续构建候选；
- Adaptive ReAct 与 Deterministic 的效率、发现、成本、稳定性和证据质量可以使用持久化指标进行对照；
- ReAct 新增发现只有在 DerivedCase 冻结并通过 Deterministic 复验后才计入有效覆盖。
