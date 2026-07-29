# Phase 2 Gray-Box Agent Evaluation Implementation Plan

> **For agentic workers:** Execute these checkboxes inline in order. The repository policy forbids adding or modifying tests in this request, so each task uses static checks, migration checks, and temporary end-to-end smoke drivers instead of committing test code.

**Goal:** Add a bounded gray-box evaluation mode that uses LangGraph to select approved cases, enforces policy and human approval before target execution, evaluates Tool/Policy traces, and persists evidence-backed authorization findings.

**Architecture:** A narrow Planner Model Adapter receives only approved case summaries and returns a structured decision. A LangGraph `StateGraph` coordinates planning, policy, optional `interrupt`, target execution, trace evaluation, persistence, and bounded continuation; deterministic services remain outside the graph. SQLite business tables remain the audit source, while a separate SQLite LangGraph checkpointer stores resumable control-flow state.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Evals/YAML, LangGraph, AsyncSqliteSaver, SQLAlchemy Async/SQLite, httpx, Markdown/JSON.

---

### Task 1: Gray-box contracts, dataset, and persistence

**Files:**
- Modify: `pyproject.toml`
- Modify: `conf/settings.py`
- Modify: `app/models.py`
- Create: `alembic/versions/20260728_0002_gray_box_schema.py`
- Create: `app/schemas/graybox_schema.py`
- Create: `samples/graybox/phase2.yaml`
- Modify: `app/services/sample_loader.py`

- [x] Add `langgraph>=1.2,<1.3` and `langgraph-checkpoint-sqlite>=3.1,<4`, then lock for Python 3.12.
- [x] Add run fields `thread_id`, `baseline_run_id`, planner/tool/policy/approval counters, plus an `approvals` table with one stable approval per run/case.
- [x] Define `ToolEvent`, `PolicyEvent`, `ApprovalEvent`, `ToolTraceEnvelope`, `GrayBoxCase`, `AttackPolicy`, `PlannerContext`, `PlannerDecision`, `AttackGraphState`, and adaptive request/response schemas. Tool arguments/results are summaries, not arbitrary raw objects.
- [x] Add ten ordered Pydantic Evals cases covering unauthorized tools, dangerous parameters/resource IDs, approval bypass, tool-output injection, and repeated planner/tool loops. Each method includes attack and safe/control behavior.
- [x] Load the dataset through `Dataset.from_file`, validate stable IDs, preserve file order, and reject unknown requested IDs.

### Task 2: Trace adapter, policy, evaluator, and sandbox

**Files:**
- Create: `app/services/tool_trace_adapter.py`
- Create: `app/services/policy_service.py`
- Create: `app/services/graybox_evaluator_service.py`
- Create: `app/services/graybox_connector.py`
- Create: `sandbox/graybox_target.py`

- [x] Parse target-provided trace envelopes into typed, field-redacted events; malformed or incomplete traces produce `inconclusive`, never an inferred authorization violation.
- [x] Enforce target ID, case allowlist, remaining graph steps, target-call budget, and severity-based approval before every target call.
- [x] Evaluate executed disallowed tools, forbidden parameter patterns/resource IDs, missing approval, untrusted tool-output follow-up, and repeated tool-call signatures.
- [x] Send a stable `Idempotency-Key` and gray-box case context to targets; keep credentials out of graph state and evidence.
- [x] Provide a local FastAPI sandbox whose mock tool registry records policy and execution events but performs no real external side effects.

### Task 3: Repository and idempotent gray-box operations

**Files:**
- Modify: `app/repositories/run_repository.py`
- Create: `app/repositories/adaptive_repository.py`

- [x] Create adaptive or gray-box deterministic runs with Target/Dataset/Policy snapshots and a stable `thread_id`.
- [x] Append `planner_decided/rejected`, `policy_allowed/denied`, `approval_requested/resolved`, `target_called`, `tool_requested/completed`, `evaluation_completed`, `finding_created`, and terminal events with continuous run-local sequences.
- [x] Use unique operation IDs for case steps, approvals, target calls, tool trace events, and findings. A repeated committed operation returns stored facts and never calls the target again.
- [x] Persist model response and Tool/Policy/Approval trace event IDs on every gray-box Finding.
- [x] Store planner requests/tokens separately from target/tool calls and expose pending approvals.

### Task 4: Planner adapter and LangGraph workflow

**Files:**
- Create: `app/infrastructure/model_adapter.py`
- Create: `app/workflows/__init__.py`
- Create: `app/workflows/attack_state.py`
- Create: `app/workflows/attack_graph.py`
- Create: `app/services/adaptive_run_service.py`

- [x] Implement a deterministic offline planner and an optional OpenAI-compatible HTTP JSON planner behind the same typed adapter; only the latter records model tokens.
- [x] Reject planner outputs outside `allowed_case_ids`, already-completed IDs, missing IDs, or unsupported actions before policy execution.
- [x] Build nodes `initialize_run -> plan_next_case -> policy_gate -> human_review/execute/skip -> evaluate -> persist -> decide_next -> finalize`.
- [x] Use `interrupt()` only after an idempotent approval request is persisted. Resume with `Command(resume=...)` and route back through `policy_gate` before execution.
- [x] Stop on graph-step, duration, target-call, planner-failure, or repeated-decision limits and persist the exact terminal reason.
- [x] Keep graph state limited to IDs, counters, approved case summaries, and redacted Finding summaries.

### Task 5: APIs and comparison reports

**Files:**
- Create: `app/api/approvals.py`
- Modify: `app/api/runs.py`
- Modify: `app/services/report_service.py`
- Modify: `app/core/lifespan.py`
- Modify: `main.py`

- [x] Add `POST /runs/adaptive`, `GET /runs/{run_id}/approvals`, and `POST /runs/{run_id}/approvals/{approval_id}`.
- [x] Resolve approvals exactly once, record the reviewer/reason, and resume the same graph `thread_id`.
- [x] Add gray-box deterministic baseline execution using the same Case, connector, policy, trace adapter, and evaluator without LangGraph or a planner.
- [x] When `baseline_run_id` is present, report adaptive-only Finding case IDs and keep planner calls/tokens separate from target/tool usage.
- [x] Create/dispose the async SQLite checkpointer and adaptive runtime services during FastAPI lifespan without import-time connections.

### Task 6: Verification

**Files:**
- No committed test files.

- [x] Run `uv sync --locked --python 3.12`, Ruff, Pyright, and compileall.
- [x] Upgrade a fresh SQLite database through Alembic revisions `0001` and `0002` and inspect the resulting business tables.
- [x] Start the local sandbox and API using temporary runtime files, execute all ten deterministic gray-box cases, then run adaptive mode through an approval interrupt/resume.
- [x] Verify no target/tool side effect occurs before approval, all calls have policy evidence, Event sequences are continuous, repeated operation IDs do not duplicate calls/findings, loop limits terminate, and every Finding links response plus Tool/Policy evidence.
- [x] Generate an Adaptive/Deterministic JSON and Markdown comparison from SQLite and verify secrets are absent.
