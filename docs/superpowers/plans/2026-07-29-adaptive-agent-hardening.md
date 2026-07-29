# Adaptive Agent Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining security, state-control, evaluator-integrity, and persisted-evidence gaps confirmed on `origin/master`.

**Architecture:** Keep validation in the existing Core boundaries: Prompt Governance owns redaction, Run Control owns terminal-state invariants, Judge Engine owns evaluator provenance, and Run Repository owns event-backed Evidence. Reuse the existing deterministic single-run service and SQLAlchemy event schema; do not add a migration or a second persistence model.

**Tech Stack:** Python 3.12, Pydantic 2, FastAPI, SQLAlchemy Async, SQLite, Ruff, Pyright, Pytest.

**Repository policy:** Do not add or modify test files. Each RED/GREEN step below uses a disposable stdin smoke script, followed by the existing test suite.

---

### Task 1: Prompt redaction and deny-all profile configuration

**Files:**
- Modify: `app/services/prompt_governance.py`

- [ ] **Step 1: Reproduce the credential leak and empty-profile fallback**

Run:

```powershell
@'
from app.services.prompt_governance import PromptGovernanceService, prompt_governance_service
print(prompt_governance_service._redact("Authorization: Basic Zm9vOmJhcg=="))
print(sorted(PromptGovernanceService([])._profiles))
'@ | python -
```

Expected RED output contains `Zm9vOmJhcg==` and two default profile IDs.

- [ ] **Step 2: Implement complete authorization-line redaction**

Add an authorization-assignment pattern that consumes the entire header value through the line boundary, then apply generic sensitive assignment and standalone authentication-scheme redaction. Preserve only the sensitive field/scheme label plus `<redacted>`.

- [ ] **Step 3: Preserve an explicitly empty profile registry**

Use:

```python
selected_profiles = profiles if profiles is not None else self._default_profiles()
```

- [ ] **Step 4: Verify both behaviors**

Re-run Step 1. Expected GREEN output contains no credential and the profile list is empty.

### Task 2: Terminal-state-safe Planner fallback

**Files:**
- Modify: `app/services/run_control.py`

- [ ] **Step 1: Reproduce terminal Run revival and fallback priority**

Construct a cancelled `AttackState`, call `handle_planner_unavailable()`, and verify the current implementation changes it to `running`. Construct a budget-exhausted pause-mode state and verify it currently pauses instead of stopping for budget exhaustion.

- [ ] **Step 2: Make terminal states idempotent**

At the start of `handle_planner_unavailable()`, return the unchanged terminal state with a terminal fallback decision using the existing `stop_reason`.

- [ ] **Step 3: Evaluate exhausted budgets before fallback mode**

Move the shared budget-exhaustion decision before `fail_closed`, `pause`, and deterministic candidate routing.

- [ ] **Step 4: Verify cancelled and budget-exhausted states remain stopped**

Expected GREEN behavior:

```text
cancelled -> cancelled / cancelled
paused mode with exhausted budget -> stopped / budget_exhausted
```

### Task 3: Evaluator Evidence and transport integrity

**Files:**
- Modify: `app/schemas/judge_schema.py`
- Modify: `app/services/judge_engine.py`

- [ ] **Step 1: Reproduce cross-Run Evidence injection and 5xx misclassification**

Pass a domain evaluator result with `cross-run-evidence` while the allowed set contains only `current-evidence`; verify the current aggregate accepts both. Judge a response with status 500 and verify the current issue is `connection_error`.

- [ ] **Step 2: Validate every external and model result against governed Evidence**

Extend external-result validation to require:

```python
set(result.evidence_refs).issubset(allowed_evidence_refs)
```

Apply the same Core check to the optional model result before aggregation.

- [ ] **Step 3: Add a server-error issue**

Add `EvaluationIssue.server_error` and use it for received HTTP 5xx responses. Keep connection and timeout issues reserved for request failures.

- [ ] **Step 4: Verify rejection and classification**

Expected GREEN behavior: cross-Run references raise `ValueError`; HTTP 500 produces `server_error`.

### Task 4: Persist the layered single-run Evidence ID

**Files:**
- Modify: `app/services/attack_executor.py`
- Modify: `app/services/run_service.py`
- Modify: `app/repositories/run_repository.py`
- Modify: `app/api/tests.py`

- [ ] **Step 1: Confirm the saved endpoint bypasses the layered executor**

Trace `/tests/dry-run-and-save` to `DeterministicRunService.run_single()` and verify its stored result has no `evidence_id` or `judge_result`.

- [ ] **Step 2: Make `AttackExecutor` injectable without changing default behavior**

Allow connector and Judge Engine injection, while keeping `run_once()` responsible for allocating the stable Evidence UUID before the target call.

- [ ] **Step 3: Add an event-backed layered single-run service path**

Add `DeterministicRunService.run_layered_single()` that:

```text
validates target
creates the one-case Run snapshot
executes through AttackExecutor
redacts persisted request/response data
persists the target event with AttackRunResult.evidence_id
finalizes the Run and returns report rows
```

- [ ] **Step 4: Persist the exact preallocated Evidence ID**

Add `RunRepository.record_layered_result()` that uses `AttackRunResult.evidence_id` as the `target_called` `EventRecord.id`, stores the full layered Judge result in an `evaluation_completed` event, and creates a Finding only for a violation verdict.

- [ ] **Step 5: Route the saved API through the layered path**

Change `/tests/dry-run-and-save` to call `run_layered_single()`. Keep `/tests/dry-run` non-persistent.

- [ ] **Step 6: Verify with disposable SQLite**

Run a one-case service smoke check with a fake connector and temporary SQLite database. Verify:

```text
step.result_json.evidence_id == target_called event.id
JudgeResult.evidence_refs contains that event.id
every Finding evidence_event_id exists in events
```

### Task 5: Full verification and review

**Files:**
- Verify only; do not modify tests.

- [ ] **Step 1: Run existing tests**

```powershell
pytest -q -p no:cacheprovider --basetemp=<writable-temp-dir>
```

Expected: 22 passed.

- [ ] **Step 2: Run static checks**

```powershell
ruff check --no-cache .
ruff format --check --no-cache .
pyright --pythonpath <project-python>
python -m compileall -q app conf alembic
```

Expected: all pass with no errors.

- [ ] **Step 3: Inspect the diff**

```powershell
git diff --check
git diff --stat origin/master...HEAD
git status --short
```

Expected: only the planned production files and this plan are changed.

- [ ] **Step 4: Request an independent code review**

Review the final diff for credential leakage, terminal-state regressions, cross-Run Evidence references, idempotent persistence, and compatibility with existing report generation.
