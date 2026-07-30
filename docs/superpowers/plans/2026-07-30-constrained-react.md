# Constrained ReAct Implementation Plan

> **For agentic workers:** Execute this plan task-by-task in the current session. The repository Test Addition Policy forbids adding or modifying tests for this feature; use existing tests and disposable smoke checks for verification.

**Goal:** Complete the constrained ReAct requirements in section 18 of `target/attacker_agent_optimization_requirements.md` without weakening the deterministic Core.

**Architecture:** Keep LangGraph as the Adaptive orchestration loop and keep authorization, execution, evaluation, persistence, and stopping in deterministic services. Extend only the missing fact flow, loop controls, prompt resource governance, and comparison reporting; reuse the existing Candidate, Policy, Approval, Observation, Evidence, Finish Gate, checkpoint, and Operation ID boundaries.

**Tech Stack:** Python 3.12, Pydantic 2, LangGraph, SQLAlchemy Async, SQLite, HTTPX, FastAPI.

---

### Task 1: Feed persisted ReAct facts into Planner

**Files:**
- Modify: `app/schemas/graybox_schema.py`
- Modify: `app/repositories/adaptive_repository.py`
- Modify: `app/workflows/attack_graph.py`
- Modify: `app/infrastructure/model_adapter.py`

- [x] Add bounded structured Coverage and information-gain references to `PlannerContext`.
- [x] Reconstruct the latest Coverage statuses and their persisted Event references in `load_adaptive_facts`.
- [x] Pass those facts into every Planner call.
- [x] Include Coverage and information-gain references in `PlannerCallSnapshot.input_fact_refs`.
- [x] Restrict `PlannerDecision.reason_code` to codes compatible with `execute` or `finish`.

### Task 2: Integrate no-gain and repeated-state stopping into LangGraph

**Files:**
- Modify: `app/schemas/graybox_schema.py`
- Modify: `app/workflows/attack_state.py`
- Modify: `app/services/adaptive_run_service.py`
- Modify: `app/services/hypothesis_service.py`
- Modify: `app/workflows/attack_graph.py`

- [x] Add explicit policy thresholds and graph-state counters for consecutive no-gain steps and repeated normalized states.
- [x] Derive the state fingerprint only from persisted Coverage, Hypothesis, Finding, and Evidence facts.
- [x] Update counters after `Evaluation` and fact persistence, not from Planner predictions.
- [x] Stop deterministically with distinct terminal reasons when either threshold is reached.

### Task 3: Use the versioned Core Planner prompt

**Files:**
- Modify: `app/schemas/graybox_schema.py`
- Modify: `app/infrastructure/model_adapter.py`
- Reuse: `app/prompts/planner_v1.txt`

- [x] Load the Planner system prompt from the Core-owned UTF-8 resource.
- [x] Verify its expected SHA-256 checksum before each physical model call.
- [x] Bind configuration and call snapshots to the actual template version and checksum.
- [x] Keep the Planner request as strict structured JSON with no caller-supplied system prompt.

### Task 4: Expand persisted ReAct observability and comparison

**Files:**
- Modify: `app/services/report_service.py`

- [x] Aggregate accepted/rejected Planner decisions, Finish Gate rejections, actual information gain, and traceable decision snapshots from persisted events.
- [x] Report verified DerivedCase IDs separately from unverified generated cases.
- [x] Extend Adaptive/Deterministic comparison with duration, evidence-link quality, findings, coverage, and actual information-gain metrics available in persisted facts.
- [x] Avoid treating Planner-predicted information gain or unverified DerivedCase output as effective coverage.

### Task 5: Verification

**Files:**
- Verify only; do not add or modify test files.

- [x] Run focused disposable smoke checks for Planner validation, persisted fact propagation, loop/no-gain stopping, prompt checksum validation, and report aggregation.
- [x] Run `uv run ruff check .`.
- [x] Run scoped `uv run ruff format --check` for changed Python files; the full-repository check still reports eight unrelated pre-existing files.
- [x] Run `uv run pyright app conf alembic`; the full-repository command still scans the unrelated `data/adaptive-agent-hardening` tree.
- [x] Run `uv run pytest -q`.
- [x] Run `uv run python -m compileall -q app conf alembic`.
- [x] Run `git diff --check` and review every changed file against section 18.
