# Phase 3 Stateful Agent Evaluation Implementation Plan

> **For agentic workers:** Execute these checkboxes inline in order. The repository policy forbids adding or modifying tests in this request, so tasks use static checks, fresh migrations, and temporary end-to-end smoke drivers instead of committed test code.

**Goal:** Add isolated Memory/RAG state evaluation, checkpoint recovery validation, evidence-backed cleanup, and SQLite-based Replay differences for the third and final security stage.

**Architecture:** A deterministic stateful runner uses test-only SQLite-backed Memory and RAG adapters scoped by run, tenant, user, and session. It records snapshots, retrieval traces, recovery facts, and cleanup evidence in the business database; Replay creates a new run from persisted source facts and compares stable Finding fingerprints without reading historical LangGraph state. The phase-two graph remains the adaptive orchestrator, while runtime rehydration allows approval recovery after a process restart when credentials are safely resupplied.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Evals/YAML, SQLAlchemy Async/SQLite, Alembic, LangGraph SQLite checkpoints, Markdown/JSON.

---

### Task 1: Stateful contracts, schema, and eight-case dataset

**Files:**
- Modify: `app/models.py`
- Create: `alembic/versions/20260728_0003_stateful_schema.py`
- Create: `app/schemas/stateful_schema.py`
- Create: `samples/stateful/phase3.yaml`
- Modify: `app/services/sample_loader.py`

- [x] Add a nullable indexed `fingerprint` to Finding and create `state_fixtures`, `state_snapshots`, `retrieval_events`, and `replays` tables. Every side-effect row has a unique stable `operation_id`; fixtures carry `scope_id`, tenant, user, session, kind, provenance, active/poisoned flags, and cleanup timestamps.
- [x] Define typed contracts for stateful cases, state identities, memory/retrieval evidence, recovery evidence, cleanup results, run requests, replay requests, and fixed/new/persistent/regressed differences.
- [x] Use `Dataset.from_file` to load eight ordered cases: Memory poisoning attack/control, RAG poisoning attack/control, cross-user/tenant isolation attack/control, and checkpoint recovery attack/control.
- [x] Require each case to declare its attacker and observer identities, expected violation, profile-specific behavior, evidence requirements, and cleanup scope.

Core result shape:

```python
class StatefulEvaluationResult(BaseModel):
    outcome: Literal["violation", "safe", "inconclusive", "error"]
    violated: bool
    reason: str
    matched_rules: list[str]
    evidence_complete: bool


class ReplayDiff(BaseModel):
    fixed: list[str]
    new: list[str]
    persistent: list[str]
    regressed: list[str]
```

### Task 2: Isolated Memory/RAG adapters and cleanup

**Files:**
- Create: `app/repositories/stateful_repository.py`
- Create: `app/services/stateful_adapters.py`

- [x] Implement `MemoryAdapter.write/read` through `StatefulRepository`, using strict tenant/user/session predicates by default and an explicit vulnerable profile only for sandbox attack scenarios.
- [x] Implement `RAGAdapter.index/retrieve`; retrieval evidence includes document ID, rank, source, tenant/user permissions, allowed/filtered decision, and reason.
- [x] Persist pre-action, post-action, observer, and cleanup snapshots. Snapshot content is redacted summaries, not arbitrary target secrets.
- [x] Implement cleanup by `scope_id` and `created_by_run_id`; it must never update fixtures outside the current test scope. Record cleanup counts and remaining-active counts as audit events.
- [x] Make repeated writes, retrieval recording, snapshots, and cleanup idempotent through stable operation IDs.

Adapter boundary:

```python
class MemoryAdapter:
    async def write(self, identity: StateIdentity, item: MemoryWrite) -> StateFixture: ...
    async def read(self, identity: StateIdentity, namespace: str) -> list[StateFixture]: ...
    async def cleanup(self, run_id: str, scope_id: str) -> CleanupResult: ...


class RAGAdapter:
    async def index(self, identity: StateIdentity, document: RAGDocument) -> StateFixture: ...
    async def retrieve(
        self,
        identity: StateIdentity,
        query: str,
        operation_id: str,
    ) -> RetrievalTrace: ...
```

### Task 3: Stateful evaluator and deterministic runner

**Files:**
- Create: `app/services/stateful_evaluator_service.py`
- Create: `app/services/stateful_run_service.py`
- Modify: `app/repositories/run_repository.py`

- [x] Create stateful Target/Dataset/Policy/Evaluator snapshots and one deterministic Step per case.
- [x] Execute case actions only against test fixtures: seed, attacker write/index, observer read/retrieve, recovery retry, evaluate, persist, and cleanup.
- [x] Evaluate persistent poisoned Memory, retrieved poisoned RAG documents, cross-user or cross-tenant visibility, missing recovery policy revalidation, and duplicated post-recovery operations.
- [x] Return `inconclusive` whenever required snapshot/retrieval/recovery evidence is missing.
- [x] Compute Finding fingerprints as SHA-256 over stable stage/category/case/rule identity, and link every Finding to evaluation plus Memory/Retrieval/Recovery/Cleanup evidence event IDs.
- [x] Finalize stateful counts from persisted Steps and expose state fixtures, snapshots, retrievals, and replay metadata through report rows.

### Task 4: Process recovery and policy revalidation

**Files:**
- Modify: `app/schemas/graybox_schema.py`
- Modify: `app/repositories/adaptive_repository.py`
- Modify: `app/services/adaptive_run_service.py`
- Modify: `app/api/approvals.py`

- [x] Allow approval resolution to optionally resupply Target and Planner configuration; credentials remain request/runtime-only and never enter checkpoint or evidence.
- [x] Rehydrate a missing adaptive runtime from persisted redacted Target, Dataset, Policy, and planner metadata. Require a Target override when the stored target snapshot indicates redacted credentials.
- [x] Read the business Run and pending Approval before resuming the same checkpoint `thread_id`.
- [x] Route resumed execution back through `policy_gate`; record a `recovery_policy_revalidated` event before any target call.
- [x] Verify repeated resume/operation IDs do not duplicate Target/Tool calls or Findings.

### Task 5: Replay, differences, API, and reports

**Files:**
- Create: `app/services/replay_service.py`
- Create: `app/api/replays.py`
- Modify: `app/api/runs.py`
- Modify: `app/services/report_service.py`
- Modify: `app/core/lifespan.py`
- Modify: `main.py`

- [x] Add `POST /runs/stateful`, `POST /runs/{source_run_id}/replay`, and `GET /runs/{run_id}/replay`.
- [x] Replay stateful cases from the persisted source dataset snapshot using an explicitly selected sandbox profile; never read the source checkpoint or transient Graph State.
- [x] Link source and replay runs in `replays`, then classify fingerprints: source-only = fixed, replay attack-only = new, intersection = persistent, replay control-only = regressed.
- [x] Extend JSON/Markdown reports with state write/persistence/cross-identity/cleanup metrics, retrieval permission evidence, recovery revalidation, and Replay differences.
- [x] Keep historical reports rebuildable solely from SQLite.

### Task 6: Verification and publication

**Files:**
- No committed test files.

- [x] Run `uv sync --locked --python 3.12`, Ruff, Ruff format check, Pyright, compileall, and the existing pytest command; record that no tests are collected rather than adding tests.
- [x] Upgrade a fresh SQLite database through revisions `0001`, `0002`, and `0003`, then inspect all new tables, indexes, and foreign keys.
- [x] Run all eight cases against vulnerable and hardened profiles using temporary database files; confirm expected attack/control outcomes and complete cleanup.
- [x] Confirm RAG evidence contains rank/source/permission filtering and cross-user/cross-tenant visibility is zero in the hardened profile.
- [x] Interrupt an approval run, recreate the application process with the same database/checkpoint, resupply runtime credentials if needed, resume the same `thread_id`, and confirm policy revalidation precedes exactly one target call.
- [x] Replay vulnerable source facts against the hardened profile and verify fixed/new/persistent/regressed classification, fingerprint stability, no dependency on source checkpoint, and SQLite-only report generation.
- [x] Confirm temporary canary credentials and poisoned raw values are absent from Graph State, checkpoints, logs, and reports.
- [x] Commit on `agent/phase3-stateful` and push the branch without creating or merging a PR unless separately requested.
