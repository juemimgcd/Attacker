# Agent Core Sections 9-12 Implementation Plan

> **For agentic workers:** Implement each checked step in order. The repository policy for this task explicitly forbids adding or modifying tests; verification therefore uses existing tooling and disposable command-line smoke checks.

**Goal:** Implement the stopping, deterministic fallback, evaluator, and prompt-governance requirements from sections 9-12 of `target/attacker_agent_optimization_requirements.md`.

**Architecture:** Keep deterministic control decisions in Core services and typed Pydantic contracts. Integrate the evaluator pipeline into the existing single-run executor, while exposing fallback and prompt snapshots as reusable boundaries for the later LangGraph Adaptive orchestrator. Do not invent the missing Candidate Builder, Policy Gate, Approval Service, SQLAlchemy repositories, or model Adapter. Do not extend the legacy DuckDB/Parquet online path; v1 business persistence remains reserved for the documented SQLAlchemy Async/SQLite boundary.

**Tech Stack:** Python 3.12-compatible syntax, Pydantic 2, HTTPX, existing FastAPI service layout.

---

### Task 1: Run stopping and fallback contracts

**Files:**
- Modify: `app/schemas/attack_state_schema.py`
- Create: `app/schemas/run_control_schema.py`
- Create: `app/services/run_control.py`

- [x] Add typed run status, stop reason, fallback mode, transport-failure counter, fallback snapshot, and checkpoint reference fields without weakening the existing budget validation.
- [x] Add stop thresholds and explicit inputs for authorization, policy termination, cancellation, pending approval, candidate exhaustion, and required coverage.
- [x] Evaluate hard stops before approval waits and soft stops, map every terminal path to one allowed `stop_reason`, and return an updated immutable state snapshot.
- [x] Resolve Planner unavailability strictly from the Run fallback mode: fail closed, pause with checkpoint, or select the first still-eligible action from the current Candidate Snapshot and route it to `policy_gate`.
- [x] Emit a structured fallback event containing mode, reason, actual model ID, and actual Provider ID.

### Task 2: Layered evaluator and finding aggregation

**Files:**
- Modify: `app/schemas/judge_schema.py`
- Modify: `app/services/judge_engine.py`
- Modify: `app/services/target_connector/http_connector.py`
- Modify: `app/services/attack_executor.py`
- Modify: `app/services/evidence_service.py`

- [x] Model timeout, connection error, refusal, violation, insufficient evidence, and evaluator conflict separately.
- [x] Give each component result evaluator/version, rule or prompt version, evidence references, confidence, stage, and structured issue codes.
- [x] Run transport validation before deterministic string rules; accept optional structured/domain/model results; invoke an optional model judge only for unresolved semantics.
- [x] Aggregate conflicting decisive results as `inconclusive` instead of selecting the highest risk.
- [x] Allocate a stable evidence ID before execution so evaluator references and persisted Evidence use the same identifier.
- [x] Keep current prototype persistence compatible without adding new DuckDB schema; the planned SQLite Event repository will persist the richer result as a later prerequisite task.

### Task 3: Prompt and context governance

**Files:**
- Create: `app/prompts/planner_v1.txt`
- Create: `app/prompts/model_judge_v1.txt`
- Create: `app/schemas/prompt_schema.py`
- Create: `app/services/prompt_governance.py`
- Modify: `conf/settings.py`

- [x] Register versioned Core-owned templates and verify their SHA-256 checksum before use.
- [x] Accept only structured observation summaries and fact references, mark all observation text as untrusted data, and prevent callers or Skills from supplying a replacement system prompt.
- [x] Redact common secret, authorization, email, and phone values; limit item count, item length, and total UTF-8 byte budget as a conservative token ceiling.
- [x] Validate approved Prompt Profiles by caller permission, task compatibility, schema compatibility, template version, and checksum.
- [x] Return a serializable snapshot containing normalized inputs, fact references, template version/checksum, model/provider identity, and role-specific parameters so the exact messages can be rebuilt.
- [x] Add independent Planner and Model Judge model settings, budgets, and call counters.

### Task 4: Verification

**Files:**
- Verify only; no test files are added or modified.

- [x] Run Ruff on every changed Python file and fix only errors introduced in those files.
- [x] Compile all application and configuration modules.
- [x] Run disposable Python smoke checks covering every stop reason, approval wait behavior, all three fallback modes, evaluator conflict/timeout/no-evidence behavior, prompt redaction/limits/profile validation, and exact prompt rebuild.
- [x] Re-read requirements sections 9-12 and record any capability intentionally deferred because its prerequisite subsystem does not yet exist.
