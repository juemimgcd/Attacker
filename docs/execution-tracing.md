# 黑盒 Agent 的模型交互路径观测

主入口是 `attacker trace proxy`：把目标 Agent 的模型 base URL 指向本地代理，
Attacker 在模型通信边界采集消息上下文、工具定义、模型生成的工具调用及参数、
后续请求回传的工具结果和模型回复。无需导入目标代码或知道其函数名，不限定 Agent 框架
或编程语言。目标必须允许配置模型地址，且使用本版支持的 Chat Completions 协议。

## 主入口：启动通用模型代理

```sh
uv run attacker trace proxy \
  --upstream-base-url https://api.openai.com/v1 \
  --output-dir data/traces/proxy \
  --port 8787
```

将目标的模型 base URL 改为 `http://127.0.0.1:8787/v1`，保留原有模型名称和 API Key。
例如使用 `OPENAI_BASE_URL` 配置的目标，可在启动目标的环境中设置该变量；具体配置入口
取决于目标，代理本身不依赖它使用哪个 SDK。`--upstream-base-url` 是原始模型服务 base URL，
包含服务要求的 `/v1` 等前缀。实际转发到该 URL 下的 `/chat/completions`。

代理只监听本机，支持普通 JSON 和 SSE 流式响应。它直接转发原请求正文，不替换模型、
不重新生成回答、不自动重试、不执行工具。请求 URL 的查询参数也按原始编码转发，
观测副本按字段脱敏记录。Authorization 及必要的协议请求头传给固定上游，
凭据不写入轨迹；禁止自动跟随上游重定向。请求最大 8 MiB。
请求头仅转发 Authorization、Content-Type、Accept、OpenAI-Organization、OpenAI-Project、
User-Agent 和 Idempotency-Key；依赖其他厂商专用请求头的服务尚未支持。
不转发 Cookie/Set-Cookie，避免共享代理连接池在不同调用者间传播会话状态。

```sh
# 查看全部捕获，按会话分组；也可指定单个 JSONL 文件。
uv run attacker trace show data/traces/proxy
```

每个模型请求独立保存一份文件。查看结果包含：

- `request.messages` / `request.tools`：模型实际收到的上下文和可用工具（脱敏副本）。
- `model_requested_tools`：模型提出的工具调用，包含调用 ID、工具名和参数。
- `agent_reported_tool_results`：后续模型请求尾部携带的工具结果。
- `response`：普通响应或由 SSE delta 重建的回复；多个 choice 和并行工具按 index 区分。
- `parent_request_ids`、`session_id`、`association`：协议关联依据。
- 状态、耗时、采集错误、截断和不完整流标记。

流式内容边接收边转发，观测副本在请求结束或中断后重建，再脱敏落盘。
看到完整的 SSE 结束事件时即登记工具调用引用，支持客户端读取结束标记后立即发起下一轮；
下游断开时仍执行轨迹收尾和上游连接关闭。不完整的 SSE 帧及未支持的 delta 字段会标记缺口。
默认响应采集上限为 1 MiB，可通过 `--max-capture-bytes` 调整；达到上限继续转发响应，
仅将采集结果标记为不完整。上游 HTTP 错误保持原状态码和正文；连接失败返回 502。
采集目录不能创建文件时，在调用上游前返回 503。

### 多次模型请求如何关联

优先使用目标可配置的 `X-Attacker-Trace-ID` 请求头。同一个 Query 的所有模型请求应使用
同一个值，新的 Query 使用新的值；该头只由代理消费，不发往模型服务。代理把它与
凭据/项目隔离范围做带随机密钥的散列，不直接将用户提供的值用作文件路径。

没有该头时，只有在当前尾部工具结果的 `tool_call_id` 以及历史中对应的工具调用内容
能够匹配到代理此前直接观察到的唯一模型响应时，才继承会话并建立父请求引用。
重复 ID/内容、来源冲突或未观察到上游请求时保留为独立请求，不根据相同 Query 文本猜测。
关联缓存最多保存 10,000 个调用，重启后清空；目录查看使用已记录的关联事实。
文本相同、重试、历史压缩、多个子 Agent 或并发分支都不自动等同于同一个执行轮次。
展示顺序是观测时间，依赖关系以引用为准。

### 观测范围

代理轨迹统一标记 `source=model_proxy`、`coverage=model_protocol_only`。
它能证明代理看到了哪些模型通信；`tool_execution_observed` 始终为 false。
模型请求调用某工具，不证明工具执行了；Agent 回传结果，不证明远程系统真的产生了副作用。
原始 Query 可能已经被目标预处理，代理看到的是进入模型的消息。模型最终回复也不一定是
目标最终展示给用户的回答。真实循环边界、重试意图、本地函数和未经过代理的请求仍不可见。

当前支持 `/v1/chat/completions`，流式重建支持 function 类型 `tool_calls`。
不支持 Responses、Anthropic、Realtime、模型列表接口或工具协议代理；旧式 `function_call`
和其他工具类型在流式重建中会标记缺口。仅有远程问答接口且不能改变模型通信路径的目标，
无法通过此功能还原内部执行。协议参考：[Chat Completions 流式事件](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)。

## 可选深度采集：Python 函数包装

对于能修改运行环境、希望补充真实函数执行的目标，仍可使用 `attacker trace run` 或 SDK。
它要求明确的函数绑定，覆盖标记为 `instrumented_boundaries_only`；这是补充能力，不是代理
接入的前置条件。

假设你已有 `my_agent.py`，入口为 `async def run(query: str)`，运行时通过模块属性
调用 `call_model` 和 `execute_tool`。准备 `query.json`：

```json
{"query": "查询订单 A123 是否可以退款"}
```

执行真实目标（工具的原有副作用也会正常发生）：

```sh
uv run attacker trace run my_agent:run \
  --input query.json \
  --model my_agent:call_model \
  --tool my_agent:execute_tool \
  --output data/traces/order-query.jsonl

uv run attacker trace show data/traces/order-query.jsonl
```

`--model`、`--tool` 和 `--loop` 可以多次指定，支持 `module:Class.method` 普通实例方法。
输入文件是入口函数的关键字参数对象。输出必须是新文件，避免覆盖已有证据。
CLI 会实际调用目标一次，不做重试；目标失败会保留原异常并以失败退出。
查看文件仍可获得已采集的事件。

**绑定的是调用时真正查找的属性。** 如果 Agent 使用
`from provider import call_model`，应绑定 `my_agent:call_model`，而非原始
`provider:call_model`。已存入工具注册表、闭包或对象实例的旧函数引用不会被模块替换
自动更新；这些场景需要使用下述 SDK，将包装后的函数放入注册表。
属性替换在命令结束时恢复；CLI 适用于单次独立运行，不用于共享服务的全局热修改。

### SDK 接入

```python
from pathlib import Path
from app.tracing import ExecutionRecorder

# 每个 Query 创建独立实例。实际应用把包装函数交给自己的 Agent/工具注册表。
recorder = ExecutionRecorder(
    output=Path("data/traces/query-unique-id.jsonl"),
    redacted_fields={"customer_name"},
    secret_values={configured_api_key},
)
model_call = recorder.wrap(original_model_call, kind="model")
tools = {name: recorder.wrap(fn, kind="tool", name=name) for name, fn in original_tools.items()}

# agent_run 必须使用上面的 model_call 和 tools，才能采集内部边界。
try:
    result = await recorder.wrap(agent_run, kind="agent")(query)
finally:
    recorder.finish()

execution_trace = recorder.snapshot().model_dump(mode="json")
```

以上名称表示目标应用已有的函数、配置和工具表，需按实际项目接入。
同步函数可直接调用，无需 `await`。SDK 返回原始结果、抛出原始异常，不替换目标行为。
如果需要真正的循环轮次，在目标已知的每轮边界使用：

```python
with recorder.span("loop", "iteration", {"iteration": iteration}):
    # 在这里执行这一轮原有逻辑
    ...
```

CLI 的 `--loop` 只适合本身代表一轮执行的函数。一次模型请求不自动等于一轮，重试也
不自动算新轮次。模型返回的 `tool_calls` 保存在模型输出中，工具实际入参保存在工具
start 事件中；第一版不猜测二者的 call ID 对应关系，也不推断未采集的策略检查。

## 函数包装的数据与完整性

- 每个事件携带 `trace_id`、`span_id`、`parent_span_id`、顺序号、时间、类型和状态。
  父子 ID 表示嵌套关系；并行兄弟节点不因为文件顺序变成串行。
- `ContextVar` 支持正常 asyncio 任务上下文继承；不自动跨进程、网络或任意原生线程传播。
  完成 Query 前应等待所有相关任务结束，再调用 `finish()`。
- start 表示进入包装函数；end 的 `ok` 表示该函数返回成功，不证明外部事务提交。
  异常和 asyncio 取消分别记录。进程崩溃、采集丢失、尚未结束都可能留下未闭合 Span。
- 默认最多保存 10,000 个事件，每个事件的数据最多 16,000 字符，截断带有显式标记。
  每个集合最多保留 128 项，嵌套深度最多 10 层，省略内容以 `capture_omitted` 标注。
  `finish()` 写入采集汇总；`show` 显示丢弃/采集错误计数、未闭合 Span，以及是否有汇总。
  有汇总只表示采集流程结束，不表示覆盖了全部内部行为。
- 复用项目的敏感字段和文本脱敏；SDK 可补充已知 Secret，CLI 可用 `--redact-field`。
  通用规则不能识别所有业务隐私。未知对象不会调用 `repr`，而会标记 `capture_omitted`；
  Pydantic 对象、字典、列表和基本类型可序列化。
- 当前不支持流式生成器，不能跟踪远程工具服务器内部执行，也不会读取模型隐藏推理。
  返回流对象的 SDK 方法可能仅记录对象类型，此时不代表采集到了流式内容。
- JSONL 逐事件追加，但不提供事务性持久化、签名或防篡改保证。它是运行观测证据，
  不是独立可信的授权记录。

## 接入已有灰盒评估

HTTP 目标可将快照作为响应中的 `execution_trace` 返回：

```python
return {"message": result, "execution_trace": execution_trace}
```

`ToolTraceAdapter` 会脱敏、校验并保留到 `trace.execution_trace`，现有响应存储及结果链路
可携带它。目标若已有 `trace` 中的工具/策略事件，仍然同时使用原协议。
仅有 execution trace、没有工具和 Policy Evidence 时，现有安全评估仍是
`inconclusive`，不会虚构 `allow` 或把“采集成功”当成“安全通过”。

代理独立保存的 JSONL 不会自动注入目标响应或自动转成安全结论；上面的 HTTP 回传方式
针对主动使用 SDK 的目标。现有安全评估要求的 Policy Evidence 不因新增代理而放宽。

当前提供 JSON 路径查看，尚未提供网页时间线、框架自动探测或执行轨迹驱动的新评估器。
