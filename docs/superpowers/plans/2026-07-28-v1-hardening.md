# V1 Delivery Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the merged three-stage prototype into a reproducible v1 delivery with automated regression tests, clean static checks, CI, current documentation, protected mutation APIs, and replay support for every run mode.

**Architecture:** Keep SQLite and the existing service/repository boundaries. Add pytest as the regression harness, an optional API-key dependency at the FastAPI router boundary, and replay dispatch that reconstructs persisted datasets and policies while requiring callers to resupply target credentials. Reuse the existing replay table and finding comparison model.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy async, Alembic, LangGraph, pytest, pytest-asyncio, Ruff, Pyright, GitHub Actions.

---

### Task 1: Reproducible development and test environment

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/conftest.py`

- [x] **Step 1: Add the test dependencies**

Add `pytest>=8.4.0` and `pytest-asyncio>=1.0.0` to the `dev` dependency group and configure pytest with `asyncio_mode = "auto"` and `testpaths = ["tests"]`.

- [x] **Step 2: Regenerate and verify the lock**

Run:

```powershell
uv lock
uv sync --locked --python 3.12
```

Expected: both commands exit 0 in the new worktree.

- [x] **Step 3: Add isolated database fixtures**

Create a pytest fixture that builds a temporary `sqlite+aiosqlite` database through `Database`, yields its session factory, and disposes the engine after each test. Tests must not write to repository `data/`.

### Task 2: Static typing regression

**Files:**
- Create: `tests/test_static_contracts.py`
- Modify: `alembic/env.py`

- [x] **Step 1: Preserve the failing type-check evidence**

Run:

```powershell
uv run pyright alembic/env.py
```

Expected before the fix: failure because `object` is not compatible with SQLAlchemy `Connection`.

- [x] **Step 2: Use the concrete SQLAlchemy connection type**

Import `Connection` from `sqlalchemy.engine` and define:

```python
def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
```

- [x] **Step 3: Verify the focused type check**

Run `uv run pyright alembic/env.py`; expected: 0 errors.

### Task 3: Stable finding fingerprints and generic replay

**Files:**
- Create: `app/services/finding_fingerprint.py`
- Create: `app/schemas/replay_schema.py`
- Modify: `app/repositories/run_repository.py`
- Modify: `app/repositories/adaptive_repository.py`
- Modify: `app/repositories/stateful_repository.py`
- Modify: `app/services/run_service.py`
- Modify: `app/services/adaptive_run_service.py`
- Modify: `app/services/replay_service.py`
- Modify: `app/api/replays.py`
- Modify: `app/core/lifespan.py`
- Create: `tests/services/test_finding_fingerprint.py`
- Create: `tests/services/test_replay_service.py`

- [x] **Step 1: Write failing fingerprint tests**

Test that the same stage, case, category, and control flag produces the same SHA-256 fingerprint and that changing the stage changes it.

- [x] **Step 2: Implement the fingerprint helper**

Serialize the four stable fields with sorted JSON and return `sha256(payload).hexdigest()`. Use it when black-box and gray-box findings are persisted; retain phase-three fingerprints and use the helper as a fallback for historical null fingerprints.

- [x] **Step 3: Write failing replay dispatch tests**

Using small fake repositories and run services, verify:

```python
await replay_service.replay(stateful_run_id, ReplayRunRequest(profile="hardened"))
await replay_service.replay(blackbox_run_id, ReplayRunRequest(target=target))
await replay_service.replay(graybox_run_id, ReplayRunRequest(target=target))
```

The tests must require `profile` for stateful sources, require a resupplied `target` for HTTP sources, and reject unsupported modes without making a target call.

- [x] **Step 4: Reconstruct persisted replay inputs**

Add repository methods that read the source `DatasetSnapshotRecord` and `PolicySnapshotRecord` and validate them into:

- `LoadedDataset` plus `RunBudget` for deterministic black-box runs;
- `LoadedGrayBoxDataset` plus `AttackPolicy` for gray-box runs.

No replay path may read a historical LangGraph checkpoint or reuse stored credentials.

- [x] **Step 5: Add explicit dataset execution entry points**

Expose `run_dataset` methods on deterministic black-box and deterministic gray-box services. They accept validated persisted datasets, caller-resupplied targets, stored budgets/policies, and a replay mode while preserving the existing public `run` behavior.

- [x] **Step 6: Dispatch and compare all supported modes**

Dispatch `stateful*`, `deterministic`, `adaptive_graybox`, and `deterministic_graybox` source modes. Persist the source/replay link and classify fixed/new/persistent/regressed with stable or fallback fingerprints.

- [x] **Step 7: Run focused replay tests**

Run:

```powershell
uv run pytest tests/services/test_finding_fingerprint.py tests/services/test_replay_service.py -q
```

Expected: all focused tests pass.

### Task 4: Service API-key protection

**Files:**
- Create: `app/api/security.py`
- Modify: `conf/settings.py`
- Modify: `main.py`
- Modify: `.env.example`
- Create: `tests/api/test_api_security.py`

- [x] **Step 1: Write failing authentication tests**

Create a minimal FastAPI route using `Depends(require_api_key)` and assert:

- no configured service key keeps local development open;
- a configured key rejects a missing or incorrect `X-API-Key` with HTTP 401;
- the configured key accepts the matching header.

- [x] **Step 2: Implement optional constant-time API-key validation**

Add `SecuritySettings.api_key: SecretStr | None`, use `APIKeyHeader(auto_error=False)`, and compare values with `secrets.compare_digest`. Error responses must not reveal the expected key.

- [x] **Step 3: Protect non-health routers**

Attach the dependency to tests, runs, approvals, and replay routers when included in `create_app`. Leave `/health`, `/docs`, and `/openapi.json` public.

- [x] **Step 4: Run focused API security tests**

Run `uv run pytest tests/api/test_api_security.py -q`; expected: all cases pass.

### Task 5: Core regression suite

**Files:**
- Create: `tests/services/test_sample_loaders.py`
- Create: `tests/services/test_policy_service.py`
- Create: `tests/services/test_stateful_adapters.py`
- Create: `tests/services/test_report_service.py`
- Create: `tests/repositories/test_migrations.py`

- [x] **Step 1: Characterize all 30 cases**

Load phase-one, phase-two, and phase-three YAML through their real loaders and assert counts 12, 10, and 8, unique IDs, and attack/control coverage.

- [x] **Step 2: Cover policy boundaries**

Exercise target/case allowlists, approval-required critical cases, target-call budgets, and repeated planner decisions with real policy service objects.

- [x] **Step 3: Cover state isolation and cleanup**

Use the SQLite-backed memory/RAG adapters to verify tenant/user/session/namespace isolation and that cleanup changes only the requested test scope.

- [x] **Step 4: Cover report reconstruction**

Persist a minimal run with evidence-linked findings and assert JSON and Markdown reports are rebuilt from SQLite without checkpoint state.

- [x] **Step 5: Cover migrations**

Upgrade an empty temporary SQLite database to Alembic head and assert the v1 run, event, finding, state, retrieval, and replay tables exist.

### Task 6: Continuous integration

**Files:**
- Create: `.github/workflows/ci.yml`

- [x] **Step 1: Add a Python 3.12 CI job**

Use checkout, install uv, `uv sync --locked --python 3.12`, then execute:

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
uv run python -m compileall app conf alembic
```

- [x] **Step 2: Keep CI deterministic**

Set `PYTHONUTF8=1`, disable external network target execution in tests, and use only temporary SQLite databases.

### Task 7: Delivery documentation

**Files:**
- Rewrite: `README.md`
- Modify: `.env.example`

- [x] **Step 1: Replace stale MVP status**

Document the completed 12/10/8 case stages, SQLite evidence model, adaptive approval recovery, state isolation, reports, and replay coverage.

- [x] **Step 2: Add executable setup and API examples**

Document Python 3.12, `uv sync --locked`, Alembic upgrade, Uvicorn startup, optional `SECURITY__API_KEY`, `X-API-Key`, run endpoints, approval resume, replay requests, and report endpoints.

- [x] **Step 3: State production boundaries**

Clearly list SQLite/single-instance assumptions and the future PostgreSQL, queue, multi-instance locking, secrets manager, observability, backup, and retention work without claiming these are part of v1.

### Task 8: Full verification and publication

**Files:**
- Modify: this plan, marking completed checkboxes

- [x] **Step 1: Run the clean full gate**

Run:

```powershell
uv sync --locked --python 3.12
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
uv run python -m compileall app conf alembic
uv run alembic upgrade head
```

Expected: every command exits 0.

- [x] **Step 2: Review repository scope**

Run `git status --short`, `git diff --check`, and inspect the complete diff. Confirm no secrets, generated databases, logs, or unrelated files are staged.

- [ ] **Step 3: Commit and push**

Commit the verified changes on `agent/v1-hardening` and push with upstream tracking. Do not merge or create a PR unless separately requested.
