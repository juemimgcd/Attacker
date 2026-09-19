# Equipment 自定义 Benchmark

Equipment 的新评测扩展是独立的 `benchmark.v2` 可执行包。测试集、目标配置、接入协议、
准备与清理、判分规则和指标定义都在包内；Core 负责加载、冻结版本、受限调度和结果持久化。
新执行路径不需要注册 Provider、Skill、CasePack 或能力契约。

## 包与 Core 的边界

| 包内文件 | 职责 |
| --- | --- |
| `benchmark.yaml` | 入口、任务文件、目标配置、指标口径、并发与时间预算 |
| `tasks.yaml` | 任务 ID、输入 payload、预期结果 expected、标签 |
| `task.schema.json` / `target.schema.json` | 包自定义的任务结构与目标 config 结构 |
| `benchmark.py` | prepare、execute、evaluate、cleanup 四个异步方法 |
| `requirements.lock` | 扩展依赖声明；加载流程不会自动联网安装 |

Core 不解释业务答案，也不根据包名选择目标适配器。更换业务任务、Agent 协议、最终状态
验证或评分方式时，修改 Benchmark 包即可。旧安全测试保留兼容代码，使用独立的旧入口。

## 本地使用

示例位于 `equipment/benchmarks/agent-task-benchmark/`，默认未启用，目标是占位 HTTPS
地址。先修改目标与测试内容，再注册；注册后改内容必须增加版本，同版本 checksum 冲突
会被拒绝。示例通过 HTTP 调用 Agent，不依赖其他装备。

```sh
uv run attacker equipment reload
uv run attacker equipment enable agent-task-benchmark --type benchmark
uv run attacker equipment benchmark validate agent-task-benchmark --target development
uv run attacker equipment contract-test equipment/benchmarks/agent-task-benchmark --type benchmark

# 配置真实目标后执行，返回含 run.id 的 JSON 报告。
uv run attacker equipment benchmark run agent-task-benchmark --target development
uv run attacker equipment benchmark report RUN_ID
uv run attacker equipment benchmark report RUN_ID --format markdown

# 生成含任务、schema、HTTP 接入和评分代码的完整包。
uv run attacker equipment scaffold benchmark my-benchmark
```

`equipment reload` 和 `equipment list` 默认面向 Benchmark；旧安全装备发现使用
`equipment reload --legacy`。`equipment validate PATH --type benchmark` 校验包文件，
`equipment benchmark validate ID --target NAME` 还校验已启用版本、任务 schema、目标配置
和密钥引用，均不调用目标。`contract-test` 检查生命周期方法签名，不等于业务运行成功。
`--task-id ID` 可重复传入以选子集；`--version VERSION` 选择包版本。
CLI Markdown 报告沿用 `{"markdown":"..."}` JSON 输出形式。

## 清单与目标

```yaml
schema_version: benchmark.v2
id: my-benchmark
name: Order agent evaluation
version: 1.0.0
description: Evaluate order tasks and execution efficiency.
attacker_compatibility: {min_version: 0.1.0, max_version: 0.x}
trust_level: trusted_enterprise
entrypoint: benchmark.py:OrderBenchmark
tasks_file: tasks.yaml
task_schema: task.schema.json
target_schema: target.schema.json
targets:
  candidate:
    config: {endpoint: 'https://agent.internal.example/invoke'}
    secret_refs: {}
    allowed_hosts: [agent.internal.example]
    parallel_safe: false
execution:
  concurrency: 1
  repetitions: 3
  timeout_seconds: 120
  cleanup_timeout_seconds: 30
  max_output_bytes: 1048576
metrics:
  - name: total_tokens
    unit: tokens
    aggregation: sum
    description: Target input and output tokens including retries.
```

任务格式为 `tasks: [{id, payload, expected, tags}]`。目标的非敏感参数放在 `config`，
凭证只配置 `secret_refs`，由已有 SecretBroker 解析，不能将明文密钥写进配置。
运行时选中的目标及包 checksum、任务快照写入 Run；一个包的所有文件一同归档。

## 生命周期

从 `app.equipment.benchmark_sdk` 导入以下返回类型。方法参数都是可序列化字典，
每个阶段在独立调用中运行，跨阶段状态必须显式通过返回值传递，不能依赖实例内存。

| 方法 | 返回 | 调用含义 |
| --- | --- | --- |
| `prepare(self, task, context)` | `BenchmarkPreparation` | 准备环境，返回 ready/reason/state |
| `execute(self, task, state, context)` | `BenchmarkObservation` | 调用目标，返回 status/output/evidence/metrics |
| `evaluate(self, task, observation, context)` | `BenchmarkEvaluation` | 根据任务与观测返回 outcome/reason/evidence/metrics |
| `cleanup(self, state, context)` | `BenchmarkCleanup` | 清理当前任务资源，返回 cleaned/reason |

`context` 包含 run_id、operation_id、task_id、repetition、target_name、target_config、
allowed_hosts、secret_names、workspace_path、timeout_seconds、metric_names 和测试身份引用。
需要凭证的阶段通过 `benchmark_secret("api_key")` 读取选中目标的已授权凭证；凭证仅注入
prepare/execute/cleanup 的子进程环境，不注入 evaluate，也不进入 JSON 上下文和报告。

prepare 返回未就绪时记为 invalid，仍执行 cleanup。execute 非 success 时保留观测并
直接记录 error/timeout/denied，不再评分。prepare 尝试后无论成功或异常都尝试 cleanup；
cleanup 有独立预算，失败会保留证据，并将正常结束的 Run 标记为 cleanup_failed。

准备状态、执行观测和清理结果分别写入 SQL Step/Event，评分失败仍保留已写入的观测。
评分结果示例：

```json
{
  "outcome": "passed",
  "reason": "Order address matches the expected state",
  "evidence": {"verified_order_id": "A"},
  "metrics": {
    "total_tokens": {"value": 320},
    "tool_call_accuracy": {"numerator": 2, "denominator": 3},
    "loop_count": {"value": 4}
  }
}
```

评分状态包括 passed、failed、inconclusive、invalid，reason 和 evidence 必须非空。
只允许输出清单声明的指标；缺失数据省略，不能填零。标量必须非负且有限，比例必须有
正分母和不超过分母的分子。形态不符合声明时该次尝试记为 error。

## 受约束的编排

Core 先校验并冻结单个包，再将选中的任务展开为 repetitions 次尝试，交给固定数量的
并发 worker。每次尝试拥有独立 operation ID 和工作目录。只有目标声明 parallel_safe
才允许并发大于 1；这项声明不会自动隔离外部 Agent 的会话和环境，隔离需要包实现。
示例将 operation ID 传入 HTTP 请求 metadata.benchmark_session_id。

prepare、execute、evaluate 共用单次任务的 timeout_seconds，cleanup 使用独立上限。
各阶段输出受 max_output_bytes 限制。Core 不以旧的 Skill 轮数、能力调用预算约束新包；
被测 Agent 内部的 token、工具次数和循环预算需要通过包的目标协议设置。

trusted_enterprise 包使用 JSON 子进程执行。它提供崩溃、超时和协议隔离，**不是恶意代码
安全沙箱**。示例复用允许列表、DNS 地址固定及有限响应读取的网络工具；任意自定义代码
不会仅因声明 allowed_hosts 就被操作系统限制网络。包仍受现有签名、信任和路径校验。

## 指标口径

- **任务完成率**：passed / 已结束且非 invalid 的尝试数。error、timeout、denied、
  inconclusive 保留在分母，invalid 和 pending 单列；每次 repetition 独立计数。
- **耗时**：示例 duration_ms 计量目标 HTTP 调用及响应处理，不含准备、评分、清理。
  Core 另存 benchmark_duration_ms 和 phase_durations_ms，不能混为模型推理耗时。
- **Token**：目标上报 input/output，包含目标模型重试；两项都存在才计算 total。
- **工具准确率**：示例匹配期望工具名及完整参数，汇总分子/分母，而非平均各题百分比。
  没有规则、轨迹或调用时不产生样本；此指标不等于必需操作覆盖率。
- **平均循环次数**：目标上报模型决策轮数，含最终回答轮，排除传输重试。

指标返回 observed、eligible、coverage。标量支持 mean/sum，并提供 total、mean、P50、
P95（nearest-rank）；比例累加分子分母。指标名、单位、scope 和说明由包声明，样本由包
产生。缺失观测不会被当成零；`scope: passed` 可只统计完成任务。跨 Agent 对比必须固定
任务、包版本、指标口径及运行条件。

示例预期目标返回 `response` 字符串，可在 `metadata.benchmark` 中提供 input_tokens、
output_tokens、loop_count、tool_calls。任务 expected.checks 支持对完整响应做 JSON
Pointer 的 equals/contains/exists 检查；expected.tool_calls 可定义工具名与参数规则。
这些遥测来自目标，示例不会独立证明其真实性；业务最终状态验证应在包内实现。

## API、迁移与当前边界

接口为 `/equipment/benchmarks` 及其 `{id}`、`{id}/enable`、`disable`、`validate`、`run`。
validate/run 的请求至少包含 `{"target":"development"}`。CLI/API 等待运行结束，结果
通过已有 Run 报告服务提供 benchmark_summary，没有接入后台 Job Worker。

旧组合式 benchmark.v1 被明确拒绝，需将任务、目标配置、执行与判分迁入 benchmark.v2；
不能直接改 schema_version 继续引用旧包。旧安全评测保留 Provider/Skill/CasePack v1
兼容入口，但新 Benchmark 不解析或调用它们。旧 Run 的历史报告仍可读取。

进程被强杀时，已写入的包快照与阶段证据保留；未完成运行不自动恢复，外部环境清理也不
保证自动接续。包应采用可重入清理和可识别的任务资源标记。示例 HTTP 包没有创建可管理
资源，cleanup 返回成功不代表已清理远端 Agent 的内部状态。
