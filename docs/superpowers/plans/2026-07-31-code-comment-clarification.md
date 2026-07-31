# Code Comment Clarification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve the production Python code comments and docstrings so maintainers can understand module responsibilities, security boundaries, persistence invariants, and recovery behavior without changing runtime behavior.

**Architecture:** Add concise Chinese module docstrings across non-empty production modules, then add targeted class/method docstrings and inline comments only where control flow or invariants are not evident from names. Preserve signatures, statements, formatting, imports, schemas, migrations, and tests.

**Tech Stack:** Python 3.12, FastAPI, LangGraph, Pydantic, SQLAlchemy Async, PostgreSQL/SQLite, Ruff, Pyright, Pytest.

---

### Task 1: API, schema, and configuration boundaries

**Files:**
- Modify: `app/api/approvals.py`
- Modify: `app/api/equipment.py`
- Modify: `app/api/health.py`
- Modify: `app/api/jobs.py`
- Modify: `app/api/metrics.py`
- Modify: `app/api/replays.py`
- Modify: `app/api/runs.py`
- Modify: `app/api/security.py`
- Modify: `app/api/tests.py`
- Modify: `app/schemas/adaptive_agent_schema.py`
- Modify: `app/schemas/attack_sample_schema.py`
- Modify: `app/schemas/attack_state_schema.py`
- Modify: `app/schemas/equipment_schema.py`
- Modify: `app/schemas/graybox_schema.py`
- Modify: `app/schemas/job_schema.py`
- Modify: `app/schemas/judge_schema.py`
- Modify: `app/schemas/model_provider_schema.py`
- Modify: `app/schemas/prompt_schema.py`
- Modify: `app/schemas/replay_schema.py`
- Modify: `app/schemas/run_control_schema.py`
- Modify: `app/schemas/run_schema.py`
- Modify: `app/schemas/stateful_schema.py`
- Modify: `app/schemas/target_schema.py`
- Modify: `conf/settings.py`
- Modify: `conf/logging.py`

- [ ] **Step 1: Add module-level responsibility comments**

Add a short Chinese module docstring before imports that explains which transport or data contract the file owns. Schema docstrings must state whether the models describe trusted Core facts, untrusted target input, or persisted snapshots.

- [ ] **Step 2: Clarify security-sensitive validators and API dependencies**

Add targeted comments around API-key enforcement, target validation, strict `extra="forbid"` models, and production configuration gates. Do not restate obvious field names.

- [ ] **Step 3: Verify syntax and formatting**

Run: `uv run ruff format --check app/api app/schemas conf`

Expected: exit code 0.

### Task 2: Deterministic, stateful, and replay services

**Files:**
- Modify: `app/services/attack_executor.py`
- Modify: `app/services/derived_case_service.py`
- Modify: `app/services/evaluator_service.py`
- Modify: `app/services/finding_fingerprint.py`
- Modify: `app/services/graybox_connector.py`
- Modify: `app/services/graybox_evaluator_service.py`
- Modify: `app/services/judge_engine.py`
- Modify: `app/services/observation_normalizer.py`
- Modify: `app/services/policy_service.py`
- Modify: `app/services/replay_service.py`
- Modify: `app/services/report_service.py`
- Modify: `app/services/run_control.py`
- Modify: `app/services/run_service.py`
- Modify: `app/services/sample_loader.py`
- Modify: `app/services/stateful_adapters.py`
- Modify: `app/services/stateful_evaluator_service.py`
- Modify: `app/services/stateful_run_service.py`
- Modify: `app/services/target_binding.py`
- Modify: `app/services/target_connector/http_connector.py`
- Modify: `app/services/tool_trace_adapter.py`

- [ ] **Step 1: Document service ownership and evidence flow**

Explain at module/class level which layer owns transport, evaluation, persistence, reporting, or replay. Make the distinction between an observation, Evaluation, Finding, and persisted Evidence explicit where those objects cross a boundary.

- [ ] **Step 2: Document non-obvious safety and replay invariants**

Add comments for deterministic orchestration versus deterministic target output, response-size enforcement, target-binding canonicalization, stable finding fingerprints, replay comparison semantics, and state cleanup.

- [ ] **Step 3: Verify targeted service checks**

Run: `uv run ruff check app/services`

Expected: exit code 0.

### Task 3: Adaptive workflow and persistence

**Files:**
- Modify: `app/workflows/attack_graph.py`
- Modify: `app/workflows/attack_state.py`
- Modify: `app/services/adaptive_observability.py`
- Modify: `app/services/adaptive_run_service.py`
- Modify: `app/services/candidate_builder.py`
- Modify: `app/services/finish_gate_service.py`
- Modify: `app/services/hypothesis_service.py`
- Modify: `app/services/prompt_governance.py`
- Modify: `app/infrastructure/model_adapter.py`
- Modify: `app/infrastructure/model_provider.py`
- Modify: `app/repositories/adaptive_repository.py`

- [ ] **Step 1: Explain graph nodes and trust boundaries**

Add comments describing Planner as a candidate selector, Policy as the authorization boundary, observations as untrusted data, SQL as the business fact source, and checkpoint as control-flow recovery state.

- [ ] **Step 2: Explain recovery and idempotency invariants**

Document stable operation IDs, resume-time runtime credential re-supply, checkpoint/thread matching, candidate snapshot freshness, repeated-action detection, and evidence-reference validation.

- [ ] **Step 3: Verify adaptive modules**

Run: `uv run pyright app/workflows app/services/adaptive_run_service.py app/services/adaptive_observability.py app/repositories/adaptive_repository.py`

Expected: exit code 0.

### Task 4: Equipment extension and supply-chain controls

**Files:**
- Modify: `app/equipment/catalog.py`
- Modify: `app/equipment/development.py`
- Modify: `app/equipment/json_schema.py`
- Modify: `app/equipment/metrics.py`
- Modify: `app/equipment/runner.py`
- Modify: `app/equipment/sdk.py`
- Modify: `app/equipment/security.py`
- Modify: `app/equipment/worker.py`
- Modify: `app/services/equipment_service.py`
- Modify: `app/services/harness_service.py`
- Modify: `app/repositories/equipment_repository.py`

- [ ] **Step 1: Explain package and trust ownership**

Document Core-owned `trusted_builtin` provenance, enterprise subprocess limits, untrusted container requirements, checksum/signature/revocation responsibilities, and why manifest validation happens before Python import.

- [ ] **Step 2: Explain broker, execution, and cleanup invariants**

Document frozen Run bindings, Provider Instance revisions, secret resolution scope, capability request IDs, input fingerprints, `in_doubt` behavior, transactional Resource Lease creation, and recovery cleanup.

- [ ] **Step 3: Verify equipment modules**

Run: `uv run ruff check app/equipment app/services/equipment_service.py app/services/harness_service.py app/repositories/equipment_repository.py`

Expected: exit code 0.

### Task 5: Repositories, runtime, observability, and sandbox

**Files:**
- Modify: `app/core/lifespan.py`
- Modify: `app/infrastructure/checkpoint.py`
- Modify: `app/infrastructure/database.py`
- Modify: `app/infrastructure/secrets.py`
- Modify: `app/models.py`
- Modify: `app/observability.py`
- Modify: `app/repositories/job_repository.py`
- Modify: `app/repositories/run_repository.py`
- Modify: `app/repositories/stateful_repository.py`
- Modify: `app/runtime.py`
- Modify: `app/services/job_service.py`
- Modify: `app/cli.py`
- Modify: `sandbox/graybox_target.py`

- [ ] **Step 1: Document storage and lifecycle responsibilities**

Clarify transaction ownership, unique sequence and operation constraints, PostgreSQL lease claiming, advisory locks, startup ordering, shutdown behavior, and why checkpoint is not the Evidence source.

- [ ] **Step 2: Document operational and sandbox limitations**

Explain secret backends, metrics/logging correlation, durable job retry semantics, and that the local gray-box target is synthetic demonstration code rather than a production security result.

- [ ] **Step 3: Verify infrastructure modules**

Run: `uv run ruff check app/core app/infrastructure app/repositories app/runtime.py app/observability.py app/cli.py sandbox`

Expected: exit code 0.

### Task 6: Whole-project behavior-preservation verification

**Files:**
- Verify only: all modified production Python files
- Do not modify: `tests/`
- Do not modify: `alembic/versions/`

- [ ] **Step 1: Confirm changes are comment/docstring only**

Run: `git diff --word-diff=porcelain -- app conf sandbox`

Expected: all additions and removals are comments, docstrings, or formatting caused by those comments; no executable statement, signature, schema field, or literal changes.

- [ ] **Step 2: Run formatting, lint, type, compile, and existing tests**

Run:

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run python -m compileall -q app conf sandbox
uv run pytest -q
```

Expected: every command exits with code 0. No test files are added or changed.

- [ ] **Step 3: Review comment usefulness**

Search for generic comments that merely repeat the next symbol name. Remove comments such as “运行服务”“保存数据” when they do not explain ownership, reason, invariant, failure semantics, or trust boundary.
