# 企业自定义评测 Harness 与 Provider/Skill 装备库需求

## 1. 文档定位

本文定义 Attacker 第四阶段的核心扩展方向：在前三阶段已经完成安全评测闭环的基础上，引入可插拔 Harness、Provider 装备库和企业自定义 Skill 机制。

本文不是当前 MVP 的实现说明。启动本阶段前，必须先冻结一个可回归的前三阶段基线：

- SQLite/SQLAlchemy 已成为唯一在线事实源；
- Deterministic 与 Adaptive Run 生命周期已经可用；
- 三阶段不少于 30 条 Case 已通过验收；
- Approval、Evidence-backed Finding、JSON/Markdown Report 和 Replay 已有稳定语义；
- HTTP Target Connector、测试专用 Memory/RAG Adapter 和模型 Adapter 已通过 Core 内置实现跑通。

本文中的“现有”“原有”均指上述第四阶段启动基线，不指当前仍使用 DuckDB/Parquet 的早期原型。

第四阶段属于 Attacker v1 三阶段交付完成后的 vNext 扩展，不阻塞前三阶段的完成定义。本文被采纳后，需要用同一架构决策同步更新 `docs/architecture.md` 中“不复制 Providers/Skills”的旧表述：仍不建设通用 Marketplace 和通用 Agent Skills，只增加面向安全评测的受控 Provider/Skill Contract。

目标不是把 Attacker 扩展为多用户 SaaS 或通用 Agent 平台，而是让企业能够在不修改 Attacker Core 的情况下：

- 接入自己的 Agent、模型、工具系统和业务平台；
- 编写符合自身业务规则的攻击、评估、证据采集和清理 Skill；
- 组合标准装备与企业私有装备，形成可复用的安全评测方案；
- 仍由 Attacker Core 统一执行 Policy Gate、预算控制、证据持久化、报告和 Replay。

Attacker 自身的规划、反馈闭环、Judge、停止与降级优化独立定义在：

- [Attacker Agent 专项优化需求](./attacker_agent_optimization_requirements.md)

本文作为后续架构设计、实施计划和阶段验收的需求基线。

---

## 2. 已确认的产品决策

### 2.1 保持单租户部署模型

当前不建设以下能力：

- 多用户账户体系；
- 多租户数据模型；
- OIDC/SSO；
- 用户、组织和角色管理；
- 通用 RBAC；
- 公共 Skill Marketplace；
- 面向互联网的不受信任用户自助安装。

默认使用场景是：

> 一个企业或一个安全团队部署一套 Attacker，由受信任的操作人员维护 Provider、Skill、Case Pack 和评测任务。

服务仍可保留部署级 API Key、网络隔离和操作日志，但不引入复杂的用户平台能力。

单租户部署不等于取消被测目标的身份模型。为了评测目标 Agent 的工具授权、审批和跨用户/跨租户隔离，Attacker 仍需把 `TestPrincipal`、目标侧 `tenant_id`、`session_id` 和凭据引用作为评测数据建模；这些对象不构成 Attacker 用户账户、组织或 RBAC。

### 2.2 Core 保持稳定，业务能力通过装备扩展

Attacker Core 只负责稳定且跨业务通用的能力：

- Run、Step、Event、Finding 和 Evidence 生命周期；
- Dataset、Policy 和 Target 快照；
- Policy Gate；
- 审批中断与恢复；
- 幂等、预算、超时和取消；
- 报告和 Replay；
- Provider/Skill 发现、校验和执行协议；
- 受控执行、故障隔离、强沙箱与凭据脱敏。

以下能力优先通过装备扩展：

- 特定厂商 Agent 接入；
- 特定企业平台 API 接入；
- 业务攻击样例生成；
- 业务安全规则；
- 自定义 Evaluator；
- 自定义 Trace 解析；
- 自定义 Evidence 采集；
- 自定义测试数据准备与清理；
- 行业或企业专属 Case Pack。

---

## 3. 目标与非目标

### 3.1 目标

1. 企业可以新增一个 Provider Package 和 Provider Instance，而不修改 Attacker Core。
2. 企业可以新增一个 Skill，并通过声明的 Capability Contract 绑定 Provider Instance。
3. 同一个 Skill 可以在满足相同 Capability Contract 的不同 Provider Instance 上运行。
4. Skill 不能绕过 Policy Gate、预算、Target allowlist 和 Evidence 规则。
5. Provider 和 Skill 的输入、输出、权限、依赖及版本均可被静态校验。
6. 装备执行产生的关键事实必须进入第四阶段启动基线中的 SQLite 事实模型，并通过 Repository 接口持久化。
7. 报告和 Replay 不直接依赖 Provider/Skill 的临时内存状态。
8. 企业私有装备可以使用本地目录或离线包交付。
9. 装备安装、启用、运行、失败和清理过程可诊断、可审计。
10. 错误装备不能拖垮 Attacker API 进程；被标记为不可信的可执行装备只能在满足强沙箱要求的平台运行。

### 3.2 非目标

- 不提供任意互联网 Skill 搜索和一键安装；
- 不允许运行时从未知 URL 自动下载代码；
- 不让 Skill 自行决定是否获得高风险权限；
- 不让 Provider 直接写入 Finding 或绕过 Event；
- 不把 LangGraph Graph State 作为装备事实源；
- 不要求第一版支持任意编程语言；
- 不为单个企业场景提前设计复杂的跨组织权限模型。
- 不复制 AtlasClaw 的通用 Channels、聊天 Session、通用 Memory、Token Pool、任意工具目录和 Hooks Marketplace；
- 不把“进程隔离”宣传为能够约束恶意代码的安全沙箱；
- 不把包签名等同于代码安全；
- 不保证重新调用外部 Target、Provider 或模型时得到与历史 Run 相同的输出。

---

## 4. 核心概念

### 4.1 Harness

Harness 是 Attacker Core 提供的受控执行环境，负责：

- 装载已启用装备；
- 构造运行上下文；
- 解析 Capability 依赖；
- 注入经过允许的配置和凭据；
- 调用 Provider 和 Skill；
- 执行 Policy Gate；
- 施加超时、预算、并发和资源限制；
- 捕获结构化输出；
- 把输出转换为 Event、Evidence 和 Finding；
- 执行失败补偿与清理；
- 支持取消、重试、恢复和 Replay。

Harness 不承载具体业务知识。

### 4.2 Provider

Provider Package 是外部系统或运行能力的适配器实现，例如：

- OpenAI-compatible Agent；
- LangGraph Agent；
- 企业内部 HTTP Agent；
- MCP Tool Server；
- 企业知识库；
- 企业审批系统；
- 业务数据库只读查询；
- 测试沙箱；
- Trace 或日志系统。

Provider Package 对外声明实现一个或多个 Capability Contract。

### 4.3 Provider Instance

Provider Instance 是 Provider Package 的一个已配置运行实例。例如，同一个 `enterprise-agent` Provider Package 可以有 `dev`、`staging` 和 `prod-sandbox` 三个实例。

Provider Package 与 Provider Instance 必须分离：

- Package 回答“运行哪份代码和契约”；
- Instance 回答“连接哪个系统、使用哪版非敏感配置和哪组 Secret 引用”；
- 一个 Package 可以对应多个 Instance；
- Run 必须绑定明确的 Instance，不能只绑定 Provider Package；
- Instance 配置变更生成新的 `config_revision`，不能原地改写历史 Run 快照；
- Secret 只保存引用及版本，不进入配置快照。

### 4.4 Skill

Skill 是在 Harness 中执行的业务评测能力，例如：

- 生成企业业务 Prompt Injection Case；
- 检测采购审批绕过；
- 检测云资源越权操作；
- 检测客服 Agent 泄露客户信息；
- 注入并验证测试 Memory；
- 构造 RAG Poisoning Fixture；
- 解析企业 Agent 的 Tool Trace；
- 校验业务审批链；
- 清理企业测试数据。

Skill 通过 Capability Contract 使用 Provider Instance，不应直接依赖具体 Provider 的内部实现。

### 4.5 Capability Contract

Capability Contract 是 Core 所有、Provider 实现、Skill 使用的稳定契约，例如：

```text
agent.invoke.v1
agent.trace.read.v1
memory.fixture.write.v1
memory.fixture.cleanup.v1
rag.document.index.v1
rag.retrieval.query.v1
approval.status.read.v1
business.order.create_sandbox.v1
business.order.cleanup_sandbox.v1
```

Capability 名称必须包含版本，但版本后缀本身不构成完整契约。每个 Contract 至少定义：

- 请求和响应 JSON Schema 及其 checksum；
- 标准错误码；
- 副作用等级；
- 风险等级；
- 是否要求稳定 `operation_id`；
- 重试与幂等要求；
- 必需 Evidence 类型；
- 资源创建与 Cleanup 语义；
- timeout、响应大小和敏感字段规则。

Provider 声明“实现 Contract”，Skill 声明“依赖 Contract”。Core 在不导入装备 Python 模块的前提下完成静态兼容性校验。

### 4.6 Case Pack

Case Pack 是可版本化的评测数据包，包含：

- Case；
- attack/control 配对；
- 默认 Policy；
- Evaluator 配置；
- 所需 Capability；
- Fixture 和 Cleanup 声明；
- 期望 Evidence；
- 标签和适用范围。

Case Pack 默认是纯数据，不执行任意代码。

### 4.7 Test Principal

`TestPrincipal` 表示调用被测目标时使用的测试主体，而不是 Attacker 登录用户。它至少包含：

```text
principal_id
tenant_id
role_labels
credential_ref
session_scope_id
```

Credential 只使用引用；实际值由 Provider Instance 在调用期间解析。跨用户、跨租户、跨 Session 和审批绕过评测必须显式绑定 Test Principal，并把主体引用写入 Evidence。

---

## 5. 总体架构

```text
CLI / API
   |
   v
Equipment Catalog
   |- Package Registry
   |- Provider Instance Registry
   |- Skill Registry
   |- Case Pack Registry
   \- Capability Contract Registry
   |
   v
Capability Resolver
   |
   v
Harness Runtime
   |- Execution Context
   |- Policy Gate
   |- Budget / Timeout / Cancel
   |- Secret Injection
   |- Sandbox
   |- Capability Broker
   |- Provider Adapter
   |- Skill Runner
   |- Resource Lease / Cleanup Coordinator
   |
   v
Run / Step / Event / Finding / Evidence
   |
   +--> Report
   \--> Replay
```

核心依赖方向必须保持：

```text
Skill -> Capability Contract <- Provider Package
                  |
                  v
             Harness Core
                  |
                  v
           Provider Instance
```

禁止：

```text
Skill -> 直接导入某个 Provider 内部模块
Provider -> 直接操作 FindingRecord
Skill -> 直接访问全局数据库 Session
Skill -> 绕过 Harness 发起网络请求
Skill -> 直接解析 Provider Secret
Run -> 只绑定 Provider Package 而不绑定 Provider Instance
```

---

## 6. 装备包目录规范

建议的本地目录：

```text
contracts/                         # 随 Attacker Core 发布，只读
├── agent.invoke.v1/
│   ├── contract.yaml
│   ├── request.schema.json
│   └── response.schema.json
└── agent.trace.read.v1/
    └── ...

equipment/                         # 部署方配置的装备根目录
├── providers/
│   ├── openai-compatible/
│   │   ├── provider.yaml
│   │   ├── PROVIDER.md
│   │   ├── adapter.py
│   │   ├── config.schema.json
│   │   └── tests/
│   └── enterprise-agent/
│       ├── provider.yaml
│       ├── PROVIDER.md
│       └── adapter.py
├── skills/
│   ├── procurement-approval-bypass/
│   │   ├── skill.yaml
│   │   ├── SKILL.md
│   │   ├── handler.py
│   │   ├── input.schema.json
│   │   ├── output.schema.json
│   │   ├── samples/
│   │   └── tests/
│   └── customer-data-leakage/
│       ├── skill.yaml
│       ├── SKILL.md
│       └── handler.py
└── casepacks/
    └── enterprise-procurement-v1/
        ├── casepack.yaml
        ├── cases.yaml
        └── README.md
```

第一版装备根目录通过配置指定：

```dotenv
EQUIPMENT__ROOT=equipment
EQUIPMENT__ALLOW_EXECUTABLE_PACKAGES=true
EQUIPMENT__REQUIRE_CHECKSUM=true
EQUIPMENT__ALLOW_UNTRUSTED=false
```

---

## 7. Provider Manifest

`provider.yaml` 至少包含：

```yaml
schema_version: provider.v1
id: enterprise-agent
name: Enterprise Agent Provider
version: 1.2.0
description: Connects Attacker to the internal enterprise agent sandbox.

attacker_compatibility:
  min_version: 1.0.0
  max_version: 1.x

entrypoint: adapter.py:EnterpriseAgentProvider

implements:
  - contract: agent.invoke.v1
  - contract: agent.trace.read.v1
  - contract: business.fixture.cleanup.v1

configuration:
  schema_file: config.schema.json

runtime:
  network:
    required: true
    allowed_hosts:
      - agent-sandbox.internal.example
  timeout_seconds: 30
  max_response_bytes: 1048576

healthcheck:
  timeout_seconds: 5
```

`provider.yaml` 描述 Provider Package，不保存 `base_url`、Token 或某个部署环境的实例配置。Provider Instance 由部署配置或管理 API 单独创建，并引用这个 Package。

### 7.1 Provider 必须实现的接口

建议最小 Protocol：

```python
class ProviderAdapter(Protocol):
    async def describe(self) -> ProviderDescription: ...
    async def validate_config(self, config: dict) -> ValidationResult: ...
    async def healthcheck(self, context: ProviderContext) -> HealthResult: ...
    async def invoke(
        self,
        capability: str,
        payload: dict,
        context: ProviderContext,
    ) -> ProviderResult: ...
    async def cleanup(
        self,
        resource: ResourceLeaseRef,
        context: ProviderContext,
    ) -> CleanupResult: ...
```

`ProviderContext` 必须绑定：

```text
run_id
step_id
operation_id
provider_package_id
provider_version
provider_checksum
provider_instance_id
config_revision
test_principal_ref
approved_host_set
budget
```

Provider Adapter 可以在调用期间通过 Core Secret Broker 使用当前 Instance 声明的 Secret 引用，但不得把 Secret 返回给 Skill、Event 或日志。

### 7.2 Provider Result

Provider 只能返回结构化事实：

```python
class ProviderResult(BaseModel):
    status: Literal["success", "denied", "error", "timeout"]
    output: dict
    evidence: list[EvidenceDraft]
    resource_leases: list[ResourceLeaseDraft]
    external_operation_id: str | None
    physical_attempts: int = 1
    retryable: bool = False
    error_code: str | None = None
    error_message: str | None = None
```

Core 必须按 Capability Contract 的 Response Schema 校验 `output`。Provider 不得自行创建 Finding。Finding 必须由 Core Evaluator 或受控 Evaluator Skill 根据 Evidence 产生。

Provider 内部发生的每次物理重试都计入调用、时间和成本统计；Provider 不得通过内部重试绕过 Run Budget。具有副作用的 Contract 默认禁止 Provider 自行重试，除非 Contract 明确规定稳定 `operation_id` 的幂等语义。

### 7.3 Capability Contract

`contract.yaml` 示例：

```yaml
schema_version: capability-contract.v1
id: agent.invoke.v1
request_schema: request.schema.json
response_schema: response.schema.json
side_effect: external_call
risk_level: medium

idempotency:
  operation_id_required: true
  provider_retry: forbidden

evidence:
  required:
    - target_request
    - target_response

limits:
  timeout_seconds_max: 120
  response_bytes_max: 1048576

redaction:
  sensitive_json_paths:
    - $.headers.authorization
```

Contract 由 Attacker Core 发布和版本化。装备只能引用已知 Contract，不能在自身 Manifest 中重新定义同名 Contract 的语义。新增或升级 Contract 必须通过独立的兼容性评审和 Contract Test。

---

## 8. Skill Manifest

`skill.yaml` 至少包含：

```yaml
schema_version: skill.v1
id: procurement-approval-bypass
name: Procurement Approval Bypass Evaluation
version: 1.0.0
description: Evaluates whether procurement operations bypass enterprise approval.

attacker_compatibility:
  min_version: 1.0.0
  max_version: 1.x

entrypoint: handler:ProcurementApprovalSkill

requires:
  capabilities:
    - binding: target_agent
      contract: agent.invoke.v1
    - binding: target_trace
      contract: agent.trace.read.v1
    - binding: procurement_fixture
      contract: business.procurement.fixture.v1

permissions:
  network: false
  filesystem:
    read:
      - package://samples/
    write:
      - workspace://runs/

execution:
  timeout_seconds: 120
  max_steps: 20
  max_provider_calls: 10
  max_output_bytes: 1048576

input_schema: input.schema.json
output_schema: output.schema.json

cleanup:
  required: true
  timeout_seconds: 30
```

### 8.1 Skill 类型

第一版支持以下类型：

| 类型 | 职责 |
|---|---|
| `case_generator` | 根据业务元数据生成结构化 Case |
| `fixture` | 准备隔离测试数据 |
| `executor` | 通过 Capability 驱动 Target 或业务沙箱 |
| `trace_adapter` | 把企业 Trace 转换为统一 Tool/Policy Trace |
| `evaluator` | 根据 Evidence 输出结构化 Evaluation |
| `cleanup` | 编排已声明的 Cleanup Capability 并校验 Resource Lease，不直接访问未登记资源 |
| `report_enricher` | 生成附加报告字段，不改变持久化事实 |

一个 Skill 可以声明多个类型，但第一版应优先保持单一职责。

### 8.2 Skill 接口

建议最小 Protocol：

```python
class Skill(Protocol):
    async def prepare(self, context: SkillContext) -> SkillPreparation: ...
    async def execute(
        self,
        payload: dict,
        context: SkillContext,
    ) -> SkillResult: ...
```

物理资源清理由 Harness 的 Cleanup Coordinator 根据 `ResourceLease` 调用 Provider 的 Cleanup Contract。Skill 可以声明清理顺序或提供清理校验，但不能通过一个无资源边界的通用 `cleanup(context)` 自行扫描和删除资源。

### 8.3 Skill Context

Skill 只能获得受控上下文：

```python
class SkillContext(BaseModel):
    run_id: str
    step_id: str
    operation_id: str
    case_id: str
    target_id: str
    test_principal_ref: str
    allowed_capabilities: list[str]
    capability_bindings: dict[str, CapabilityBindingRef]
    budget: SkillBudget
    workspace_path: str
    resource_lease_refs: list[str]
```

Skill 不直接获得：

- 数据库 Session；
- 全量环境变量；
- Attacker 全局配置；
- 其他 Run 的工作目录；
- 原始 Secret 字典；
- Provider 原始配置或客户端对象；
- 未授权 Provider Instance；
- 未声明 Capability。

Skill 如果确实需要某类凭据支持，必须通过一个受控 Provider Capability 完成操作，而不是请求通用 Secret 读取权限。

---

## 9. Equipment Catalog

装备库需要提供：

### 9.1 发现

- 加载 Core 随版本发布的 Capability Contract Registry；
- 扫描配置的 Provider、Skill 和 Case Pack 根目录；
- 忽略隐藏目录、缓存目录和符号链接逃逸；
- 解析 Manifest；
- 计算包 checksum；
- 验证入口文件存在；
- 验证 ID 和版本唯一；
- 验证 Attacker 版本兼容性。

### 9.2 注册

Registry 保存：

- package ID；
- package type；
- version；
- source path；
- checksum；
- manifest snapshot；
- enabled 状态；
- validation 状态；
- validation errors；
- discovered_at；
- loaded_at。

Provider Instance Registry 另外保存：

- instance ID；
- Provider Package ID/version/checksum；
- environment 和 display name；
- config revision；
- Secret binding revision；
- enabled 和 health status；
- allowed host set；
- created_at 和 updated_at。

Package 的启用状态表示代码可被使用；Instance 的启用状态表示某个具体连接可被绑定。两者必须同时启用。

### 9.3 查询

支持按以下条件查询：

- ID；
- type；
- Capability Contract；
- enabled；
- version；
- tag；
- validation status。
- Provider Instance ID；
- environment；
- health status。

### 9.4 启用与禁用

- 未通过校验的装备不能启用；
- 禁用装备不应影响历史报告和 Replay；
- 正在运行的 Run 固定使用启动时的装备快照；
- reload 只影响新 Run；
- 删除包前必须确认没有活跃 Run 依赖；
- 历史 Run 保存 package ID、version、checksum、Provider Instance ID、config revision 和 Secret binding revision。

### 9.5 冲突处理

相同 package type、ID 和 version 重复时必须报错，不允许静默覆盖。相同 ID/version 但 checksum 不同必须返回 `immutable_version_conflict`。

Capability Resolver 必须确定性选择 Provider Instance：

1. Run 显式绑定；
2. Policy 默认绑定；
3. 唯一满足 Contract、Target、Test Principal、环境和健康条件的启用 Instance；
4. 多个候选且未指定时失败。

不允许基于目录扫描顺序随机选择。

---

## 10. Harness Runtime

### 10.1 信任等级、故障隔离与强沙箱

装备信任等级：

| 等级 | 执行方式 | 安全含义 |
|---|---|---|
| `trusted_builtin` | 可同进程 | Attacker 自带并随 Core 发布 |
| `trusted_enterprise` | 独立子进程 | 提供崩溃、超时和协议隔离，不抵御恶意代码 |
| `untrusted` | 强制 OS/容器沙箱 | 约束文件、网络、进程和资源；平台不支持时拒绝运行 |

企业可执行 Skill 和 Provider Adapter 默认至少使用独立子进程；只有 `trusted_builtin` 可以同进程执行。独立子进程是故障隔离，不是安全沙箱。

子进程 Runner 至少提供：

- 独立工作目录；
- 最小环境变量；
- stdin/stdout 结构化协议；
- 超时后终止进程树；
- 最大 stdout/stderr；
- 明确退出码；
- 取消信号；
- Windows 和 Linux Contract Test。

`untrusted` 装备必须使用强沙箱。首个强沙箱实现优先支持 Linux 容器：

- 非 root 用户；
- 只读根文件系统；
- CPU/内存限制；
- 网络默认关闭；
- 挂载最小工作目录；
- seccomp/AppArmor；
- no-new-privileges；
- 执行结束销毁。

Windows 如果没有等价的文件、网络和进程限制能力，只支持 `trusted_builtin` 与 `trusted_enterprise`，不得把普通子进程标记为 `untrusted` 安全执行。

### 10.2 文件系统

以下文件访问规则只有在强沙箱中才是安全边界；普通子进程只能做路径协议校验，不能承诺抵御恶意代码：

- 只能读取自己的 package 目录；
- 只能写当前 Run workspace；
- 禁止 `..` 路径穿越；
- 禁止符号链接逃逸；
- 禁止访问 `.env`、数据库文件和 checkpoint 文件；
- 清理只能作用于当前 Run 创建的路径和资源。

### 10.3 网络

- 强沙箱默认禁止 Skill 直接联网；
- 外部调用优先通过 Provider；
- Provider 只允许访问 Manifest 声明并经部署配置批准的 host；
- DNS 解析后的 IP 仍需校验；
- 禁止跟随未校验重定向；
- 禁止访问云元数据地址；
- 禁止访问 Attacker 控制面和数据库端口；
- 记录目标 host、耗时、状态码和 operation_id，但不记录 Secret。

### 10.4 Secret Broker

- Provider Instance 只保存 Secret 引用及版本；
- 实际值来自部署配置或 Secrets Backend；
- Secret 仅在调用期间解析；
- Skill 不获得通用 Secret handle；
- Provider Adapter 只能通过 Core Secret Broker 为当前 Instance 和当前调用使用已声明 Secret；
- Broker 不提供“读取并返回明文”的通用接口；
- stdout、stderr、Event、Finding、Report 和 Replay 统一脱敏；
- Secret 不进入装备快照；
- Run 快照只保存 Secret binding revision；
- 调用结束释放解析出的 Secret。

### 10.5 幂等

每次装备操作必须绑定稳定的：

```text
run_id
step_id
operation_id
package_id
package_version
provider_instance_id
config_revision
capability
test_principal_ref
```

重复 operation_id：

- 不得重复调用具有副作用的 Provider；
- 返回已持久化结果；
- 清理重复执行必须安全；
- 进程恢复后仍保持幂等。

---

## 11. Policy Gate 集成

装备不能自行扩大权限。

Policy Gate 在每次调用前校验：

- Provider 是否启用；
- Skill 是否启用；
- package checksum 是否与 Run 快照一致；
- Capability 是否在 Skill Manifest 中声明；
- Capability 是否在 Run Policy 中允许；
- Capability Contract checksum 是否与 Run 快照一致；
- Provider Instance、config revision 和 Secret binding revision 是否与 Run 快照一致；
- Target 是否在 allowlist；
- Case 是否在 allowlist；
- Test Principal、Tenant 和 Session scope 是否在允许范围；
- 是否需要人工审批；
- provider call budget；
- step budget；
- duration budget；
- network allowlist；
- cleanup scope；
- risk level。

高风险 Capability 示例：

```text
business.record.create.v1
business.record.update.v1
business.record.delete.v1
tool.shell.execute.v1
filesystem.write.v1
external.message.send.v1
```

高风险 Capability 默认要求审批或只能绑定测试沙箱 Provider。

---

## 12. Evidence 与 Finding

### 12.1 装备事件

至少记录：

```text
equipment_discovered
equipment_validation_failed
equipment_loaded
skill_started
skill_completed
skill_failed
skill_timed_out
provider_call_requested
provider_call_allowed
provider_call_denied
provider_call_completed
provider_call_failed
cleanup_started
cleanup_completed
cleanup_failed
```

### 12.2 Evidence

运行时装备 Evidence 至少包含：

- package ID/version/checksum；
- capability；
- operation_id；
- 输入摘要；
- 输出摘要；
- Provider Package 和 Provider Instance；
- config revision 和 Secret binding revision；
- Target；
- Test Principal、Tenant 和 Session scope 引用；
- Policy decision；
- 时间和延迟；
- external operation ID；
- redaction 状态；
- cleanup 状态。

### 12.3 Finding

Finding 必须引用判定所需的：

- Skill 执行 Event；
- Provider 调用 Event；
- Evaluator Event；
- 必要的 Policy Event。

不得仅根据 Skill 返回的字符串直接创建 Finding。

涉及 Fixture、副作用资源或状态污染的 Finding 还必须关联 `ResourceLease` 和最新 Cleanup 状态，但 Finding 可以先于 Cleanup 完成。Cleanup 失败影响 Run/Report 的清理状态，不得导致已经有充分 Evidence 的 Finding 消失。只读评测不强制产生 Cleanup Event。

`equipment_discovered`、`equipment_validation_failed` 和 `equipment_loaded` 属于控制面审计事件，可以没有 `run_id`；Skill、Provider 和 Cleanup 事件属于 Run 事实，必须绑定 `run_id`、`step_id` 和 `operation_id`。

---

## 13. Replay

Run 启动时保存：

- Provider Manifest snapshot；
- Skill Manifest snapshot；
- Case Pack snapshot；
- package ID；
- version；
- checksum；
- Capability Contract ID/checksum；
- Provider Instance binding；
- config revision；
- Secret binding revision；
- Test Principal 和 Target binding；
- 非敏感配置；
- Policy。

Replay 必须区分三种模式：

### 13.1 Evidence Re-evaluate

只读取源 Run 已保存的 Evidence，使用固定或显式升级的 Evaluator 重新判定，不调用 Target、Provider 或模型。该模式用于验证 Evaluator 变化，且应尽可能确定性。

### 13.2 Same-binding Rerun

使用与源 Run 相同的 Package version/checksum、Provider Instance、config revision、Secret binding revision、Capability Contract 和 Policy 重新调用外部系统。

该模式只保证绑定一致，不保证输出一致。外部 Target、Provider 状态、模型和 Secret 的实际内容可能变化，报告必须把它标记为新的执行结果，不能宣称复现历史输出。

若本地装备已不存在：

- 不允许静默使用新版本；
- 返回缺少历史装备错误；
- 可通过离线归档恢复历史装备。

### 13.3 Upgrade Comparison

显式选择新的 Provider/Skill 版本，用于比较：

- Target 修复效果；
- Skill/Evaluator 升级效果；
- Case Pack 升级带来的新 Finding。

报告必须区分：

```text
target_changed
provider_changed
provider_instance_changed
provider_config_changed
skill_changed
casepack_changed
contract_changed
policy_changed
test_principal_changed
```

避免把装备变化错误解释为 Target 安全变化。

---

## 14. API 与 CLI

### 14.1 查询 API

```text
GET /equipment/provider-packages
GET /equipment/provider-packages/{package_id}
GET /equipment/provider-instances
GET /equipment/provider-instances/{instance_id}
GET /equipment/skills
GET /equipment/skills/{skill_id}
GET /equipment/casepacks
GET /equipment/contracts
```

### 14.2 管理 API

第一版只允许操作本地已存在的装备目录：

```text
POST /equipment/reload
POST /equipment/provider-packages/{package_id}/validate
POST /equipment/provider-packages/{package_id}/enable
POST /equipment/provider-packages/{package_id}/disable
POST /equipment/provider-instances
POST /equipment/provider-instances/{instance_id}/enable
POST /equipment/provider-instances/{instance_id}/disable
POST /equipment/skills/{skill_id}/validate
POST /equipment/skills/{skill_id}/enable
POST /equipment/skills/{skill_id}/disable
```

不提供通过 URL 下载远程包的 API。

### 14.3 Dry Run

```text
POST /equipment/provider-instances/{instance_id}/healthcheck
POST /equipment/skills/{skill_id}/dry-run
```

Dry Run：

- 使用隔离 workspace；
- 使用独立 operation_id；
- 仍经过 Policy Gate；
- 不产生正式 Finding；
- 保存验证 Event；
- 如果 Dry Run 创建 Resource Lease，则必须执行 cleanup；只读 Dry Run 不强制生成 cleanup。

### 14.4 CLI

建议提供：

```text
attacker equipment list
attacker equipment validate <path>
attacker equipment reload
attacker provider-instance healthcheck <instance-id>
attacker skill dry-run <id>
attacker casepack validate <path>
```

CLI 与 API 复用同一 Service，不复制校验逻辑。

---

## 15. 数据模型

建议新增：

### 15.1 equipment_packages

```text
id
package_type
package_id
version
checksum
source_path
manifest_json
enabled
validation_status
validation_errors_json
discovered_at
loaded_at
```

唯一约束：

```text
package_type + package_id + version
```

相同唯一键但 checksum 不同必须拒绝，不允许把修改后的内容继续发布为同一个版本。

### 15.2 provider_instances

```text
id
provider_package_id
display_name
environment
config_revision
config_json
secret_binding_revision
secret_refs_json
allowed_hosts_json
enabled
health_status
created_at
updated_at
```

`config_json` 必须脱敏；`secret_refs_json` 只保存 Secret 名称、引用和版本，不保存实际值。

### 15.3 run_equipment_snapshots

```text
id
run_id
package_type
package_id
version
checksum
manifest_json
provider_instance_id
config_revision
secret_binding_revision
capability_contract_id
capability_contract_checksum
test_principal_ref
target_binding_ref
```

一个 Run 可以有多条装备快照；快照在 Run 启动后不可修改。

### 15.4 equipment_executions

```text
id
run_id
step_id
operation_id
package_id
package_version
package_checksum
provider_instance_id
config_revision
capability
capability_contract_checksum
test_principal_ref
status
physical_attempts
input_summary_json
output_summary_json
error_code
started_at
completed_at
```

唯一约束：

```text
operation_id
```

详细 Evidence 仍进入 Event，不在多个事实模型中重复保存。

### 15.5 resource_leases

```text
id
run_id
created_by_operation_id
provider_instance_id
resource_type
external_resource_id
cleanup_contract
cleanup_payload_ref
status
last_cleanup_operation_id
created_at
cleaned_at
```

`ResourceLease` 是 Cleanup 的唯一资源边界。Cleanup Coordinator 只能处理当前 Run 拥有且已登记的 Lease。

---

## 16. 装备供应链安全

### 16.1 包校验

- Manifest schema 校验；
- Capability Contract ID、Schema 和 checksum 校验；
- Provider/Skill input/output JSON Schema 校验；
- checksum；
- 文件数量和总大小限制；
- 解压深度限制；
- 拒绝绝对路径；
- 拒绝 `..`；
- 拒绝符号链接逃逸；
- 拒绝 archive bomb；
- 拒绝重复 ID/version；
- 拒绝不兼容 Attacker 版本；
- 拒绝未声明入口。

静态校验不得通过 import Provider/Skill Python 模块来读取 Schema 或元数据。

### 16.2 签名

第二阶段支持离线签名：

- 企业内部签名密钥；
- 包签名；
- 发布者 ID；
- 信任根配置；
- 签名验证结果；
- 吊销列表。

签名只证明包的发布来源和内容完整性，不证明代码没有恶意行为，也不降低其所需的运行时隔离等级。

生产配置可以要求：

```dotenv
EQUIPMENT__REQUIRE_SIGNATURE=true
```

### 16.3 静态检查

Executable Skill 和 Provider Adapter 启用前可执行：

- Python AST 检查；
- import allow/deny list；
- 依赖清单检查；
- Secret pattern 扫描；
- shell 调用检查；
- 网络库使用检查；
- 已知漏洞扫描。

静态检查不能替代运行时沙箱。

---

## 17. 可观测性

日志字段：

```text
run_id
step_id
operation_id
package_id
package_version
package_checksum
capability
capability_contract_checksum
provider_instance_id
config_revision
test_principal_ref
skill_id
trust_level
physical_attempts
duration_ms
status
error_code
```

指标：

- equipment discovery duration；
- invalid package count；
- enabled package count；
- skill execution count/duration；
- skill timeout count；
- provider call count/duration/error；
- provider physical attempt count；
- provider instance health/error；
- capability denied count；
- cleanup failure count；
- package checksum mismatch count；
- sandbox termination count。

不得把 Skill 输入全文、Provider 原始响应或 Secret 作为日志标签。

---

## 18. 分阶段实施

Attacker Agent 内核应先满足独立需求文档中的闭环和安全边界，再开放企业装备参与 Adaptive Workflow。

### 18.1 第四阶段 A：Contract 与本地 Catalog

交付：

- Provider/Skill/Case Pack Manifest；
- Core-owned Capability Contract；
- Manifest 和 input/output/config JSON Schema；
- 目录扫描；
- 静态校验；
- Package/Provider Instance/Skill/Case Pack/Contract Registry；
- Capability Resolver；
- 查询 API；
- reload；
- Run 装备快照；
- 两个内置 Provider；
- 三个示例 Skill。

此阶段只允许 `trusted_builtin` Python 装备在同进程运行；接口设计不能依赖全局变量、数据库 Session、Provider 客户端或原始 Secret。

### 18.2 第四阶段 B：Harness Runtime

交付：

- 独立子进程 Skill 和 Provider Adapter Runner；
- JSON stdin/stdout 协议；
- timeout/cancel；
- workspace 隔离；
- environment allowlist；
- Secret Broker；
- Provider 调用代理；
- Policy Gate；
- Event/Evidence；
- Resource Lease 和 Cleanup Coordinator；
- crash recovery；
- dry-run。

### 18.3 第四阶段 C：强沙箱、Replay 与版本治理

交付：

- `untrusted` Linux 强沙箱；
- trust level 策略和平台能力检查；
- package checksum；
- Package 内容寻址归档和 dependency lock；
- 历史装备快照；
- Evidence Re-evaluate；
- Same-binding Rerun；
- Upgrade Comparison；
- 变更维度报告；
- 离线包导入；
- 签名验证；
- 兼容性策略。

### 18.4 第四阶段 D：企业装备开发体验

交付：

- Provider SDK；
- Skill SDK；
- 脚手架命令；
- Manifest JSON Schema；
- 本地验证 CLI；
- 示例工程；
- Contract Test Kit；
- 企业装备开发文档；
- CI 模板。

---

## 19. 首批内置装备

### 19.1 Provider

1. `http-agent-provider`
   - `agent.invoke.v1`
   - `agent.trace.read.v1`

2. `isolated-state-provider`
   - `memory.fixture.write.v1`
   - `memory.fixture.read.v1`
   - `memory.fixture.cleanup.v1`
   - `rag.document.index.v1`
   - `rag.retrieval.query.v1`

### 19.2 Skill

1. `prompt-injection-evaluator`
   - 把第一阶段黑盒 Evaluator 迁移为标准 Skill。

2. `tool-policy-trace-evaluator`
   - 把第二阶段 Tool/Policy Trace 解析迁移为标准 Skill。

3. `state-poisoning-evaluator`
   - 把第三阶段 Memory/RAG 评估迁移为标准 Skill。

迁移时必须保持第四阶段启动时冻结的 30 条三阶段基线 Case 的结果和 Evidence 语义不变。

---

## 20. 验收标准

### 20.1 Catalog

- 至少 2 个 Provider Package 可被发现和加载；
- 同一 Provider Package 至少可配置 2 个相互独立的 Provider Instance；
- 至少 3 个 Skill 可被发现和加载；
- 至少 2 个 Case Pack 可被发现；
- 相同 ID/version 但 checksum 不同会以 `immutable_version_conflict` 明确失败；
- 非法 Manifest 不会进入 enabled 状态；
- reload 不影响正在运行的 Run；
- 相同目录内容每次解析结果一致。

### 20.2 扩展性

- 新增企业 Provider 不修改 Attacker Core；
- 新增企业 Skill 不修改 Attacker Core；
- Skill 只依赖 Capability Contract，不直接导入 Provider；
- 同一 Skill 可切换两个通过同一 Contract Test 的 Provider Instance；
- Capability Contract 多候选且未绑定 Instance 时明确失败；
- Package、Instance、配置修订和 Secret 引用修订可以独立追踪。

### 20.3 安全

- 未声明 Capability 的调用被拒绝；
- Contract checksum 或 Schema 不一致的调用被拒绝；
- 超预算调用被拒绝；
- 高风险 Capability 未审批不能执行；
- Skill 或 Provider Adapter 超时后进程树被终止；
- `untrusted` 装备的路径穿越、符号链接逃逸和非 allowlist 网络访问被强沙箱拒绝；
- 不支持强沙箱的平台拒绝运行 `untrusted` 装备；
- Secret 不出现在数据库、日志、Event、Report 和 checkpoint；
- Skill 无法通过通用接口读取 Provider Secret 明文；
- checksum 不一致的包不能运行；
- Cleanup 只能作用于当前 Run 登记的 Resource Lease。

### 20.4 事实与报告

- 每次 Provider 调用都有 operation_id；
- 每个 Finding 引用装备执行 Evidence；
- Run 保存 Provider/Skill/Case Pack 版本和 checksum、Provider Instance、config revision、Secret binding revision 及 Contract checksum；
- 报告可只从数据库重建；
- Evidence Re-evaluate 不调用外部系统；
- Same-binding Rerun 可重建原绑定但不宣称复现原输出；
- Upgrade Comparison 明确标记 Target、Instance、配置、Contract 和装备变化；
- 装备缺失时 Replay 明确失败，不静默替换版本。

### 20.5 稳定性

- Skill 异常不会导致 FastAPI 进程退出；
- Provider 超时不会阻塞其他 Run；
- 进程重启后不会重复有副作用的 Provider 调用；
- cleanup 失败被记录并可重试；
- 重试统计包含 Provider 内部物理尝试，且不会绕过预算；
- malformed stdout 不会被当作成功结果；
- stdout/stderr 超限会终止装备执行；
- Windows 和 Linux Contract Test 均通过。

### 20.6 回归

- 第四阶段启动时冻结的 30 条基线 Case 全部继续可运行；
- 第四阶段启动基线的 JSON/Markdown 报告继续可生成；
- 第四阶段启动基线的审批恢复语义不变；
- 第四阶段启动基线的 fixed/new/persistent/regressed Finding Diff 分类不变；
- Ruff、Pyright、pytest 和 CI 全部通过。

---

## 21. 完成标准

满足以下条件后，可认为 Attacker 已具备企业自定义评测 Harness 的第一版能力：

1. Core、Capability Contract、Provider Package、Provider Instance、Skill 和 Case Pack 边界清晰；
2. 企业无需修改 Core 即可接入业务系统；
3. 企业无需修改 Core 即可增加业务评测规则；
4. 装备有版本、checksum、快照和兼容性约束；
5. 装备执行经过 Policy Gate、预算和审批；
6. 错误装备与 API 主进程故障隔离，`untrusted` 装备只在强沙箱运行；
7. Secret Broker、Resource Lease、文件和网络限制具有明确且可验证的安全边界；
8. 装备输出进入统一 Event/Evidence/Finding 模型；
9. Replay 能区分 Evidence Re-evaluate、Same-binding Rerun 和 Upgrade Comparison，并识别 Target、Instance、配置、Contract 与装备变化；
10. 第四阶段启动时冻结的三阶段评测基线完成装备化迁移且无行为回归。
