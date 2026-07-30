# Attacker

Attacker 是一个面向 AI Agent 的授权安全评测服务。它执行结构化攻击与安全对照用例，通过确定性 Evaluator、Policy Gate 和可恢复工作流记录证据，并从 SQLite 生成 Finding、报告和 Replay 差异。

> 仅用于明确授权的目标与隔离测试环境。项目默认拒绝未显式授权的公网或不可解析 Target。

## V1 状态

三个评测阶段已经完成：

| 阶段 | 用例数 | 覆盖范围 |
|---|---:|---|
| 纯黑盒 | 12 | Prompt Injection、系统提示泄露、敏感数据、上下文污染、资源预算 |
| 灰盒 Agent | 10 | Tool/Policy Trace、越权工具、危险参数、审批绕过、Planner 循环 |
| 带状态 Agent | 8 | Memory/RAG 污染、身份隔离、Checkpoint 恢复、Replay |
| **合计** | **30** | 每个阶段均包含攻击和正常/安全对照 |

V1 已包含：

- FastAPI 运行、审批、报告和 Replay API；
- SQLite + SQLAlchemy 唯一在线事实源；
- Alembic `0003` 数据库结构；
- JSON 和 Markdown 报告；
- Finding 到 Evidence Event 的引用；
- LangGraph 自适应工作流、checkpoint、interrupt 和恢复后 Policy 重校验；
- tenant/user/session/namespace 状态隔离与测试数据清理证据；
- black-box、gray-box 和 stateful Run 的 Replay；
- fixed、new、persistent、regressed 差异分类；
- 可选的服务端 API Key；
- pytest、Ruff、Pyright 和 GitHub Actions CI。

详细边界和验收标准见 [target/summary.md](target/summary.md)，架构见 [docs/architecture.md](docs/architecture.md)。

## 运行模型

```text
Target + Dataset + Policy
  -> Evaluation Run
  -> Event-backed Finding
  -> SQLite
  -> JSON / Markdown Report
  -> Replay Diff
```

- **Deterministic Mode**：固定 Dataset、Policy 和 Evaluator，不依赖 LLM 决策。
- **Adaptive Mode**：LangGraph 只负责编排下一步；Target、Case、Tool、审批和预算仍由 Policy Gate 决定。
- **Replay**：数据集和策略来自持久化快照；HTTP Target 地址和凭据必须由调用者重新提供。Replay 不读取历史 checkpoint，也不从数据库恢复秘密。

对 adaptive gray-box 源 Run 执行 Replay 时，会使用持久化的 Case/Policy 和确定性灰盒执行器；这样比较的是相同安全事实在新 Target 上的变化，而不是 Planner 随机性。

## Agent 模型 Provider 边界

Planner 和可选 Model Judge 通过窄 `model.inference.v1` 契约调用模型。Attacker Core
负责构造受控 Prompt、执行预算与 Policy 校验、验证结构化响应并决定状态迁移；Provider
只负责鉴权、协议适配、受限重试、健康检查以及 token、延迟和用量归一化。

- Provider 不能创建 Candidate、授权、Finding、停止条件或 Graph 路由；
- 每次物理请求（包括 Provider 内部重试）都计入独立 Provider 调用预算；
- Provider 返回的估算成本计入 Run 的 `max_cost` 硬预算；
- 每次尝试只记录状态、错误类别和延迟，不把凭据或原始错误响应写入事件；
- Planner 与 Model Judge 使用独立 Prompt Profile、模型身份和用量统计；
- 确定性 Planner 不调用模型，物理 Provider 调用数为零。
- checkpoint 恢复会先读取稳定 operation 对应的 Planner Event，不重复模型请求。

## 环境要求

- Python `>=3.12,<3.13`
- [uv](https://docs.astral.sh/uv/)

安装锁定依赖：

```powershell
uv sync --locked --python 3.12
```

创建数据库：

```powershell
uv run alembic upgrade head
```

启动服务：

```powershell
uv run uvicorn main:app --reload
```

默认地址：

- OpenAPI：`http://127.0.0.1:8000/docs`
- 健康检查：`GET http://127.0.0.1:8000/health`

## 配置

复制 `.env.example` 为 `.env` 后按需修改：

```dotenv
APP__APP_NAME=attacker
APP__APP_ENV=local
APP__DEBUG=true
APP__API_PREFIX=

DATABASE__URL=sqlite+aiosqlite:///data/attacker.sqlite3
CHECKPOINT__DATABASE_PATH=data/langgraph_checkpoints.sqlite3

# 留空时适合本机开发；设置后业务接口必须携带 X-API-Key。
SECURITY__API_KEY=replace-with-a-random-secret
```

启用 API Key 后，以下接口需要请求头：

```http
X-API-Key: replace-with-a-random-secret
```

`/health`、`/docs` 和 `/openapi.json` 保持公开。运行、审批、Replay 和测试接口受保护。

## API 示例

### 纯黑盒运行

```http
POST /runs/deterministic
Content-Type: application/json
X-API-Key: replace-with-a-random-secret

{
  "target": {
    "name": "local-sandbox",
    "endpoint": "http://localhost:9000/chat"
  },
  "dataset_path": "samples/blackbox/phase1.yaml",
  "budget": {
    "max_cases": 12,
    "max_target_calls": 32,
    "max_duration_seconds": 300,
    "max_response_bytes": 1048576
  }
}
```

### 确定性灰盒运行

```http
POST /runs/graybox/deterministic
Content-Type: application/json

{
  "target": {
    "name": "local-agent",
    "endpoint": "http://localhost:9000/agent"
  },
  "dataset_path": "samples/graybox/phase2.yaml"
}
```

### 自适应灰盒运行

```http
POST /runs/adaptive
Content-Type: application/json

{
  "target": {
    "name": "local-agent",
    "endpoint": "http://localhost:9000/agent"
  },
  "dataset_path": "samples/graybox/phase2.yaml",
  "planner": {
    "backend": "deterministic",
    "provider_id": "deterministic",
    "max_physical_attempts": 1
  },
  "policy": {
    "max_provider_calls": 20,
    "max_cost": "0.50"
  }
}
```

若运行进入人工审批：

```http
GET /runs/{run_id}/approvals
```

```http
POST /runs/{run_id}/approvals/{approval_id}
Content-Type: application/json

{
  "approved": true,
  "resolved_by": "security-reviewer",
  "reason": "authorized isolated evaluation",
  "target": {
    "name": "local-agent",
    "endpoint": "http://localhost:9000/agent"
  }
}
```

进程重启后恢复审批时需要重新提供 Target；原始凭据不会写入 checkpoint 或业务数据库。

等待审批时 Run 状态为 `waiting_approval`，且没有终止 `stop_reason`。Planner 按策略进入
`pause` 时，Run 状态为 `paused`，可恢复同一个 LangGraph thread：

```http
POST /runs/{run_id}/resume
Content-Type: application/json

{
  "target": {
    "name": "local-agent",
    "endpoint": "http://localhost:9000/agent"
  },
  "planner": {
    "backend": "deterministic"
  }
}
```

仅当进程重启后需要重新注入运行时 Target/Planner 配置时才需要提供这些字段。恢复配置
必须与 Run 快照中的 Target 名称、地址以及 Planner backend/provider/model 匹配。

授权方可以请求三种有界硬停止，调用方不能直接指定 Graph 路由或任意停止原因：

```http
POST /runs/{run_id}/control
Content-Type: application/json

{
  "action": "cancel",
  "reason": "authorized evaluation window ended"
}
```

`action` 可取：

- `cancel`；
- `revoke_target_authorization`；
- `terminate_policy`。

控制请求持久化为幂等 Event，并在下一次 Planner、Approval 或 Target 副作用边界前由 Core
执行。已经在 `waiting_approval` 或 `paused` 的 Run 会直接安全结束。

### 带状态运行

```http
POST /runs/stateful
Content-Type: application/json

{
  "profile": "hardened",
  "dataset_path": "samples/stateful/phase3.yaml"
}
```

可用 profile：

- `vulnerable`
- `hardened`
- `regressed`

### Replay

Stateful Run：

```http
POST /runs/{source_run_id}/replay
Content-Type: application/json

{
  "profile": "hardened"
}
```

Black-box 或 gray-box Run：

```http
POST /runs/{source_run_id}/replay
Content-Type: application/json

{
  "target": {
    "name": "patched-local-agent",
    "endpoint": "http://localhost:9000/agent",
    "auth": {
      "type": "bearer",
      "token": "resupplied-at-runtime"
    }
  }
}
```

Replay 结果：

```http
GET /runs/{run_id}/replay
```

差异含义：

- `fixed`：源 Run 存在、Replay 不再存在；
- `new`：Replay 新出现的攻击 Finding；
- `persistent`：源 Run 和 Replay 都存在；
- `regressed`：Replay 新出现的安全对照 Finding。

### 报告

```http
GET /runs/{run_id}/report.json
GET /runs/{run_id}/report.md
```

报告只读取 SQLite 中的 Run、Step、Event、Finding、状态证据与 Replay 关联，不依赖 LangGraph checkpoint。

Adaptive 报告额外包含由持久化事件计算的指标：

- Planner decision/rejection/fallback/error 与模型物理尝试、token、成本和延迟；
- Candidate 数量、过滤原因、Snapshot 生成/过期/拒绝；
- Coverage、实际信息增益、预测偏差、循环和无增益计数；
- Evaluator conflict/inconclusive、停止原因、恢复次数；
- DerivedCase 冻结、确定性复验和有效发现数；
- 每个 Finding 的最短 Evidence 路径长度。

指标不使用 Prompt、Target 原始响应或 Secret 作为标签。设置 `baseline_run_id` 后，只有
Dataset、Target、Test Principal、Policy、Evaluator、Candidate 宇宙和装备快照一致的
Adaptive/Deterministic Run 才会计算效率收益。发现收益只统计带持久化 Evidence 的
`derived_case_verified`；不可比或没有可测收益时，报告不会推荐 Adaptive 作为默认模式。

## 开发验证

完整本地检查：

```powershell
uv sync --locked --python 3.12
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
uv run python -m compileall -q app conf alembic
```

GitHub Actions 对 push 和 pull request 执行同一组检查。测试使用临时 SQLite 数据库，不调用外部 Target。

## Harness 与企业装备

服务启动时从 `contracts/` 和 `equipment/` 发现本地离线装备，验证 Manifest、JSON
Schema、Attacker 兼容性、入口文件、Capability 引用与内容 checksum，并把结果写入
SQLite Catalog。内置目录包含 2 个 Provider、3 个 evaluator Skill 和 2 个数据型 Case
Pack。

```powershell
uv run attacker equipment reload
uv run attacker equipment list --type provider
uv run attacker provider-instance healthcheck isolated-state-default
uv run attacker skill dry-run state-poisoning-evaluator --payload '{\"documents\":[]}'
```

查询与管理 API 位于 `/equipment/*`，沿用服务的 `X-API-Key` 保护。管理接口只操作本地
已存在目录，不接受远程包 URL。Provider Instance 的非敏感配置和 Secret 引用分别生成
不可变 revision；Run 使用 package/instance/contract 快照。完整开发和安全边界见
[`docs/equipment-development.md`](docs/equipment-development.md)。

## V1 安全边界

- Target 默认为本机、回环或私网；公网 Target 需要显式设置 `allow_public_target=true`。
- Target 和 Planner 凭据在快照、事件、报告和 checkpoint 中被脱敏。
- 高风险步骤在未审批时不能执行。
- Planner 不能越过 Target、Case、Tool 和预算 allowlist。
- 状态测试数据按 run、tenant、user、session 和 namespace 隔离并清理。
- 服务 API Key 是单密钥部署基础，不等同于完整用户身份系统或 RBAC。

## 生产化路线图

当前版本在保留单 API Key、暂不引入身份平面的前提下，提供以下生产基础：

- PostgreSQL SQLAlchemy 数据库和 PostgreSQL LangGraph checkpoint；
- 基于数据库租约、`FOR UPDATE SKIP LOCKED`、心跳和过期恢复的持久化 Run Job；
- `env:`（仅本地）、受限 `file:` 和 Vault KV v2 `vault:` Secret 引用；
- 会拒绝 SQLite、自动建表、弱控制密钥和未签名外部装备的生产配置门禁；
- request ID、结构化日志、Prometheus 指标、可选 OpenTelemetry OTLP；
- liveness、依赖 readiness、非 root 容器、PostgreSQL/API/worker Compose；
- 校验 checksum 的 PostgreSQL 与装备归档备份/空目标恢复脚本。

以下内容仍不是已交付能力：

- OIDC/JWT、用户体系、RBAC 和审批人组织权限；
- PostgreSQL、Vault、入口、遥测和存储后端自身的跨可用区高可用；
- Kubernetes/Helm、自动水平扩缩容和平台级网络策略；
- 由本仓库直接运营的集中日志、告警后端和 Secret 自动轮换；
- 已在目标生产环境完成的负载、混沌、渗透和灾备恢复证明。

在公网、多租户或关键生产环境部署前，应完成目标环境验证和独立安全评审。

生产部署、升级、回滚、Worker 排空、SLO、告警和灾备步骤见
[`docs/operations/production-runbook.md`](docs/operations/production-runbook.md)。

队列提交示例：

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -H "X-API-Key: $ATTACKER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"request_id":"stateful-20260730-001","kind":"stateful","payload":{"profile":"hardened"}}'
```

Durable Job 不接受 password、token、API key 等原始 Secret 字段；敏感执行必须通过
Provider Instance Secret 引用绑定。身份、OIDC、RBAC 仍明确不在当前范围内。
