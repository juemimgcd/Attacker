# Enterprise Readiness Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Convert critical smoke coverage into permanent regression tests, connect production logging/alerting/secret rotation, and add reusable enterprise operations equipment modeled on the boundaries proven by `atlasclaw-providers`.

**Architecture:** Core remains single-tenant and provider-neutral. Runtime state metrics are derived from PostgreSQL/SQLite repositories at scrape time, structured JSON logs are collected through OpenTelemetry into Loki, Prometheus routes bounded alerts to Alertmanager, and Vault Agent refreshes file-backed Provider secrets that the existing call-scoped broker reads on every lease. Enterprise equipment keeps datasource reads, deterministic evaluation, and high-risk writes separate; no SmartCMP-specific field or AtlasClaw runtime dependency enters Core.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy async, pytest, Prometheus, Alertmanager, OpenTelemetry Collector, Loki, Vault Agent, Docker Compose, YAML/JSON equipment manifests.

---

## Scope and success criteria

- The existing 22 tests remain green and permanent tests cover catalog immutability, Harness idempotency/redaction, durable job recovery, mounted-file secret rotation, operational metrics, deployment wiring, and enterprise equipment behavior.
- `/metrics` refreshes bounded queue, expired-lease, and stale-worker gauges from the database without exposing job IDs, payloads, principals, or secrets.
- Prometheus loads Attacker alert rules and sends alerts to Alertmanager; Alertmanager reads its webhook URL from a rotation-safe, read-only directory mount.
- OpenTelemetry Collector tails structured JSON Attacker logs and exports native OTLP logs to an internal Loki service.
- Vault Agent has a production example that renews authentication and re-renders Provider/alert routing secrets; Attacker observes a rotated Provider secret on the next call-scoped lease.
- The catalog discovers one additional Provider, three additional Skills, one additional Case Pack, and three additional Capability Contracts.
- Enterprise equipment follows the AtlasClaw Provider patterns: read-only datasource capabilities are distinct from risky execution, outputs are normalized, and high-risk execution remains protected by Core approval policy.

## File map

### Permanent regression coverage

- Create `tests/equipment/test_catalog_regressions.py` for deterministic reload, immutable-version conflicts, and invalid package enablement.
- Create `tests/equipment/test_harness_regressions.py` for operation fingerprint idempotency, secret redaction, atomic lease persistence, and principal-scoped cleanup recovery.
- Create `tests/infrastructure/test_secret_rotation.py` for mounted-file hot rotation, symlink/path traversal rejection, and Vault reference parsing.
- Create `tests/repositories/test_job_recovery.py` for idempotent enqueue, expired lease retry/exhaustion, and stale worker metrics.
- Create `tests/observability/test_operational_metrics.py` for bounded Prometheus queue/lease/worker gauges.
- Create `tests/operations/test_deployment_assets.py` for Prometheus, Alertmanager, OTel/Loki, Vault Agent, and Compose wiring.
- Create `tests/equipment/test_enterprise_equipment.py` for package discovery, contract checks, deterministic evaluator behavior, and high-risk contract metadata.

### Operational integration

- Modify `app/repositories/job_repository.py` to return aggregate queue/lease/worker facts only.
- Modify `app/observability.py` to publish job-state, queue-age, expired-lease, and stale-worker gauges.
- Modify `app/api/metrics.py` to refresh repository-backed gauges before serialization.
- Modify `deploy/prometheus.yml` to load rules and route to Alertmanager.
- Create `deploy/prometheus-alerts.yml` with readiness, HTTP 5xx, queue age, failed job, expired lease, provider error, cleanup failure, checksum mismatch, and sandbox termination alerts.
- Create `deploy/alertmanager.yml` with severity grouping, inhibition, and a generic webhook whose URL comes from the rotation-safe `/run/attacker-alert-secrets/alert_webhook_url` directory mount.
- Modify `deploy/otel-collector.yml` to tail structured JSON logs and export them to Loki over native OTLP while retaining trace export.
- Create `deploy/loki.yml` for a single-binary internal log backend with filesystem retention.
- Create `deploy/vault-agent.hcl` and `deploy/vault-agent-provider-secrets.ctmpl` for token-file auto-auth and periodic KV v2 rendering.
- Modify `docker-compose.production.yml` to add Loki, Alertmanager, and optional Vault Agent services, startup ordering, mounts, limits, secrets, and persistent volumes.
- Modify `docs/operations/production-runbook.md` and `.env.example` with alert routing, log queries, Vault rotation, validation, and rollback procedures.

### Enterprise equipment

- Create `contracts/enterprise.resource.read.v1/` with a low-risk read contract returning one normalized resource.
- Create `contracts/enterprise.alert.read.v1/` with a low-risk read contract returning one normalized alert.
- Create `contracts/enterprise.change.execute.v1/` with a high-risk external-call contract requiring an operation ID and evidence.
- Create `equipment/providers/enterprise-ops-provider/` with HTTPS-only, allowlisted, non-redirecting resource/alert reads and change execution using a call-scoped bearer secret.
- Create `equipment/skills/enterprise-resource-compliance-evaluator/` to request normalized resource facts and deterministically assess exposure, encryption, backup, monitoring, and patch posture.
- Create `equipment/skills/enterprise-alert-triage-evaluator/` to request normalized alert facts and classify active, persistent, noisy, muted, and recovered states without mutating the alert.
- Create `equipment/skills/enterprise-change-risk-evaluator/` to block changes missing approval, rollback, maintenance-window, ownership, or bounded blast radius.
- Create `equipment/casepacks/enterprise-operations-controls-v1/` with representative resource, alert, and change-control cases.
- Modify `.github/workflows/equipment-contract.yml`, `README.md`, and `docs/equipment-development.md` to validate and document the new packages.

### Task 1: Add catalog and Harness regression coverage

- [x] Write failing tests that construct temporary packages and assert deterministic discovery, `immutable_version_conflict`, and invalid-package disablement.
- [x] Run `uv run pytest tests/equipment/test_catalog_regressions.py -q` and confirm failures are caused by missing test fixtures or uncovered behavior.
- [x] Add only reusable test fixtures required to exercise the existing public Catalog/Repository interfaces.
- [x] Write failing Harness tests for same-operation replay, changed request fingerprints, complete redaction, atomic Resource Lease persistence, and principal-scoped cleanup recovery.
- [x] Run `uv run pytest tests/equipment/test_harness_regressions.py -q` and confirm each test catches the intended invariant.
- [x] Make the minimum production correction only if a regression exposes a real invariant violation.
- [x] Run both regression modules and the existing suite.

### Task 2: Add durable job and Secret rotation regression coverage

- [x] Write a failing mounted-file rotation test that leases `file:enterprise/api_token`, atomically replaces the file, and expects the next lease to expose only the new value.
- [x] Add traversal, symlink, empty-file, and Vault reference validation cases.
- [x] Run `uv run pytest tests/infrastructure/test_secret_rotation.py -q` and verify RED for rotation semantics before any resolver change.
- [x] Write failing job tests for enqueue identity, lease expiry retry, retry exhaustion, cancellation, and stale worker aggregation.
- [x] Run `uv run pytest tests/repositories/test_job_recovery.py -q` and verify RED for the missing aggregate snapshot API.
- [x] Implement `JobRepository.metrics_snapshot(stale_after_seconds=...)` with aggregate SQL only.
- [x] Run both modules and confirm GREEN.

### Task 3: Publish database-backed operational metrics

- [x] Write a failing test for `refresh_job_metrics()` that expects gauges for each status, oldest ready age, expired active leases, and stale workers.
- [x] Run `uv run pytest tests/observability/test_operational_metrics.py -q` and confirm the helper is missing.
- [x] Add bounded Prometheus gauges and an update function in `app/observability.py`.
- [x] Update `/metrics` to read `request.app.state.job_repository`, refresh aggregates, and then serialize metrics.
- [x] Ensure repository errors do not return stale success: log a bounded error and publish a scrape-refresh failure gauge.
- [x] Run the focused module and API security tests.

### Task 4: Connect alerting, centralized logs, and rotation assets

- [x] Write failing deployment-asset tests that parse YAML/HCL text and assert Prometheus rule loading, Alertmanager routing via `url_file`, OTel filelog-to-Loki, Loki storage, Vault Agent auto-auth/template refresh, and Compose service/mount/secret wiring.
- [x] Run `uv run pytest tests/operations/test_deployment_assets.py -q` and confirm missing assets fail.
- [x] Add Prometheus rules whose expressions use only metrics emitted by Attacker.
- [x] Add Alertmanager severity routing, warning inhibition under matching critical alerts, and webhook secret delivery.
- [x] Add OTel `filelog` JSON parsing and native OTLP HTTP export to Loki; retain bounded memory and batch processors.
- [x] Add Loki single-binary configuration and Compose service with internal-only networking and persistent storage.
- [x] Add Vault Agent token-file auto-auth and a KV v2 template that renders Provider and alert webhook secrets with restrictive permissions.
- [x] Update Compose with startup ordering, read-only roots, dropped capabilities, resource limits, secret files, and an opt-in `vault-agent` profile.
- [x] Run deployment-asset tests and `docker compose -f docker-compose.production.yml config --quiet`.

### Task 5: Add enterprise Capability Contracts and Provider

- [x] Write failing tests that expect the three contracts and `enterprise-ops-provider` to validate, expose correct risk/side-effect metadata, and reject unsafe configuration.
- [x] Run the focused enterprise equipment tests and confirm missing packages fail.
- [x] Add strict JSON request/response schemas for resource read, alert read, and change execution.
- [x] Add manifests with required Evidence, response limits, declared error codes, and high-risk change metadata.
- [x] Implement the Provider with HTTPS URL validation, manifest/instance host allowlists, no redirects, call-scoped bearer credentials, normalized stable outputs, and bounded error mapping.
- [x] Add offline contract scenarios for `describe`, `validate_config`, and rejection behavior that do not contact a real enterprise system.
- [x] Run Provider validation and contract checks on Windows.

### Task 6: Add enterprise evaluator Skills and Case Pack

- [x] Write failing direct-handler tests for resource exposure/encryption findings, noisy/persistent alert classification, and change blocking when approval/rollback/blast-radius controls are absent.
- [x] Write failing catalog tests that expect all three Skills and the enterprise Case Pack.
- [x] Implement resource and alert Skills as two-step Capability Broker continuations: request facts once, then evaluate the matching `capability_results` entry.
- [x] Implement change risk as a pure deterministic evaluator; it must never execute the change capability.
- [x] Add strict input/output schemas, bounded evidence summaries, offline contract scenarios, documentation, and representative cases.
- [x] Run validation/contract tests for all new Skills and confirm deterministic repeated output.

### Task 7: Documentation, CI, and delivery gate

- [x] Add every new Provider and Skill to the Ubuntu/Windows equipment Contract workflow.
- [x] Document component ownership, secret rotation semantics, alert delivery verification, Loki queries, and the boundary between evaluation and change execution.
- [x] Run `uv run ruff format --check .`.
- [x] Run `uv run ruff check .`.
- [x] Run `uv run pyright`.
- [x] Run `uv run pytest -q`.
- [x] Run `uv run python -m compileall -q app conf alembic equipment`.
- [x] Run all built-in package validation/contract commands; Docker-dependent Compose/promtool/amtool/Loki/OTel validators are wired into supply-chain CI because Docker is unavailable locally.
- [x] Run `git diff --check`, inspect the complete diff, commit scoped changes, push `feat/enterprise-readiness-completion`, and report any target-environment-only validation limits.
