# 配置驱动的并发 Agent 业务测试

Attacker 可以根据场景模拟用户，与目标 Agent 连续对话，批量重复真实业务流程，
然后根据声明的成功标准输出逐次证据和 CI 报告。该入口与原有攻击评测并存，
不会把正常业务测试计入安全 Finding 或漏洞数量。

## 启动

复制 [完整配置示例](examples/business-tests.yaml)，替换目标地址、模型、场景、验收端点，
并在运行环境设置配置所引用的凭据环境变量。文件只保存环境变量名称，不填写凭据值。

```bash
uv run attacker business-test validate --config docs/examples/business-tests.yaml
uv run attacker business-test validate --config my-suite.yaml --check-env
uv run attacker business-test run --config my-suite.yaml --output data/business-tests
```

`validate` 只检查配置，不发送网络请求；`--check-env` 同时检查凭据变量是否存在。
`run` 在连接目标之前检查所有凭据引用。一次命令启动整批任务，不需要人工逐轮输入。

首版入口是 CLI/YAML，没有新增 Web 表单或业务测试 HTTP 路由。

## 外部接入契约

目标、准备、验收和清理端点均由配置明确声明，使用 GET 或 POST。不会执行配置中的
Python、shell 或任意模板表达式，也不会从目标返回内容中发现新的请求地址。

| 方式 | 配置 |
| --- | --- |
| 同步 JSON | `target.protocol: json`，`response_text_pointer` 提取回复 |
| 直接 SSE | `target.protocol: sse`，配置 `stream`，回复从标准化结果 `/text` 提取 |
| 提交后读取结果流 | `target` 接收提交结果，`target.completion` 使用其中的任务 ID 请求 SSE |

[异步 SSE 接入片段](examples/business-target-async-sse.yaml) 展示 AtlasClaw 风格的
`POST /agent/run` → `GET /agent/runs/{run_id}/stream`。它是配置片段，不是可直接运行的完整测试集。
接口路径参照本地源码，部署前缀、认证、合法 session key 和业务验收接口仍须由部署方配置；
没有据此宣称已经完成 AtlasClaw 实际业务联调。

SSE 按事件名提取文本，支持追加增量和替换全文两种模式。只在配置声明的结束信号出现后完成；
错误事件、失败终态和没有结束信号的断流均记为执行错误。响应和结果流上限为 1 MiB。
SSE 的完整事件保存在任务证据中，标准化结果结构为 `text`、`events`、`terminal`。
`target.response_text_pointer` 从 completion 结果取文本；`session_response_pointer` 从提交响应取会话 ID。
`done_pointer` 必须指向布尔值，只能用于目标明确表示**整个测试任务完成**的字段，
不能拿“一轮生成结束”代替“全部用户需求完成”。通常省略它，由模拟用户推进后续对话。

HTTP Target 延续 Core 的本地/内网地址限制和 DNS 固定机制，不跟随重定向。
该 CLI 尚未接入公网 Target 的 Provider Instance 授权绑定，因此公网业务目标会在校验时拒绝，
不会绕过既有授权边界。模拟用户和评判模型复用现有 OpenAI-compatible 模型 Provider，
使用完整 chat-completions endpoint 和 JSON-object 输出；无需新增依赖。
轮询任务接口、WebSocket、任意自定义鉴权流程尚未支持。

### 请求字段映射

占位符用于配置值，完整占位符保留 JSON 类型，嵌入字符串时只接受标量：

| 占位符 | 含义 |
| --- | --- |
| `${run_id}` / `${task_id}` / `${scenario_id}` | 本批次、独立尝试、场景标识 |
| `${session_id}` | 每次尝试独立的会话标识 |
| `${messages}` / `${query}` | 本次会话历史 / 当前用户输入 |
| `${facts}` | 本场景模拟用户信息 |
| `${setup}` | 准备端点的 JSON 结果 |
| `${target}` | 最新目标结果；completion 调用时是提交响应 |
| `${facts#/project}` | 用 RFC 6901 JSON Pointer 选择子字段 |

会话目标必须绑定 `${messages}`，或者同时绑定 `${session_id}` 和 `${query}`。
支持嵌套消息字段，不局限于顶层 `messages`。
`path_suffix: '/${target#/run_id}/stream'` 只向固定端点追加路径，绑定值按单个路径片段编码。
凭据不经该机制插值；通过 `credential_env`、`credential_header`、`credential_prefix` 配置。
每个端点分别声明自己的凭据引用，completion 不会自动继承提交端点的认证。

若服务端要求先创建会话，在每个场景声明 `setup`，并设置
`target.session_setup_pointer: /session_key`。执行器会把返回值设置为 `${session_id}`。
也可以用 `session_response_pointer` 从第一轮响应取得服务端会话 ID；后续不得改变。
发现不同尝试返回同一会话 ID 时，本次尝试失败。外部仍需按任务/会话标识隔离数据库和业务资源，
Attacker 不能仅凭唯一会话 ID 保证外部平台的租户隔离。

## 场景、自动对话与评判

`task` 是首轮用户任务，`user_facts` 提供用户知道的信息和后续动作要求。
模拟用户使用独立模型上下文，输出 `reply`、`finish` 或 `blocked`；缺少必要信息时应停止，
不能编造参数或修改验收标准。它没有直接访问目标工具或执行平台操作的权限。

每个场景必须有非空 `criteria`。评判模型和模拟用户分开配置、分开调用。
规则评判优先执行，语义评判只接收对应标准指定的证据：

| 字段 | 含义 |
| --- | --- |
| `source: conversation` | 完整对话；仅支持语义评判 |
| `source: target` | 最后一轮目标返回的 JSON 或标准化 SSE 结果 |
| `source: verification` | 场景验收端点返回的 JSON |
| `evaluator: semantic` | 按自然语言 `description` 评判，并要求引用相应证据 |
| `evaluator: equals` | 比较 `pointer` 处的值和 `expected`；区分布尔、数字、字符串类型 |
| `evaluator: contains` | 字符串包含、数组成员或对象键存在 |
| `evaluator: exists` | 字段存在；字段为 null 仍表示存在 |

真实创建成功、规格正确、没有重复写入等业务标准应使用 verification 或可审计的 target 证据。
对话中的“已经成功”不能证明后端状态。语义评判仍可能误判，应检查失败证据并校准成功标准；
更换模型或评判配置时，用新批次记录，不能混为同一个实验。

缺少证据返回 `inconclusive`；`exists` 在已经取得响应、但指定字段缺失时返回失败。
模型遗漏标准、重复标准 ID、引用其他标准的证据或返回不合法结构，均不会被当作通过。
每个任务输出 `passed`、`failed`、`error`、`inconclusive` 或 `cancelled`。

## 调度、限制与清理

`concurrency` 是同时执行的独立测试会话数。场景按配置顺序循环分配，直到 `max_tasks`
耗尽；也会受 `run_timeout_seconds` 和 `max_calls` 限制。任务总量必须覆盖所有场景至少一次。
每个任务另有 `max_turns`、`task_timeout_seconds` 和模型输入字节上限。

执行槽使用 asyncio，主要等待目标和模型 API，不为每个测试 Agent 新建线程。
业务执行器不自动重发 HTTP 请求，模型 Provider 的物理尝试数也固定为 1。
网络错误、HTTP 429 和业务不符合标准分别留下证据，不会通过重试隐藏波动。

`max_calls` 计入准备、目标、completion、模拟用户、评判和清理调用。
配置了清理的任务在开始时预留一次清理调用预算。计数代表已领取的调用尝试上限，
包含可能在参数渲染或连接前失败的尝试，不等同于目标已成功收到的请求数。

准备或业务执行失败、任务超时、批次取消后，仍尝试声明的清理端点。
清理受其自身超时约束，因此总运行时长可能超过运行期限一个清理超时窗口。
清理失败会单独留在事件中，并将最终任务标记为 error，原验收结论仍保存。

HTTP 客户端取消不保证远端任务停止。异步业务的清理端点需要在外部完成任务终止/收敛及资源清理，
否则可能发生清理之后远端继续写入。SIGKILL、断电或进程崩溃也不能保证执行清理。
不要直接使用生产资源进行高并发创建测试；通过外部测试环境及 namespace 隔离重复运行。

## 报告与流水线

每次运行生成新的 `business-<uuid>/` 目录：

- `manifest.json`：配置快照及 SHA-256、目标版本、提示词版本。快照会脱敏，重跑仍使用原配置和凭据引用。
- `task-00001.json` 等：逐轮对话、端点返回、模型决策与用量、逐条验收结论、清理结果。
- `report.json`：整批及分场景结果计数、调用数量、每次尝试的证据文件索引。
- `junit.xml`：可由 Jenkins 等 CI 读取。

每个调用开始和结束时原子替换任务记录；中断前已经写出的证据不会被后续批次覆盖。
正常取消会生成部分报告，硬中断可能只有 manifest 和各任务的最后记录。
当前批处理使用文件证据，不注册到原 SQL Run/Job 表和 `/runs/{id}/report`，
不提供进程重启后的自动续跑，也不套用原 Durable Job 的自动重试，以免重复业务写入。

退出码：`0` 表示配置验证成功或整批全部通过；`1` 表示批次存在任何未通过结果；
`2` 表示配置、IO 或执行器错误；Ctrl-C 返回 `130`。
`inconclusive` 和未执行完的任务在 JUnit 中映射为 error，不作为通过或跳过。

Jenkins 的 stage 可以直接运行命令，并在 `post { always { ... } }` 中收集产物：

```groovy
stage('Agent business acceptance') {
  steps {
    sh 'uv run attacker business-test run --config my-suite.yaml --output data/business-tests'
  }
  post {
    always {
      junit testResults: 'data/business-tests/business-*/junit.xml', allowEmptyResults: false
      archiveArtifacts artifacts: 'data/business-tests/business-*/*.json', allowEmptyArchive: true
    }
  }
}
```

CI 每次构建使用独立工作目录或独立输出路径，避免把历史报告计入本次统计。
重复执行同一配置会生成新批次，原来的偶发失败记录仍然保留。
