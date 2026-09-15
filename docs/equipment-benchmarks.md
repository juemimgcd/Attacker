# Equipment 自定义 Benchmark

Benchmark 是 Equipment 的一种数据包。测试集、目标绑定、任务流程、判分规则都在
Equipment 侧定义；Core 只解析统一契约，通过 Harness 调度 Skill、保存证据和聚合指标。
本功能的入口是 `attacker equipment benchmark`。

## 组件边界

| 组件 | 负责内容 |
| --- | --- |
| `benchmarks/<id>/benchmark.yaml` | 选择 CasePack、Skill、目标绑定，声明指标口径与执行参数 |
| `casepacks/<id>/casepack.yaml` + `tasks.yaml` | 任务输入、预期结果、标签；使用 `casepack.v2` |
| `providers/<id>/` 和 Provider Instance | 连接目标、适配协议、读取轨迹、管理资源；沿用现有能力契约 |
| `skills/<id>/` | 环境准备、任务调用、状态验证、判分及指标归一化 |
| Core | 版本冻结、能力门禁、并发与时间预算、SQL Run/Step/Event、资源租约清理和报告 |

同一 CasePack 可以组合不同 Provider；同一 Provider 可以运行不同 Benchmark。
Core 没有根据 benchmark 名称选择判分器或解析某家 Agent 的输出。
现有 `casepack.v1` 继续供原来的安全用例流程使用，不自动转换成任务数据集。

## 开始使用

仓库提供三个默认不启用的可编辑装备：

- `equipment/benchmarks/agent-task-benchmark/benchmark.yaml`
- `equipment/casepacks/agent-task-examples/`
- `equipment/skills/agent-task-evaluator/`

示例目标 `development` 绑定 `http-agent-dev`，它是现有的占位地址，必须换成实际部署
的 Provider Instance 才能运行真实评测。实例应满足其 Provider 的网络允许列表、密钥
引用及启用要求。目标配置留在 Provider Instance，Benchmark 只引用实例 ID。
需要签名的部署仍需为这些扩展签名；`trusted_enterprise` 不会绕过现有供应链校验。

从仓库目录执行：

```sh
uv run attacker equipment reload
uv run attacker equipment enable agent-task-examples --type casepack
uv run attacker equipment enable agent-task-evaluator --type skill
uv run attacker equipment enable agent-task-benchmark --type benchmark

# 完整预检：解析已启用的包、任务、输入 schema 和目标绑定，不调用目标。
uv run attacker equipment benchmark validate agent-task-benchmark --target development

# 配置真实目标后执行；JSON 报告包含 run.id。
uv run attacker equipment benchmark run agent-task-benchmark --target development
uv run attacker equipment benchmark report RUN_ID
uv run attacker equipment benchmark report RUN_ID --format markdown

# 新建组合配置，引用现有 CasePack 和 Skill。
uv run attacker equipment scaffold benchmark my-benchmark
```

`equipment validate PATH --type benchmark` 只校验单个包；
`equipment benchmark validate ID --target NAME` 才做跨包预检。
`--task-id ID` 可以重复传入，按数据集原顺序选择子集；空选择、重复 ID 和不存在的 ID
均会被拒绝。`--version VERSION` 选择 Benchmark 版本，CasePack 与 Skill 版本由包内固定。
修改已注册包后必须增加其版本，同版本不同内容仍会被拒绝。

CLI 的 Markdown 输出采用 `{"markdown": "..."}` JSON 对象，沿用现有 CLI 输出契约。

## Benchmark 定义

```yaml
schema_version: benchmark.v1
id: my-benchmark
name: Order Agent benchmark
version: 1.0.0
description: Evaluate order tasks and execution efficiency.
attacker_compatibility: {min_version: 0.1.0, max_version: 0.x}
casepack: {id: my-order-tasks, version: 1.0.0}
skill: {id: agent-task-evaluator, version: 1.0.0}
targets:
  candidate:
    bindings: {target: my-agent-instance}
    parallel_safe: false
execution:
  concurrency: 1
  repetitions: 3
  timeout_seconds: 120
  max_steps: 3
  max_provider_calls: 1
metrics:
  - name: total_tokens
    unit: tokens
    aggregation: sum
    description: Target input and output tokens including model request retries.
```

任务格式是 `tasks: [{id, payload, expected, tags}]`；`payload` 和 `expected` 的业务结构
由 Skill 自己的 input schema 定义。Core 仅验证任务 ID 唯一及包文件边界。
`equipment/schemas/` 提供 Benchmark、任务集、评测输出的 JSON Schema。

目标 binding 名称必须与 Skill 的 `requires.capabilities[].binding` 完全一致。
使用高风险能力时，由调用方通过 `--approve-capability CONTRACT` 或 API 请求的
`approved_high_risk_capabilities` 显式提供允许范围，Benchmark 包不能自己授予能力。

每次运行冻结 Benchmark、CasePack、Skill、Provider、Contract 的内容校验和，以及
Provider Instance 的 config/secret revision。每次任务尝试有独立 operation ID、工作目录；
示例 Skill 将这个 ID 作为 `metadata.benchmark_session_id` 传给目标。目标适配器需要真正
实现会话和环境隔离。只有目标配置声明 `parallel_safe: true` 才允许并发大于 1。

`max_steps` 限制的是 Equipment Skill 的续执行次数，`max_provider_calls` 限制的是
Harness 的能力调用次数；它们不是被测 Agent 的循环次数或 token 上限。
目标内部预算应由其 Provider/任务协议实现。单次任务时间限制受 Benchmark、Skill、
Provider、Contract 各自上限共同约束。

## 自定义 Skill 输出

Skill 仍使用现有 `prepare(context)`、`execute(payload, context)`，通过
`CapabilityRequest` 请求能力并在下次调用获得 `capability_results`。
初始 payload 是 `{"task": {...}, "metric_names": [...]}`。
成功结束时返回 `SkillResult(status="success", output={"benchmark": ...})`：

```json
{
  "outcome": "passed",
  "reason": "Order A now has the expected delivery address",
  "evidence": {"verification_operation_id": "..."},
  "metrics": {
    "total_tokens": {"value": 320},
    "tool_call_accuracy": {"numerator": 2, "denominator": 3},
    "loop_count": {"value": 4}
  }
}
```

`BenchmarkEvaluation`、`MetricSample` 可从 `app.equipment.sdk` 导入。
输出必须有非空 reason 和 evidence；只返回本次 `metric_names` 中声明的指标。
缺失指标应省略，不能填 0。数值必须有限且非负；比例需提供正分母和不超过分母的分子。
声明的聚合方式与样本形态不一致时，该次尝试记为 error。

判分状态：`passed`、`failed`、`inconclusive`（缺少决定性证据）、`invalid`（任务或
环境无法形成有效评测，必须说明原因）。调用失败、超时、策略拒绝由 Harness 单独记录。
任务失败不会被计为安全漏洞，也不会生成 Finding。

## 指标口径

- **任务完成率**：passed / 已结束且非 invalid 的尝试数。error、timeout、denied、
  inconclusive 都保留在分母，invalid 单列。未开始或未结束的任务单列 pending，部分运行
  的完成率仅代表已结束的尝试。每次 repetition 是一次独立尝试。
- **完成时间**：示例 `duration_ms` 为 Harness 测量的目标能力调用耗时，包含传输和
  broker 开销，不是模型纯推理时间。`harness_duration_ms` 单独记录整体 Skill 执行耗时。
  失败调用没有完整观测时不推测时长；`scope: passed` 可只统计成功任务。
- **Token**：被测 Agent 的 input/output/total，包含其模型请求重试；两项 usage 都存在
  时才得到 total。Attacker 自身规划/评分消耗不能混入。
- **工具准确率**：示例规则精确匹配工具名和参数；汇总为 sum(numerator) /
  sum(denominator)，不平均每题百分比。缺少规则、缺少轨迹或没有调用时不产生样本。
  调用成功与调用正确不同；示例准确率不代表必需操作覆盖率，也不用于强制唯一调用顺序。
- **平均循环次数**：示例口径为 Agent 模型决策轮数，包含最终回答轮，排除传输重试。
  必须由目标上报；不能拿 Harness 的 Skill 轮数代替。

每项指标都附带 observed / eligible / coverage。标量聚合支持 mean、sum，并附带
total、mean、P50、P95（nearest-rank）；比例按分子分母加总。支持添加自定义非负指标，
其名称、单位、scope、说明和聚合方式由 Benchmark 声明，样本由 Skill 生成。
跨 Agent 对比时应使用一致的任务、判分器版本、指标定义和运行条件。

示例 HTTP 适配沿用 `agent.invoke.v1`，可返回：

```json
{
  "response": "42",
  "metadata": {
    "benchmark": {
      "input_tokens": 120,
      "output_tokens": 5,
      "loop_count": 1,
      "tool_calls": []
    }
  }
}
```

示例 Skill 支持以 JSON Pointer 对完整响应做 `equals`、`contains`、`exists` 检查。
工具规则可放在 `expected.tool_calls: [{name, arguments}]`。数值和轨迹是目标上报数据，
示例不会独立证明其真实性。验证环境最终状态、必需操作覆盖、复杂工具序列或模型评分时，
应增加/更换 Equipment Skill 和它需要的 Provider 能力，不修改 Core 聚合器。

## API 与执行边界

- `GET /equipment/benchmarks`、`GET /equipment/benchmarks/{id}`
- `POST /equipment/benchmarks/{id}/enable`、`disable`
- `POST /equipment/benchmarks/{id}/validate`、`run`，body 至少为 `{"target":"development"}`
- 沿用 Run 报告服务，结果增加 `benchmark_summary`；CLI report 可重新读取 SQL 事实。

第一版 CLI/API 都等待本次运行结束，不接入后台 Job Worker。单个任务错误保留后继续，
调度失败或取消终止本次运行；任务资源按已有 Resource Lease 协议清理，最终再检查残留，
清理失败写入证据。进程被强杀时，已落库的尝试和冻结绑定保留，但未结束的运行不会自动
重新执行；现有租约恢复机制可继续处理清理。没有引入新的 SQL 表或迁移。

本次验证：Ruff、格式和 Pyright 通过；隔离 SQLite 的装备 reload、enable 和完整
benchmark validate 通过（0 次外部调用）。现有测试 126 通过，1 个目录数量断言仍预期
只有四类包以及旧的 Skill/CasePack 数量，与新增装备不符；本次没有修改测试文件。
尚未用真实 Agent 跑端到端评测，示例不是性能或能力测量结果。
