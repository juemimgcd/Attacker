# Phase 1 Black-Box Evaluation Implementation Plan

> **For agentic workers:** Implement each checkbox in order. The repository policy forbids adding or modifying tests for this change, so verification uses existing checks and a temporary local smoke target.

**Goal:** Complete Attacker's first formal delivery phase: deterministic black-box evaluations backed by SQLite evidence and reproducible reports.

**Architecture:** FastAPI delegates to a deterministic run service. The service loads a Pydantic Evals YAML dataset, executes approved HTTP request/response cases within a fixed budget, evaluates responses with deterministic rules, and atomically persists run events and evidence-backed findings through SQLAlchemy Async repositories. Reports query SQLite only.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Evals, httpx, SQLAlchemy Async, aiosqlite, Alembic, YAML, Markdown/JSON.

---

### Task 1: Runtime and database foundation

**Files:**
- Modify: `pyproject.toml`
- Modify: `conf/settings.py`
- Create: `app/infrastructure/database.py`
- Create: `app/models.py`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/20260728_0001_phase1_schema.py`

- [ ] Set the supported runtime to Python 3.12 and add SQLAlchemy, aiosqlite, Alembic, and Pydantic Evals.
- [ ] Define Target, DatasetSnapshot, PolicySnapshot, Run, Step, Event, and Finding ORM tables with foreign keys, run-local event ordering, and unique operation IDs.
- [ ] Configure an async engine/session factory and an initial Alembic revision that creates the same schema.
- [ ] Verify with `uv lock --python 3.12` and `uv run --python 3.12 alembic upgrade head`.

### Task 2: Dataset and evaluation contracts

**Files:**
- Modify: `app/schemas/attack_sample_schema.py`
- Modify: `app/schemas/judge_schema.py`
- Create: `app/schemas/run_schema.py`
- Modify: `app/services/sample_loader.py`
- Create: `app/services/evaluator_service.py`
- Create: `samples/blackbox/phase1.yaml`

- [ ] Define typed black-box inputs, turns, evaluator rules, expected outcome, control metadata, evidence requirements, cleanup, redaction, and budgets.
- [ ] Load the YAML through `pydantic_evals.Dataset.from_file`, validate every input, preserve file order, and support a stable case-ID subset.
- [ ] Add 12 cases covering direct injection, system-prompt leakage, sensitive canaries, multi-turn pollution, and resource consumption, with attack and normal/refusal controls.
- [ ] Implement deterministic transport, pattern, refusal, latency, and response-size evaluation with explicit `violation`, `refused`, `safe`, `error`, and `budget_aborted` outcomes.
- [ ] Verify dataset loading and all required categories using a read-only Python command.

### Task 3: SQLite repositories and deterministic runner

**Files:**
- Create: `app/repositories/run_repository.py`
- Modify: `app/services/target_connector/http_connector.py`
- Create: `app/services/run_service.py`

- [ ] Persist target, dataset, and policy snapshots before starting a run.
- [ ] Persist run-start, target-call, evaluation, finding, budget, and run-completion events with continuous sequence numbers.
- [ ] Make each case operation idempotent: an existing operation ID returns its stored result without another target call or duplicate Event/Finding.
- [ ] Execute cases in dataset order, support multi-turn and repeated calls, enforce case/call/time/response budgets, and redact secrets before evidence persistence.
- [ ] Finalize aggregate counts for network errors, refusals, violations, safe results, budget aborts, false positives, and defense overblocking.

### Task 4: Reports, API wiring, and lifecycle

**Files:**
- Create: `app/services/report_service.py`
- Create: `app/api/runs.py`
- Modify: `app/api/tests.py`
- Modify: `app/core/lifespan.py`
- Modify: `main.py`

- [ ] Add deterministic-run creation, run detail, JSON report, and Markdown report endpoints.
- [ ] Keep the single-case dry-run endpoint and migrate its saved form to the SQLite runner.
- [ ] Create and dispose the database and services in FastAPI lifespan without import-time I/O.
- [ ] Generate both report formats exclusively from persisted SQLite rows and Evidence references.

### Task 5: Verification

**Files:**
- No test files are added or modified.

- [ ] Run Alembic against a temporary SQLite file and inspect all seven tables.
- [ ] Run Ruff and Pyright over the implementation and fix task-related findings.
- [ ] Start a temporary local HTTP target, run the 12-case dataset through FastAPI/service code, regenerate JSON and Markdown reports from SQLite, and confirm continuous events plus 100% Finding-to-Evidence links.
- [ ] Review `git diff` to ensure unrelated user changes remain untouched.
