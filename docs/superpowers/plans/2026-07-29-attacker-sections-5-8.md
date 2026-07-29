# Attacker Sections 5–8 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the persisted adaptive feedback loop, deterministic candidate construction,
strict planner decisions, hypothesis lifecycle, finish validation, and coverage-oriented strategy
selection required by sections 5–8.

**Architecture:** Keep authorization, candidate construction, fact updates, and finish decisions in
deterministic Core services. Persist candidate snapshots, observations, planner decisions,
hypothesis transitions, coverage transitions, and realized information gain as immutable Event
facts; LangGraph state carries only identifiers and compact counters. The model adapter receives
only bounded candidate summaries and explicitly untrusted observation summaries.

**Tech Stack:** Python 3.12, Pydantic v2, SQLAlchemy async, SQLite, LangGraph, Ruff, Pyright, Pytest.

**Repository test policy:** Do not add or modify test files. Use fail-first one-off contract probes,
then run the existing CI suite unchanged.

---

### Task 1: Adaptive Core contracts

**Files:**
- Create: `app/schemas/adaptive_agent_schema.py`
- Modify: `app/schemas/graybox_schema.py`
- Modify: `app/workflows/attack_state.py`

- [ ] **Step 1: Run a fail-first import probe**

```powershell
uv run python -c "from app.schemas.adaptive_agent_schema import CandidateSnapshot"
```

Expected: fail with `ModuleNotFoundError`.

- [ ] **Step 2: Define strict immutable facts and decisions**

Create Pydantic models for:

```python
class CandidateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot_id: str
    run_id: str
    candidates: tuple[Candidate, ...]
    rejected: tuple[CandidateRejection, ...]


class PlannerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["execute", "finish"]
    candidate_snapshot_id: str
    candidate_id: str | None
    reason_code: PlannerReasonCode
    evidence_refs: list[str]
    hypothesis_refs: list[str]
    expected_information_gain: InformationGain | None
```

Also define bounded untrusted observations, hypothesis states/transitions, coverage transitions,
finish-gate results, realized-gain metrics, and immutable derived cases.

- [ ] **Step 3: Extend case and policy inputs without breaking existing datasets**

Add optional/defaulted case metadata for hypothesis templates, coverage tags, prerequisites,
capability binding, repeat policy, and expected information gain. Add policy capability allowlist,
per-action repeat ceiling, and early-finish control.

- [ ] **Step 4: Extend graph state with references only**

Add candidate snapshot/action IDs, denied action IDs, coverage values, hypothesis/observation/finding
references, repeat counts, evidence gaps, and persisted-gain references. Do not store prompts,
responses, Provider clients, or database sessions.

- [ ] **Step 5: Verify contracts**

```powershell
uv run python -c "from app.schemas.adaptive_agent_schema import CandidateSnapshot, PlannerDecision"
uv run ruff check app/schemas app/workflows/attack_state.py
```

Expected: imports succeed and Ruff reports no errors.

### Task 2: Deterministic Core services

**Files:**
- Create: `app/services/candidate_builder.py`
- Create: `app/services/hypothesis_service.py`
- Create: `app/services/observation_normalizer.py`
- Create: `app/services/finish_gate_service.py`
- Create: `app/services/derived_case_service.py`

- [ ] **Step 1: Run fail-first service import probes**

```powershell
uv run python -c "from app.services.candidate_builder import CandidateBuilder"
```

Expected: fail with `ModuleNotFoundError`.

- [ ] **Step 2: Implement deterministic candidate construction**

`CandidateBuilder.build(...)` must:

```text
filter enabled/compatible -> validate target/case/capability/bindings -> enforce budget and repeat
limits -> require prerequisites -> retain uncovered/evidence-gap/control value -> sort by explicit
priority tuple -> stable candidate ID tie-break -> stable snapshot checksum
```

Every rejection must have a structured reason. The service must not call an LLM.

- [ ] **Step 3: Implement deterministic fact transitions**

`HypothesisService` maps persisted evaluation facts to `supported`, `rejected`, or `inconclusive`.
Coverage becomes `covered` only from complete persisted evaluation evidence. Realized information
gain is calculated from post-execution deltas, never from planner predictions.

- [ ] **Step 4: Implement bounded untrusted observations and finish gate**

The normalizer truncates summaries and always marks Target, tool, RAG, and Skill content as
untrusted. The finish gate accepts finish only when required coverage and controls are complete,
evidence gaps and pending approvals are empty, or policy explicitly permits early finish.

- [ ] **Step 5: Implement immutable derived-case freezing**

Freeze generator ID/version, parent case, input fact references, output, and canonical SHA-256.
Deterministic verification requires persisted evidence references before the case can contribute
effective coverage.

- [ ] **Step 6: Verify deterministic behavior with one-off probes**

Run the builder twice with identical inputs and assert equal candidate order and snapshot IDs.
Construct invalid bindings, exhausted budgets, completed actions, and missing prerequisites and
assert their structured rejection codes. No repository test files are created.

### Task 3: Persist adaptive facts through the existing Event store

**Files:**
- Modify: `app/repositories/adaptive_repository.py`

- [ ] **Step 1: Add event-backed persistence methods**

Persist and reload:

```text
hypothesis_created / hypothesis_updated
candidate_snapshot_created
observation_normalized
coverage_updated
information_gain_measured
derived_case_frozen / derived_case_verified
planner_finish_rejected
```

Use stable operation IDs for idempotency. Validate every referenced Event/Finding belongs to the
same Run before accepting a planner decision or fact update.

- [ ] **Step 2: Expand planner audit snapshots**

`record_planner_result` stores the candidate snapshot ID, prompt template version/checksum, model
ID, model parameters, schema version, and persisted input fact references. Natural-language reason
codes remain audit metadata and never authorization evidence.

- [ ] **Step 3: Verify repository round trips**

Use a temporary SQLite database in a one-off Python process to persist and reload one candidate
snapshot and one hypothesis transition. Assert cross-run references are rejected.

### Task 4: Integrate the fixed feedback loop

**Files:**
- Modify: `app/infrastructure/model_adapter.py`
- Modify: `app/services/adaptive_run_service.py`
- Modify: `app/workflows/attack_graph.py`

- [ ] **Step 1: Make planner input candidate-bound**

The deterministic planner chooses the first ranked candidate ID. The model planner receives a
static trusted system instruction plus a user JSON payload containing candidate summaries and
explicitly untrusted bounded observations. Strict JSON validation rejects unknown fields.

- [ ] **Step 2: Add graph nodes and routing**

Implement:

```text
initialize hypotheses
  -> build_candidates
  -> plan_next_case
  -> finish_gate or policy_gate
  -> approval/execute
  -> normalize_observation
  -> evaluate
  -> persist
  -> update_facts
  -> decide_next
  -> build_candidates or finalize
```

Planner rejection records an Event and never bypasses candidate, policy, approval, budget, or finish
validation.

- [ ] **Step 3: Rehydrate entirely from persisted facts**

On recovery, load the current candidate snapshot, observation references, hypothesis states,
coverage states, and completed/denied actions from Events/Steps before continuing. Do not trust
transient model context as an audit fact.

- [ ] **Step 4: Verify existing approval recovery**

Run the unchanged recovery test and confirm the Target is called once, Policy is revalidated, and
secrets do not enter reports or checkpoints.

### Task 5: Full verification and delivery

**Files:**
- Modify only files listed above plus this plan.

- [ ] **Step 1: Run exact CI parity commands**

```powershell
uv sync --locked --python 3.12
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
uv run python -m compileall -q app conf alembic
```

Expected: every command exits zero and all existing tests pass.

- [ ] **Step 2: Review scope**

```powershell
git diff --check
git status --short
git diff --stat
```

Confirm no test file was added or modified and every changed production line maps to sections 5–8.
