# Harness Provider Skill Completion Plan

> **For agentic workers:** Execute tasks in dependency order. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Close the requirements that remained non-functional or only structurally represented after the first Harness/Provider/Skill delivery.

**Architecture:** A Run freezes complete package, instance, Contract, principal, target, config, and secret-binding facts before equipment execution. Skills issue bounded declarative Capability requests through a Core broker; Core alone resolves Provider bindings, applies Policy, persists executions/Evidence/leases, and returns redacted results. Existing three-stage evaluators remain the single source of evaluation semantics and are exposed through built-in Skill adapters instead of being reimplemented approximately.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy async, SQLite/Alembic, asyncio subprocess protocol, YAML/JSON Schema.

**Constraint:** Do not add or modify test code. Verify with the existing suites and direct bounded smoke scripts.

---

### Task 1: Correct catalog and supply-chain facts

**Files:**
- Modify: `app/models.py`
- Modify: `app/schemas/equipment_schema.py`
- Modify: `app/equipment/catalog.py`
- Modify: `app/repositories/equipment_repository.py`
- Create: `alembic/versions/20260730_0005_equipment_completion.py`
- Modify: `conf/settings.py`

- [x] Persist package name, source type/ref, signature status, publisher, install time, and immutable validation facts.
- [x] Verify signed packages against Ed25519 trust roots and reject revoked publisher IDs, package checksums, or signature IDs.
- [x] Preserve the previously valid immutable package record when changed content reuses an ID/version; register the conflicting discovery as a control-plane failure without disabling historical active-Run content.
- [x] Verify conflict, revocation, and archive materialization with direct temporary-directory smoke checks.

### Task 2: Freeze and consume real Run equipment bindings

**Files:**
- Modify: `app/services/equipment_service.py`
- Modify: `app/repositories/equipment_repository.py`
- Modify: `app/services/run_service.py`
- Modify: `app/services/adaptive_run_service.py`
- Modify: `app/services/stateful_run_service.py`
- Modify: `app/core/lifespan.py`

- [x] Add idempotent `freeze_run_bindings()` that persists Skill, Case Pack, Provider Instance, Contract checksum, principal, target, config revision, and secret-binding revision rows.
- [x] Inject EquipmentService optionally into all deterministic/adaptive/stateful Run services and freeze stage-appropriate built-ins immediately after Run creation.
- [x] Resolve Provider Instance revisions by the frozen revision rather than the latest mutable instance view.
- [x] Make Harness authorization consume Run snapshots when `run_id` is real, so reload/disable affects new Runs only.
- [x] Verify every stage creates reconstructable equipment snapshots and archived content remains executable after a simulated reload conflict.

### Task 3: Add the Core Capability Broker and recovery-safe cleanup

**Files:**
- Modify: `app/schemas/equipment_schema.py`
- Modify: `app/services/harness_service.py`
- Modify: `app/repositories/equipment_repository.py`
- Modify: `app/core/lifespan.py`

- [x] Add bounded declarative Skill Capability requests/results keyed by request ID and manifest binding.
- [x] Execute each request only through `invoke_provider()`, with frozen binding, Policy Gate, schema, Evidence, physical-attempt budget, and redaction checks.
- [x] Re-enter a Skill with redacted Capability results until it completes or reaches its declared step/provider-call limits.
- [x] Reject undeclared bindings, changed Contract checksums, duplicate request IDs with different payloads, and ambiguous instances.
- [x] Recover active/failed ResourceLeases at startup using stable cleanup operation IDs; persist retry count, failure, and completion.
- [x] Verify a sample Skill can consume a Provider result without receiving a Provider object, DB session, or raw secret.

### Task 4: Make built-in evaluator Skills semantically equivalent

**Files:**
- Modify: `equipment/skills/prompt-injection-evaluator/handler.py`
- Modify: `equipment/skills/prompt-injection-evaluator/{input,output}.schema.json`
- Modify: `equipment/skills/tool-policy-trace-evaluator/handler.py`
- Modify: `equipment/skills/tool-policy-trace-evaluator/{input,output}.schema.json`
- Modify: `equipment/skills/state-poisoning-evaluator/handler.py`
- Modify: `equipment/skills/state-poisoning-evaluator/{input,output}.schema.json`

- [x] Delegate black-box evaluation to `EvaluatorService` using full persisted Case and TargetResponse inputs.
- [x] Delegate gray-box evaluation to `GrayBoxEvaluatorService` using full Case, response, and normalized Trace inputs.
- [x] Delegate stateful evaluation to `StatefulEvaluatorService` using full Case, memory, retrieval, and recovery inputs.
- [x] Return each existing result model unchanged inside structured Skill output and add only non-sensitive evaluator Evidence.
- [x] Run all frozen sample cases through both the direct evaluator and Skill adapter and assert serialized result equality in a smoke script.

### Task 5: Complete observability and Contract tooling

**Files:**
- Create: `app/equipment/metrics.py`
- Modify: `app/services/equipment_service.py`
- Modify: `app/services/harness_service.py`
- Modify: `app/api/equipment.py`
- Modify: `app/equipment/development.py`
- Modify: `.github/workflows/equipment-contract.yml`
- Modify: `docs/equipment-development.md`

- [x] Track bounded non-sensitive discovery, validation, load, Skill, Provider, denial, timeout, physical-attempt, cleanup, checksum, and sandbox counters/durations.
- [x] Expose an API snapshot of equipment metrics without input/output/Secret labels.
- [x] Upgrade Contract tooling from method introspection to validate declared request/response examples and executable adapter results where an offline scenario is supplied.
- [x] Document revocation, Run snapshot behavior, Capability Broker rounds, cleanup recovery, and Linux sandbox prerequisites.
- [x] Verify OpenAPI, CLI Contract checks, and metrics smoke output.

### Task 6: Requirement and regression gate

**Files:**
- Modify only implementation files required by verification failures.

- [x] Run `uv run ruff format --check .`.
- [x] Run `uv run ruff check .`.
- [x] Run `uv run pyright`.
- [x] Run `uv run pytest`.
- [x] Run migrations from an empty SQLite database through `20260730_0005`.
- [x] Run catalog/reload conflict, all-stage snapshot, Capability Broker, evaluator-equivalence, cleanup recovery, signature revocation, API/CLI, wheel-content, and Windows untrusted-rejection smoke checks.
- [x] Re-read all requirements in `target/harness_provider_skill_requirements.md`, record platform-dependent verification limits, inspect `git diff --check`, commit on the completion branch, and leave the worktree clean.

## Verification record

- Windows local: Ruff format/check, Pyright, 22 existing pytest cases, empty-database Alembic upgrade, CLI Contract checks, OpenAPI/metrics, immutable conflict, Ed25519 revocation, archive materialization, three-stage snapshots, active-Run frozen revision, Capability Broker, scoped cleanup recovery, 30/30 evaluator equivalence, wheel contents, and untrusted-package rejection.
- Linux: the repository CI matrix runs equipment validation and Contract checks on `ubuntu-latest`; a production strong-sandbox image, seccomp/AppArmor profile, and deployment network policy are environment-owned and cannot be exercised on this Windows host.
