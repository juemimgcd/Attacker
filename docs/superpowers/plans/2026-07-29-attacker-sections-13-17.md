# Attacker Sections 13–17 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the bounded model-provider boundary, event-derived adaptive observability,
deterministic/adaptive calibration reporting, and auditable completion criteria required by
sections 13–17.

**Architecture:** Keep Planner and optional Model Judge request construction, budget enforcement,
schema validation, state transitions, and fact aggregation inside Attacker Core. A narrow
`model.inference.v1` adapter handles only protocol/authentication, bounded retry, health, and
normalized physical-attempt usage. Derive metrics and comparisons from persisted Run, Step,
Event, and Finding facts so reports remain rebuildable from SQLite and never expose prompts,
raw target responses, or secrets as metric labels.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, LangGraph, httpx, SQLAlchemy Async, SQLite,
Alembic, Ruff, Pyright, pytest. No additional runtime or storage technology is introduced.

**Repository test policy:** Do not add or modify test files. Use fail-first one-off contract probes,
then run the existing CI suite unchanged.

---

### Task 1: Define the narrow `model.inference.v1` contract

**Files:**
- Create: `app/schemas/model_provider_schema.py`
- Create: `app/infrastructure/model_provider.py`
- Modify: `app/schemas/prompt_schema.py`
- Modify: `app/services/prompt_governance.py`

- [ ] **Step 1: Run a fail-first import probe**

```powershell
uv run python -c "from app.schemas.model_provider_schema import ModelInferenceRequest"
```

Expected: fail with `ModuleNotFoundError`.

- [ ] **Step 2: Define strict provider facts**

Create Pydantic models with `extra="forbid"` for:

```python
class ProviderAttempt(BaseModel):
    attempt: int
    status: Literal["success", "error", "timeout"]
    latency_ms: int
    error_category: str | None


class ModelProviderUsage(BaseModel):
    physical_attempts: int
    input_tokens: int
    output_tokens: int
    latency_ms: int
    estimated_cost: Decimal
    attempts: tuple[ProviderAttempt, ...]


class ModelInferenceRequest(BaseModel):
    contract: Literal["model.inference.v1"]
    operation_id: str
    task: PromptTask
    provider_id: str
    model_id: str
    messages: tuple[PromptMessage, ...]
    response_schema_version: str
    temperature: float
    timeout_seconds: float
    max_physical_attempts: int


class ModelInferenceResult(BaseModel):
    structured_output: dict[str, Any]
    usage: ModelProviderUsage
```

Validate that physical attempt counts equal the attempt list, exactly one successful final attempt
exists for successful results, and no secret-bearing headers or arbitrary routing fields are part of
the Core/Provider contract.

- [ ] **Step 3: Implement the provider adapter**

Add:

```python
class ModelProvider(Protocol):
    async def infer(self, request: ModelInferenceRequest) -> ModelInferenceResult: ...
    async def healthcheck(self, timeout_seconds: float) -> bool: ...
```

`OpenAICompatibleModelProvider` injects its own credential, calls only its configured endpoint with
`follow_redirects=False`, retries no more than the Core-supplied physical-attempt limit, records
every timeout/request/protocol error category and latency, and returns normalized token usage.
`ModelProviderError` carries only structured attempts and a sanitized error category.

- [ ] **Step 4: Extend governed prompts with Core-owned structured payload**

Add `trusted_payload` to `PromptBuildRequest` and `PromptSnapshot`. The Core constructs this field;
untrusted Target/tool/RAG/Skill text stays in `observations`. Render both as separate JSON keys:

```json
{
  "trusted_core_payload": {},
  "trusted_fact_refs": [],
  "untrusted_observations": []
}
```

The existing count, byte-limit, redaction, checksum, profile authorization, and deterministic
rebuild checks remain mandatory.

- [ ] **Step 5: Verify the contract**

Use a one-off fake `httpx.MockTransport` provider probe to assert one timeout followed by one
success yields two physical attempts and one normalized result. Assert an exhausted attempt budget
raises `ModelProviderError` without returning response bodies or credentials.

### Task 2: Route Planner and optional Model Judge through the provider boundary

**Files:**
- Modify: `app/infrastructure/model_adapter.py`
- Modify: `app/schemas/adaptive_agent_schema.py`
- Modify: `app/schemas/graybox_schema.py`
- Modify: `app/repositories/adaptive_repository.py`
- Modify: `app/services/adaptive_run_service.py`
- Modify: `app/workflows/attack_state.py`
- Modify: `app/workflows/attack_graph.py`

- [ ] **Step 1: Extend bounded configuration and usage**

Add `provider_id`, `max_physical_attempts`, and `max_provider_calls` with conservative defaults.
Extend Planner usage with physical attempts, latency, estimated cost, and per-attempt error
categories. Extend `PlannerCallSnapshot` with provider ID, prompt profile ID, and governed-input
checksum.

- [ ] **Step 2: Make Planner Core-owned and provider-backed**

`OpenAICompatiblePlannerAdapter` must:

1. build `core.planner.v1` messages through `PromptGovernanceService`;
2. put only immutable candidate data and remaining budget in `trusted_core_payload`;
3. put bounded `UntrustedObservation` summaries in the untrusted area;
4. call `model.inference.v1` with the remaining physical-attempt allowance;
5. validate the structured output as `PlannerDecision`;
6. return the governed prompt snapshot and normalized usage.

The deterministic adapter remains model-free and reports zero physical attempts.

- [ ] **Step 3: Provide the optional Model Judge adapter**

Add a provider-backed Model Judge adapter that uses `core.model_judge.v1`, validates the returned
`EvaluatorResult`, enforces `stage="model_judge"`, and returns facts to `JudgeEngine`; it cannot
create a Finding or mutate authorization. Do not make Model Judge the default path.

- [ ] **Step 4: Enforce physical-call budget in the graph**

Add `provider_call_count` to graph state. Before each model call, calculate:

```text
remaining = policy.max_provider_calls - provider_call_count
allowed_attempts = min(planner.max_physical_attempts, remaining)
```

Zero remaining attempts terminates before any Provider request. Success and failure both add every
physical attempt to the state and persisted planner event. Invalid Planner output may retry only
within the configured logical failure limit and never reaches Policy Gate or Target execution.

- [ ] **Step 5: Persist provider audit facts**

Planner decision/error Events store the role, provider/model identity, physical attempts,
per-attempt error categories, tokens, latency, cost, prompt profile/version/checksum, governed input
checksum, and fact references. Do not store credentials, raw response bodies, or full Prompt
content.

### Task 3: Record complete-loop and observability facts

**Files:**
- Create: `app/services/adaptive_observability.py`
- Modify: `app/repositories/adaptive_repository.py`
- Modify: `app/workflows/attack_state.py`
- Modify: `app/workflows/attack_graph.py`

- [ ] **Step 1: Enrich immutable events**

Record:

```text
candidate_snapshot_created
candidate_snapshot_expired
planner_decided / planner_rejected / planner_fallback / planner_error
policy_*
target_called
observation_normalized
evaluation_completed
case_persisted
information_gain_measured
run_control_evaluated
run_*
```

`run_started` additionally stores Test Principal references, evaluator snapshot, candidate-universe
checksum, and provider/capability equipment snapshot. Candidate expiry and persist events use stable
operation IDs.

- [ ] **Step 2: Persist predicted versus actual gain**

Carry the selected candidate's `expected_information_gain` only as prediction metadata. The
`information_gain_measured` Event stores the prediction beside Core-computed coverage, evidence,
finding, target-call, provider-call, cost, and duration deltas. Only actual deltas affect no-gain
control.

- [ ] **Step 3: Record loop-control observations**

Persist normalized state fingerprints, repeated action/state counters, consecutive no-gain count,
planner failures, target transport failures, canonical stop reason, and checkpoint/runtime resume
count. Continue using deterministic Core rules; Planner text cannot modify these counters.

- [ ] **Step 4: Build safe event-derived metrics**

`AdaptiveObservabilityService` returns scalar counts and bounded ID/category lists for:

```text
planner decisions/rejections/fallbacks/errors and physical usage
candidate totals/filter reasons/snapshot lifecycle
principal/tenant/session references present in persisted scope facts
risk and coverage status
per-step actual gain and prediction mismatch
derived-case frozen/verified/effective counts
repeated action/state and no-gain counts
evaluator agreement/conflict/inconclusive
stop reason and resume count
shortest evidence path per Finding
```

Metric output must not include prompt text, raw Target response data, headers, or secrets.

### Task 4: Add evidence-backed adaptive calibration and completion reporting

**Files:**
- Modify: `app/services/report_service.py`
- Modify: `README.md`

- [ ] **Step 1: Add adaptive observability to JSON and Markdown reports**

For adaptive runs, include the event-derived metrics under `adaptive_observability`; render a compact
Markdown section with candidate, Provider, gain, evaluator, loop, stop, resume, and evidence-path
statistics.

- [ ] **Step 2: Validate comparison eligibility**

Before reporting efficiency gains, compare adaptive and deterministic snapshots for:

```text
dataset checksum
redacted Target identity/configuration
Test Principal references
policy semantics
evaluator snapshot
candidate universe checksum
provider/capability equipment snapshot
```

Return `comparison_eligible=false` plus structured mismatch reasons when facts differ. Never label
an incomparable run as an efficiency win.

- [ ] **Step 3: Separate efficiency and discovery**

Efficiency reports Target calls, duration, provider calls, cost, achieved coverage, and evidence
completeness for eligible comparable runs. Discovery counts only `derived_case_verified` facts with
persisted deterministic evidence; frozen but unverified DerivedCase output cannot increase effective
coverage.

- [ ] **Step 4: Make recommendation evidence-based**

Set `adaptive_recommended=true` only when comparison is eligible and persisted actual metrics show a
measurable efficiency, verified discovery, coverage, or evidence benefit without a safety/evidence
regression. Planner-declared expected gain is never counted as benefit.

- [ ] **Step 5: Document the implemented boundary**

Update README with the provider contract, physical-attempt accounting, safe observability fields,
and the distinction between comparable efficiency and deterministically verified discovery. Keep
the existing technology stack and production-readiness exclusions unchanged.

### Task 5: Verify requirements and deliver

**Files:**
- Modify only the files listed above plus this plan.
- Do not modify or add files under `tests/`.

- [ ] **Step 1: Run one-off acceptance probes**

Use temporary SQLite and fake providers to verify:

1. retries count as physical calls;
2. exhausted Provider budget causes no request;
3. invalid Planner JSON causes no Target call;
4. candidate expiry and complete-loop Events are present;
5. metrics contain no prompt/raw response/secret values;
6. incomparable baselines suppress efficiency claims;
7. only deterministically verified DerivedCase facts count as discovery.

- [ ] **Step 2: Run exact CI parity commands**

```powershell
uv sync --locked --python 3.12
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
uv run python -m compileall -q app conf alembic
```

Expected: every command exits zero and all existing tests pass.

- [ ] **Step 3: Review scope and acceptance coverage**

```powershell
git diff --check
git status --short
git diff --stat
git diff --name-only -- tests
```

Confirm no test file changed, no dependency or database technology was added, metrics exclude
sensitive payloads, and every production change maps to sections 13–17.
