# Harness Provider Skill Review Hardening Implementation Plan

> **For agentic workers:** Execute tasks inline in dependency order. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve all security, recovery, authorization, snapshot, and replay defects found in the completion-branch review without adding or modifying tests.

**Architecture:** Trust is assigned by Core-owned provenance, not package assertions. Provider execution persists a bounded redacted result and all Resource Leases atomically, while stale executions remain explicitly recoverable. Run bindings are frozen as one immutable set, and Replay compares persisted source and replay facts instead of request hints.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy async, SQLite/Alembic, asyncio subprocess protocol, YAML/JSON Schema.

**Constraint:** Do not add or modify test code. Reproduce and verify behavior with bounded temporary smoke scripts plus the existing quality and test commands.

---

### Task 1: Enforce Core-owned package trust and revocation

**Files:**
- Modify: `app/equipment/catalog.py`
- Modify: `app/equipment/development.py`
- Modify: `app/equipment/runner.py`

- [x] Derive built-in provenance from configured Core package roots and reject `trusted_builtin` on offline/local executable packages.
- [x] Pin every Core Provider, Skill, Case Pack, and Contract with a cross-platform canonical checksum.
- [x] Apply checksum revocation before the unsigned-package early return.
- [x] Inspect executable contract methods without importing the module in the CLI process; keep executable scenarios in the configured Runner.
- [x] Verify unsigned checksum revocation, external trust escalation rejection, and side-effect-free contract introspection with temporary packages.

### Task 2: Make Provider completion, result persistence, and leases recovery-safe

**Files:**
- Modify: `app/models.py`
- Modify: `app/repositories/equipment_repository.py`
- Modify: `app/services/harness_service.py`
- Create: `alembic/versions/20260730_0006_harness_review_hardening.py`

- [x] Persist a bounded redacted structured execution result and an immutable request identity fingerprint.
- [x] Reject operation-ID reuse with a different Run/package/instance/principal/input identity.
- [x] Persist Provider completion and validated redacted Resource Leases in one transaction.
- [x] Preserve observed physical attempts when a Provider result violates budget or retry policy.
- [x] Return the persisted structured result for both first and repeated completed executions; report stale `running` executions as in-doubt.
- [x] Return persisted Lease IDs on repeated Provider executions and mark pre-fingerprint legacy rows explicitly in-doubt.
- [x] Verify duplicate operations, changed fingerprints, crash-safe lease persistence, secret rejection, and physical-attempt accounting.

### Task 3: Close Capability and cleanup authorization gaps

**Files:**
- Modify: `app/services/harness_service.py`

- [x] Require every Capability request to be allowed by the Manifest, Skill context, and Run policy.
- [x] Resolve cleanup Secrets using the frozen Provider Instance revision and pass them only for the cleanup call.
- [x] Redact cleanup failures before persistence.
- [x] Recover cleanup by `(run_id, test_principal_ref)` with explicit Lease IDs so principals are never mixed.
- [x] Verify restricted Skill contexts, credentialed cleanup, and multi-principal recovery.
- [x] Redact the complete persisted Skill result, output summary, and Evidence.

### Task 4: Freeze complete immutable Run bindings atomically

**Files:**
- Modify: `app/repositories/equipment_repository.py`
- Modify: `app/services/equipment_service.py`
- Modify: `app/models.py`
- Modify: `alembic/versions/20260730_0006_harness_review_hardening.py`

- [x] Build and validate the full Skill, Case Pack, Provider, Instance, Contract, principal, target, config, and Secret-revision set before persistence.
- [x] Save the complete set in one transaction.
- [x] Compare every immutable snapshot field on repeated freeze and reject any mismatch.
- [x] Enforce uniqueness for package snapshots whose Provider Instance is absent.
- [x] Verify no partial rows survive a failed freeze and changed target/principal/Contract facts produce conflicts.

### Task 5: Make Replay binding and change reports factual

**Files:**
- Modify: `app/services/replay_service.py`
- Modify: `app/services/equipment_service.py`
- Modify: `app/services/run_service.py`
- Modify: `app/services/adaptive_run_service.py`
- Modify: `app/services/stateful_run_service.py`

- [x] Preserve source target binding during same-binding snapshot cloning and reject a different supplied target.
- [x] Compare canonical non-secret Target behavior and normalize Run-local Policy target IDs.
- [x] Load source and replay Run snapshots after execution and compute Provider, Instance, config, Skill, Case Pack, Contract, target, policy, and principal changes from persisted facts.
- [x] Keep Upgrade Comparison explicit and make unchanged dimensions deterministically false.
- [x] Verify same-binding target rejection and each supported change dimension with temporary database Runs.

### Task 6: Regression and delivery gate

**Files:**
- Modify only implementation files required by verification failures.

- [x] Run focused temporary smoke scripts for all review findings.
- [x] Run `uv run ruff format --check .`.
- [x] Run `uv run ruff check .`.
- [x] Run `uv run pyright`.
- [x] Run `uv run pytest`.
- [x] Run an empty-database Alembic upgrade through `20260730_0006`.
- [x] Run `git diff --check`, inspect the final diff, commit, and push `fix/harness-provider-skill-review`.
