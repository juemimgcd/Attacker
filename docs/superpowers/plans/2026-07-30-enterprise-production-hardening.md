# Enterprise Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Attacker deployable as a production-oriented, non-identity-managed evaluation service with PostgreSQL-backed coordination, external secret resolution, exported telemetry, reproducible deployment, and operator recovery procedures.

**Architecture:** Preserve the current FastAPI/service/repository boundaries and synchronous request APIs. Add a PostgreSQL-compatible persistence layer and a durable job lease queue as an optional distributed execution path, keep SQLite for local development, and select the LangGraph checkpointer from configuration. Production safety is enforced by a startup profile validator; secrets, telemetry, deployment, backup, and verification remain separate focused modules.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy asyncio, Alembic, PostgreSQL/asyncpg, LangGraph PostgreSQL checkpointer, Prometheus client, OpenTelemetry OTLP, Docker Compose, GitHub Actions.

---

## Scope and success criteria

Identity, OIDC, user management, and RBAC are explicitly excluded. The existing deployment API key remains the control-plane boundary.

This branch is successful when:

1. SQLite remains usable for local development and all existing tests pass.
2. PostgreSQL is a supported application database and checkpointer, with migration-driven startup.
3. Durable jobs can be enqueued, leased by one worker, heartbeated, retried after lease expiry, completed, and inspected.
4. Production startup fails closed for unsafe settings instead of silently using development defaults.
5. Provider secrets can be resolved from environment, mounted files, or Vault KV v2 without persisting raw values.
6. Health, readiness, Prometheus metrics, request correlation, and optional OTLP tracing are available.
7. A non-root app container, PostgreSQL-backed Compose stack, worker, migration job, backup/restore scripts, and operator runbook exist.
8. CI validates code quality, migrations, PostgreSQL compatibility, package contracts, container construction, dependency vulnerabilities, and SBOM generation.

Per repository policy, this implementation does not add or modify test files. Existing tests and non-test verification commands are used throughout.

## File map

### Runtime and persistence

- Modify `conf/settings.py`: database pools, checkpoint URL, worker, secret, observability, and production profile validation.
- Modify `app/infrastructure/database.py`: dialect-aware engine options, connectivity/readiness checks, and migration-only production behavior.
- Create `app/infrastructure/checkpoint.py`: SQLite/PostgreSQL checkpointer context manager.
- Create `app/runtime.py`: shared runtime construction and shutdown for API and workers.
- Modify `app/core/lifespan.py`: use the shared runtime and graceful shutdown.
- Modify `app/repositories/run_repository.py`: replace SQLite-only conflict insertion with portable transactional handling.
- Modify `app/models.py`: durable job and worker heartbeat records.
- Create `app/schemas/job_schema.py`: strict queue API contracts.
- Create `app/repositories/job_repository.py`: enqueue, lease, heartbeat, retry, complete, and inspect operations.
- Create `app/services/job_service.py`: payload dispatch to existing run services.
- Create `app/api/jobs.py`: optional distributed job endpoints.
- Create `alembic/versions/20260730_0007_enterprise_runtime.py`: job/worker schema and indexes.
- Modify `app/cli.py`: `worker` and production configuration validation commands.

### Security and secrets

- Create `app/infrastructure/secrets.py`: environment, mounted-file, and Vault KV v2 resolvers plus broker factory.
- Modify `app/equipment/security.py`: asynchronous leased-secret interface with memory clearing.
- Modify `app/services/harness_service.py`: await leased secret resolution.
- Modify `app/equipment/catalog.py`: production profile continues to enforce package signatures and revocation checks.
- Modify `.env.example`: safe local defaults plus documented production variables.

### Observability and health

- Create `app/observability.py`: request ID middleware, Prometheus instruments, optional OpenTelemetry configuration, and shutdown.
- Modify `app/equipment/metrics.py`: preserve snapshots while exporting bounded Prometheus series.
- Modify `conf/logging.py`: structured JSON logging option and correlation fields.
- Modify `app/api/health.py`: liveness, dependency-aware readiness, and compatibility health route.
- Create `app/api/metrics.py`: Prometheus endpoint guarded by an optional monitoring key.
- Modify `main.py`: install middleware and routes.

### Delivery and operations

- Create `Dockerfile`: locked, non-root production image with healthcheck.
- Create `docker-compose.production.yml`: migration, PostgreSQL, API, worker, and optional telemetry services.
- Create `deploy/prometheus.yml`: scrape configuration.
- Create `deploy/otel-collector.yml`: OTLP receiver/export pipeline.
- Create `scripts/backup.sh`: consistent PostgreSQL and immutable equipment archive backup.
- Create `scripts/restore.sh`: explicit-confirmation restore into an empty target.
- Create `docs/operations/production-runbook.md`: deploy, upgrade, rollback, SLO, alert, backup, restore, and incident procedures.
- Modify `README.md`: production profile and distributed execution entry points.

### Verification and supply chain

- Modify `.github/workflows/ci.yml`: PostgreSQL service, migrations, production configuration validation, and existing checks.
- Modify `.github/workflows/equipment-contract.yml`: keep cross-platform package contracts.
- Create `.github/workflows/supply-chain.yml`: container build, vulnerability audit, and CycloneDX SBOM artifact.
- Modify `pyproject.toml` and regenerate `uv.lock`: production and verification dependencies.

## Task 1: Production configuration and portable persistence

- [x] Add PostgreSQL, pool, migration, checkpoint, worker, secret, and observability settings with bounded values.
- [x] Add a `Settings.validate_production()` method that rejects production debug mode, SQLite, missing API/metrics keys, unsigned equipment, absent external secret backend, and unconfigured sandbox isolation.
- [x] Make `Database` apply pool options only to non-SQLite engines and expose `ping()` and `readiness()`.
- [x] Disable `Base.metadata.create_all()` in the production profile; production schema comes only from Alembic.
- [x] Add a checkpoint context manager that chooses `AsyncSqliteSaver` or PostgreSQL `AsyncPostgresSaver`.
- [x] Replace the SQLite-only run-step upsert with a nested transaction and `IntegrityError` recovery.
- [x] Run the new migration through `upgrade head`, `downgrade 20260730_0006`, `upgrade head`, and run `uv run pytest -q`.

## Task 2: Durable distributed execution

- [x] Add `RunJobRecord` and `WorkerHeartbeatRecord` with queue, status, lease owner/expiry, attempts, payload/result/error summaries, and timestamps.
- [x] Add migration `20260730_0007` with indexes for `(status, available_at)` and lease expiry.
- [x] Implement PostgreSQL claiming with `SELECT ... FOR UPDATE SKIP LOCKED`; use a serialized optimistic fallback for SQLite development.
- [x] Make enqueue idempotent by caller-supplied request ID and reject a reused ID with a different payload fingerprint.
- [x] Dispatch deterministic, adaptive, deterministic gray-box, and stateful payloads through their existing Pydantic request models and services.
- [x] Add `/jobs`, `/jobs/{id}`, `/jobs/{id}/retry`, and `/jobs/{id}/cancel`.
- [x] Add `attacker worker --worker-id ID --concurrency N --poll-seconds S` with SIGTERM drain, lease heartbeat, bounded retries, and stale lease recovery.
- [x] Run a local one-shot enqueue/worker/status smoke command without adding test files.

## Task 3: Production security profile and external secrets

- [x] Change secret leasing to an async context manager and keep secret values out of persisted exception details.
- [x] Support `env:NAME`, `file:relative/path`, and `vault:path/to/secret#field` references.
- [x] Constrain file references to a configured mounted-secret root and reject symlinks/path traversal.
- [x] Resolve Vault KV v2 over HTTPS using a token from an environment variable or mounted token file; support namespace and bounded timeouts.
- [x] Build the configured broker in shared runtime construction and inject it into `HarnessService`.
- [x] Enforce signature-required, executable-package, untrusted-sandbox, API key, database, checkpoint, and secret-backend invariants in production.
- [x] Add `attacker config validate-production` for deployment gates without starting the API.

## Task 4: Observability and health

- [x] Add correlation middleware that accepts a safe `X-Request-ID` or creates one and returns it in the response.
- [x] Record bounded request count, latency, in-progress requests, job states, provider outcomes, sandbox terminations, and readiness.
- [x] Expose Prometheus text at `/metrics`, with a separate monitoring API key when configured.
- [x] Configure OTLP traces only when an endpoint is supplied and flush providers during graceful shutdown.
- [x] Add `/health/live` and `/health/ready`; readiness checks database connectivity, checkpoint initialization, and catalog load state.
- [x] Keep `/health` as a compatibility endpoint reporting aggregate readiness.
- [x] Configure JSON logs in production and ensure request/job/run/provider identifiers are bindable fields.

## Task 5: Production deployment and recovery assets

- [x] Build a multi-stage, non-root image from `uv.lock` and include only runtime/application assets.
- [x] Add Compose services for PostgreSQL, Alembic migration, API, worker, Prometheus, and OpenTelemetry Collector with health/dependency ordering.
- [x] Use Docker secrets or mounted files for database, control-plane, monitoring, Vault, and telemetry credentials.
- [x] Add CPU/memory/PID limits, read-only root filesystem where supported, temporary writable mounts, `no-new-privileges`, and graceful stop periods.
- [x] Add versioned backup metadata, `pg_dump`, equipment archive backup, checksum manifest, and retention hooks.
- [x] Require explicit confirmation and an empty destination for restore; verify checksums before applying data.
- [x] Document deployment, migrations, upgrade/rollback, worker drain, stale leases, backup/restore, SLOs, alerts, and incident response.

## Task 6: Verification matrix and supply chain

- [x] Extend CI with a PostgreSQL service and run Alembic plus existing tests against SQLite and PostgreSQL-compatible repository paths.
- [x] Validate the production configuration using secret placeholders supplied only to the CI process.
- [x] Build the production container and validate Compose syntax in CI.
- [x] Run dependency vulnerability auditing with a documented allowlist mechanism.
- [x] Generate and retain a CycloneDX SBOM for every branch/PR build.
- [x] Run Ruff format/check, Pyright, all existing tests, package validation/contracts, migration round-trip, and Python compilation locally.
- [x] Review the final diff for accidental identity/RBAC scope, raw secrets, unsafe defaults, and unrelated changes.

## Task 7: Commit and publish

- [x] Confirm the worktree is clean except for this branch's intended changes.
- [x] Commit with a scoped production-hardening message.
- [x] Push `feat/enterprise-production-hardening` and report verification evidence plus environment-dependent checks that were not executed locally.

## Verification evidence

- Local: Ruff format/check, Pyright, 22 existing pytest cases, Python compilation, SQLite
  migration round trip, production-profile validation, API/job/worker end-to-end smoke,
  two Provider validations, five Provider/Skill contract checks, dependency audit, and
  CycloneDX SBOM generation.
- CI-defined: PostgreSQL migration/worker smoke, production image build, Compose parsing,
  and image vulnerability scan.
- Environment limitation: Docker and the PostgreSQL client are not installed on the local
  Windows workstation, so the CI-defined checks above were not represented as local passes.
