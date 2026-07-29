# Attacker Remaining Requirements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining lifecycle, budget, recovery, and external-stop gaps across the
Attacker Agent optimization requirements without adding a framework, dependency, database table,
or test file.

**Architecture:** Keep all authorization, state transition, retry recovery, and termination
decisions in Attacker Core. Persist lifecycle facts in the existing Run/Event tables, keep
LangGraph checkpoints as resumable control state, and reuse persisted Planner outcomes before any
new Provider call. Continue using the existing HTTP connector, Provider boundary, SQLite
repositories, and Pydantic contracts.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, LangGraph, httpx, SQLAlchemy Async, SQLite,
Alembic, Ruff, Pyright, pytest. No additional technology is introduced.

**Repository test policy:** Do not add or modify files under `tests/`. Use fail-first one-off
contract probes and then run the existing suite unchanged.

---

### Task 1: Complete the real Run snapshot and cost budget

**Files:**
- Modify: `app/schemas/graybox_schema.py`
- Modify: `app/schemas/model_provider_schema.py`
- Modify: `app/infrastructure/model_provider.py`
- Modify: `app/workflows/attack_state.py`
- Modify: `app/workflows/attack_graph.py`
- Modify: `app/repositories/adaptive_repository.py`
- Modify: `app/services/adaptive_run_service.py`

- [ ] **Step 1: Verify missing contracts**

Run:

```powershell
uv run python -c "from app.schemas.graybox_schema import AttackPolicy; assert hasattr(AttackPolicy(), 'max_cost')"
```

Expected: assertion failure before implementation.

- [ ] **Step 2: Add the bounded cost and snapshot fields**

Add a positive optional `AttackPolicy.max_cost`, put the Core-generated `goal_id` and actual
`planner_fallback_snapshot` in `AttackGraphState`, and store `goal_id` in `run_started`. Every
`RunBudgetSnapshot` constructed by the graph must receive the configured maximum and current
Planner estimated cost.

- [ ] **Step 3: Normalize Provider-reported estimated cost**

Read an optional non-negative `usage.estimated_cost` from the configured Provider response into
`ModelProviderUsage`. The value stays structured and never derives from raw response text.

- [ ] **Step 4: Enforce the cost hard stop**

Before a new Provider call and after normalized Provider usage is persisted, stop with
`budget_exhausted` when the configured cost has been consumed. Deterministic Planner calls remain
zero-cost.

- [ ] **Step 5: Run contract probes**

Validate that negative/zero configured cost is rejected, Provider cost is normalized, and a
consumed cost budget returns `budget_exhausted` before Policy or Target execution.

### Task 2: Make Planner calls recovery-idempotent

**Files:**
- Modify: `app/repositories/adaptive_repository.py`
- Modify: `app/workflows/attack_graph.py`

- [ ] **Step 1: Verify the recovery reader is absent**

Run:

```powershell
uv run python -c "from app.repositories.adaptive_repository import AdaptiveRepository; assert hasattr(AdaptiveRepository, 'load_planner_outcome')"
```

Expected: assertion failure before implementation.

- [ ] **Step 2: Add a narrow persisted-outcome reader**

Add:

```python
async def load_planner_outcome(self, operation_id: str) -> dict[str, Any] | None:
    ...
```

It returns only the persisted Planner event ID, type, and structured evidence for
`planner_decided`, `planner_rejected`, or `planner_error`.

- [ ] **Step 3: Reuse the persisted outcome before Provider invocation**

At the start of `plan_next_case`, load the stable operation ID. Reconstruct and validate
`PlannerResult`/`PlannerUsage` from persisted JSON. A recovered error follows the configured
fallback; a recovered decision follows its already-persisted acceptance result. Neither path calls
the Provider again or increments database usage twice.

- [ ] **Step 4: Probe crash-window recovery**

Use a fake Provider plus an in-memory persisted Planner outcome. Assert the graph restores physical
usage and routing with zero new fake-Provider calls.

### Task 3: Persist Approval waiting and make Planner pause resumable

**Files:**
- Modify: `app/schemas/graybox_schema.py`
- Modify: `app/repositories/adaptive_repository.py`
- Modify: `app/services/adaptive_run_service.py`
- Modify: `app/workflows/attack_graph.py`
- Modify: `app/api/runs.py`
- Modify: `README.md`

- [ ] **Step 1: Add lifecycle persistence methods**

Add repository methods that mark a Run `waiting_approval`, mark it `paused`, and return it to
`running`. Each transition uses a stable Event operation ID and never sets a terminal
`stop_reason`.

- [ ] **Step 2: Checkpoint the waiting state before interrupt**

Split Approval preparation from the interrupt node. The preparation node creates/reuses the
Approval, persists `waiting_approval`, and completes its Graph state write before the next node
interrupts. Approval resolution returns the Run to `running` before policy revalidation.

- [ ] **Step 3: Replace terminal pause with a resumable interrupt**

Route Planner fallback `pause` to a `planner_pause` node. That node interrupts with the current
run/thread/fallback facts. Add:

```python
async def resume_paused(
    self,
    *,
    run_id: str,
    target: TargetConfig | None,
    planner: PlannerConfig | None,
) -> dict[str, Any]:
    ...
```

It rehydrates runtime-only credentials when necessary and resumes the same LangGraph thread.

- [ ] **Step 4: Add the resume API**

Add a strict `PlannerResumeRequest` containing optional Target/Planner runtime configuration and:

```text
POST /runs/{run_id}/resume
```

Only a paused adaptive Run is accepted. Terminal or approval-waiting Runs are rejected.

- [ ] **Step 5: Probe lifecycle transitions**

Using temporary SQLite and an in-memory checkpointer, verify `running -> waiting_approval ->
running` and `running -> paused -> running`, with no duplicate Approval or fallback Event.

### Task 4: Wire external hard-stop controls into Core routing

**Files:**
- Modify: `app/schemas/graybox_schema.py`
- Modify: `app/repositories/adaptive_repository.py`
- Modify: `app/services/adaptive_run_service.py`
- Modify: `app/workflows/attack_graph.py`
- Modify: `app/api/runs.py`
- Modify: `README.md`

- [ ] **Step 1: Define the bounded control protocol**

Add:

```python
class AdaptiveControlAction(str, Enum):
    cancel = "cancel"
    revoke_target_authorization = "revoke_target_authorization"
    terminate_policy = "terminate_policy"


class AdaptiveControlRequest(BaseModel):
    action: AdaptiveControlAction
    reason: str
```

No arbitrary Graph route or stop reason is accepted from the caller.

- [ ] **Step 2: Persist idempotent control requests**

Store one stable Event per Run/action. Terminal Runs reject new control requests. Paused or
approval-waiting Runs can be finalized immediately because no Target/Provider call is in flight.

- [ ] **Step 3: Enforce controls at side-effect boundaries**

Before Planner Provider invocation, Approval creation, and Target execution, load persisted control
flags and map them deterministically:

```text
cancel                       -> cancelled
revoke_target_authorization -> policy_terminated
terminate_policy             -> policy_terminated
```

Add conditional Graph routes so a stop never falls through to Target execution.

- [ ] **Step 4: Add the control API**

Add:

```text
POST /runs/{run_id}/control
```

Return the persisted action, current Run status, and reason. The endpoint does not accept Target,
Case, Provider, or Capability mutations.

- [ ] **Step 5: Probe all hard-stop actions**

Assert each persisted action selects the canonical stop reason and that a control observed before
execute produces no connector call.

### Task 5: Verify and deliver

**Files:**
- Modify: `README.md`
- Do not modify: `tests/**`, `pyproject.toml`, `uv.lock`, Alembic revisions

- [ ] **Step 1: Document the remaining lifecycle controls**

Document cost budgeting, waiting/paused state semantics, paused resume, external controls, and
Provider recovery idempotence. Keep the V1 technology and production exclusions unchanged.

- [ ] **Step 2: Run acceptance probes**

Run one-off probes for cost enforcement, Planner recovery, Approval/pause lifecycle, external
controls, secret-free events, and no duplicate Provider/Target calls.

- [ ] **Step 3: Run exact CI parity**

```powershell
uv sync --locked --python 3.12
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
uv run python -m compileall -q app conf alembic
```

- [ ] **Step 4: Review scope**

```powershell
git diff --check
git diff --name-only -- tests pyproject.toml uv.lock alembic
git status --short
```

Confirm the existing stack is unchanged, no test or migration was added, and each change maps to a
remaining requirement.
