# Harness Provider Skill Implementation Plan

> **For agentic workers:** Implement task-by-task in dependency order. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add a versioned local equipment catalog, controlled Harness runtime, Provider/Skill contracts, persistence, API/CLI management, built-in equipment, and replay-ready snapshots without changing the existing three-phase evaluation behavior.

**Architecture:** Core owns immutable capability contracts, package discovery/validation, persistence, policy decisions, execution records, evidence, leases, and redaction. Providers and Skills are loaded only through manifest snapshots and capability bindings; trusted enterprise code is isolated behind a JSON subprocess protocol, while unsupported `untrusted` execution is rejected. Existing evaluation services remain intact and the new equipment subsystem is initialized beside them.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy async, SQLite/Alembic, YAML, asyncio subprocesses, httpx.

**Constraint:** Do not add or modify tests in this change. Run the existing Ruff, Pyright, and Pytest suites plus direct catalog/API/CLI smoke checks.

---

### Task 1: Core schemas and persistent facts

**Files:**
- Create: `app/schemas/equipment_schema.py`
- Modify: `app/models.py`
- Create: `alembic/versions/20260730_0004_equipment_schema.py`

- [x] Define strict Provider, Skill, Case Pack, capability, context, result, lease, policy, instance, and replay snapshot models.
- [x] Add immutable package, provider-instance revision, run snapshot, idempotent execution, resource lease, and control-plane audit tables.
- [x] Add an Alembic migration with matching constraints and indexes.
- [x] Verify model metadata and migration import with `uv run python -c "from app.models import Base; print(sorted(Base.metadata.tables))"`.

### Task 2: Static discovery, validation, and deterministic resolution

**Files:**
- Create: `app/equipment/__init__.py`
- Create: `app/equipment/json_schema.py`
- Create: `app/equipment/catalog.py`
- Create: `app/repositories/equipment_repository.py`
- Create: `app/services/equipment_service.py`
- Modify: `conf/settings.py`

- [x] Scan only configured local roots, reject unsafe paths/symlink escapes, bound file count/size/depth, parse manifests without importing package code, and compute deterministic checksums.
- [x] Validate manifest versions, compatibility, entrypoints, referenced schemas, known capability contracts, duplicate immutable versions, and executable trust policy.
- [x] Persist valid and invalid discovery results, revisions, enablement, audit events, and deterministic catalog queries.
- [x] Resolve capability bindings by explicit instance, policy default, or exactly one compatible enabled/healthy instance; reject ambiguity.
- [x] Verify deterministic reload twice and compare serialized results.

### Task 3: Controlled Harness runtime

**Files:**
- Create: `app/equipment/runner.py`
- Create: `app/equipment/worker.py`
- Create: `app/equipment/security.py`
- Create: `app/services/harness_service.py`

- [x] Implement trusted-builtin invocation and bounded JSON stdin/stdout subprocess invocation with isolated workspaces, minimal environment, timeout/cancel, output limits, and process-tree termination.
- [x] Reject `untrusted` packages unless a Linux strong-sandbox backend is available; never represent a normal subprocess as a sandbox.
- [x] Implement path/host validation, metadata-address blocking, secret redaction, and call-scoped Secret Broker leases.
- [x] Enforce package/instance/contract snapshots, allowlists, approvals, risk, timeout, and provider-call budgets before invocation.
- [x] Persist idempotent executions before/after physical calls, structured equipment events/evidence, physical attempts, leases, and cleanup status.
- [x] Verify malformed output, timeout, duplicate operation ID, denied capability, and unavailable sandbox using direct smoke commands without adding test files.

### Task 4: Equipment API and CLI

**Files:**
- Create: `app/api/equipment.py`
- Create: `app/cli.py`
- Modify: `main.py`
- Modify: `app/core/lifespan.py`
- Modify: `pyproject.toml`
- Modify: `.env.example`

- [x] Add all requirement query, reload, validate, enable/disable, instance, healthcheck, and Skill dry-run endpoints behind the existing API-key dependency.
- [x] Initialize and reload one shared EquipmentService/HarnessService during FastAPI lifespan.
- [x] Add `attacker equipment ...`, provider healthcheck, Skill dry-run, and Case Pack validation commands that reuse the same services.
- [x] Verify OpenAPI paths and CLI help output.

### Task 5: Core contracts and built-in equipment

**Files:**
- Create: `contracts/*/contract.yaml`
- Create: `contracts/*/{request,response}.schema.json`
- Create: `equipment/providers/{http-agent-provider,isolated-state-provider}/...`
- Create: `equipment/skills/{prompt-injection-evaluator,tool-policy-trace-evaluator,state-poisoning-evaluator}/...`
- Create: `equipment/casepacks/{attacker-baseline-v1,attacker-controls-v1}/...`

- [x] Publish versioned Core contracts with request/response schemas, checksums, error/risk/idempotency/evidence/cleanup/limit semantics.
- [x] Add the two trusted built-in Provider packages and two independent default instances.
- [x] Add the three trusted built-in evaluator Skills as structured adapters without changing existing evaluator behavior.
- [x] Add two data-only Case Packs that reference existing sample datasets.
- [x] Verify discovery counts: at least 2 Providers, 3 Skills, 2 Case Packs, and all referenced contracts.

### Task 6: SDK, supply-chain tooling, replay metadata, and documentation

**Files:**
- Create: `app/equipment/sdk.py`
- Create: `equipment/schemas/{provider,skill,casepack}.schema.json`
- Create: `docs/equipment-development.md`
- Modify: `app/services/report_service.py`
- Modify: `app/services/replay_service.py`
- Modify: `docs/architecture.md`
- Modify: `README.md`

- [x] Export narrow Provider/Skill protocols and scaffold/validation helpers without exposing DB sessions, global settings, raw secrets, or concrete Provider clients.
- [x] Document offline package development, trust levels, signature/checksum meaning, validation, CI commands, and Windows/Linux sandbox differences.
- [x] Include persisted equipment snapshots and binding-change dimensions in reports/replay metadata without depending on live package memory.
- [x] Replace the old “do not copy Providers/Skills” wording with the controlled security-evaluation contract decision.
- [x] Verify documentation commands against the implemented CLI.

### Task 7: Regression and requirement verification

**Files:**
- Modify only implementation files required by failures; do not add or modify tests.

- [x] Run `uv run ruff format --check .`.
- [x] Run `uv run ruff check .`.
- [x] Run `uv run pyright`.
- [x] Run `uv run pytest`.
- [x] Run Alembic upgrade against a temporary SQLite database.
- [x] Run catalog reload, API OpenAPI, CLI, Provider healthcheck, Skill dry-run, idempotency, and redaction smoke checks.
- [x] Re-read `target/harness_provider_skill_requirements.md`, record implemented guarantees and any platform-dependent limitations, and inspect `git diff --check`.
