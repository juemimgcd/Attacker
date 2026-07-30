# Attacker Production Runbook

## Supported production shape

This runbook covers a PostgreSQL-backed API tier plus one or more durable workers. Identity,
OIDC, user management, and RBAC are intentionally outside this deployment. Keep the API behind
an enterprise gateway or private load balancer and retain the deployment API key as a second
service boundary.

SQLite and the local LangGraph checkpoint file are development-only. Production schema changes
are applied only by Alembic. A successful container start is not evidence of high availability:
the database, ingress, volumes, secret backend, telemetry backend, and backup destination must
each meet the operator's availability target.

## Required secrets and configuration

Create an operator-controlled directory that is not committed to Git:

```bash
install -d -m 0700 deploy/secrets deploy/provider-secrets
openssl rand -base64 48 > deploy/secrets/postgres_password
python -c 'import secrets; print(secrets.token_urlsafe(48))' > deploy/secrets/api_key
python -c 'import secrets; print(secrets.token_urlsafe(48))' > deploy/secrets/metrics_api_key
chmod 0600 deploy/secrets/*
```

Create `database_url` for SQLAlchemy/asyncpg and `checkpoint_url` for psycopg. Percent-encode
reserved characters in the password.

```text
postgresql+asyncpg://attacker:<encoded-password>@postgres:5432/attacker
postgresql://attacker:<encoded-password>@postgres:5432/attacker
```

Put those complete URLs into:

```text
deploy/secrets/database_url
deploy/secrets/checkpoint_url
```

Provider Instance secrets use references, never raw values:

- `file:smartcmp/api_token` resolves
  `deploy/provider-secrets/smartcmp/api_token`;
- `vault:teams/security/attacker#api_token` resolves a Vault KV v2 field when
  `SECRETS__BACKEND=vault`.

External Provider/Skill packages must have an Ed25519 `SIGNATURE.json`. Add publisher public
keys to `deploy/security/equipment_trust_roots.json`. Keep revoked publisher IDs, signature IDs,
and package checksums in `deploy/security/equipment_revocations.json`. Core-shipped packages are
verified against compiled checksums and do not require a separate signature.

## Build and configuration gate

```bash
docker build -t attacker:production .
docker compose -f docker-compose.production.yml config --quiet
docker compose -f docker-compose.production.yml run --rm api \
  attacker config validate-production
```

The production validator fails closed when it sees debug mode, SQLite, ORM auto-create, missing
control/metrics keys, non-PostgreSQL checkpoints, unsigned external equipment, environment
Provider Secrets, missing trust/revocation files, or an untrusted runtime without a container
image.

## Deploy and verify

```bash
docker compose -f docker-compose.production.yml up -d postgres otel-collector
docker compose -f docker-compose.production.yml run --rm migrate
docker compose -f docker-compose.production.yml up -d api worker prometheus
docker compose -f docker-compose.production.yml ps
```

Verify:

```bash
curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
curl --fail -H "X-API-Key: $ATTACKER_API_KEY" \
  http://127.0.0.1:8000/jobs
curl --fail -H "Authorization: Bearer $ATTACKER_METRICS_API_KEY" \
  http://127.0.0.1:8000/metrics
```

`/health/live` proves only that the process serves HTTP. `/health/ready` checks database
connectivity, the checkpointer, and equipment catalog initialization.

## Durable run jobs

Submit a secret-free job:

```bash
curl --fail -X POST http://127.0.0.1:8000/jobs \
  -H "X-API-Key: $ATTACKER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "request_id": "security-window-2026-07-30-001",
    "kind": "stateful",
    "payload": {"profile": "hardened"},
    "priority": 100
  }'
```

Reusing `request_id` with identical facts returns the same job. Reusing it with a different
kind or payload is rejected. Jobs containing fields such as passwords, API keys, credentials,
or tokens are rejected because queue payloads are durable. Bind those values through Provider
Instance Secret references.

Workers claim jobs through a database lease. PostgreSQL uses `FOR UPDATE SKIP LOCKED`; expired
leases are retried until `max_attempts` is exhausted. A worker receiving SIGTERM stops claiming,
heartbeats its current work, and drains for `WORKER__SHUTDOWN_GRACE_SECONDS`.

Inspect or operate a job:

```bash
curl -H "X-API-Key: $ATTACKER_API_KEY" http://127.0.0.1:8000/jobs/<job-id>
curl -X POST -H "X-API-Key: $ATTACKER_API_KEY" \
  http://127.0.0.1:8000/jobs/<job-id>/cancel
curl -X POST -H "X-API-Key: $ATTACKER_API_KEY" \
  http://127.0.0.1:8000/jobs/<job-id>/retry
```

Cancellation of a queued job is immediate. Cancellation of running target activity is
cooperative: it prevents further queue execution but cannot retract an already issued external
request. Target authorization and run-control endpoints remain the authoritative emergency
stops.

## Upgrade and rollback

Before upgrade:

1. Confirm a recent backup and restore drill.
2. Stop workers so no new jobs are claimed.
3. Wait for active worker heartbeats to reach zero or expire.
4. Build and scan the new image.
5. Read every Alembic migration and confirm its downgrade behavior.

Upgrade:

```bash
docker compose -f docker-compose.production.yml stop worker
docker compose -f docker-compose.production.yml run --rm migrate
ATTACKER_IMAGE=attacker:<new-version> \
  docker compose -f docker-compose.production.yml up -d api worker
```

Rollback application code only when the previous version supports the upgraded schema. If a
schema downgrade is required, stop API and workers, take another backup, run the explicit
Alembic downgrade target, and then start the previous image. Never run concurrent application
versions with incompatible schema expectations.

## Backup

The backup script requires PostgreSQL client tools, `tar`, `sha256sum`, and `realpath`.

```bash
export ATTACKER_BACKUP_ROOT=/srv/backups/attacker
export ATTACKER_DATABASE_URL_FILE="$PWD/deploy/secrets/checkpoint_url"
export ATTACKER_DATA_DIR=/var/lib/docker/volumes/attacker-production_attacker-data/_data
./scripts/backup.sh
```

It creates a timestamped directory containing:

- a custom-format `pg_dump`;
- the immutable equipment archive;
- metadata;
- SHA-256 checksums.

The script never deletes older backups. Apply retention in the backup platform only after
replication and restore verification. Encrypt backups at the storage layer and restrict access
because reports and evidence may contain sensitive customer information even after credential
redaction.

For a fully consistent storage boundary, drain workers and block package reload/config
mutations during the archive snapshot, or use an atomic volume snapshot coordinated with
`pg_dump`.

## Restore

Restore is intentionally fail-closed:

- the destination database must contain zero public tables;
- the destination data directory must exist and be empty;
- the checksum manifest must pass;
- archive paths must be relative and cannot contain `..`;
- `ATTACKER_RESTORE_CONFIRM=restore-empty-target` is mandatory.

```bash
export ATTACKER_RESTORE_CONFIRM=restore-empty-target
export ATTACKER_RESTORE_SOURCE=/srv/backups/attacker/20260730T120000Z
export ATTACKER_DATABASE_URL_FILE=/secure/empty_database_url
export ATTACKER_DATA_DIR=/srv/restore/attacker-data
./scripts/restore.sh
```

After restore, run `alembic current`, start one API replica without workers, verify readiness
and representative reports, then start workers. Record the elapsed time as RTO and the newest
restored event timestamp as RPO evidence.

## Suggested initial SLOs and alerts

Adopt customer-specific targets; these are conservative starting points:

| Indicator | Initial objective | Alert |
|---|---:|---|
| API readiness | 99.5% monthly | unavailable for 5 minutes |
| HTTP 5xx ratio | <1% over 15 minutes | >2% for 10 minutes |
| Job queue delay | p95 <30 seconds | p95 >2 minutes |
| Job terminal failure ratio | <2% excluding policy denials | >5% for 15 minutes |
| Expired worker leases | 0 sustained | any increase for 10 minutes |
| Provider healthcheck | >99% for required providers | 3 consecutive failures |
| Backup age | <24 hours | no successful backup for 30 hours |
| Restore drill | quarterly | overdue by 14 days |

Alert separately on sandbox termination, cleanup failure, invalid/revoked packages, package
signature failure, database pool exhaustion, OTLP export failure, and disk usage above 80%.

## Incident sequence

1. Revoke target authorization or terminate the affected run.
2. Stop workers if external side effects may continue.
3. Preserve database, logs, job lease state, equipment snapshots, and audit evidence.
4. Rotate affected Provider Secrets and the deployment API key.
5. Add compromised publisher/signature/checksum facts to the revocation file.
6. Recover active Resource Leases and verify cleanup results.
7. Restore service with one worker, then increase concurrency after validation.
8. Document timeline, affected Run IDs, evidence IDs, recovery decisions, and preventive action.

## Known boundaries

- This Compose topology is production-oriented but is not a complete HA platform.
- PostgreSQL HA, ingress HA, cross-zone volumes, external Vault HA, and telemetry retention are
  deployment responsibilities.
- The OTLP Collector example logs trace summaries; replace the debug exporter with the
  organization's authenticated backend.
- The egress network permits outbound access. Enforce destination policy with the platform
  firewall or egress proxy in addition to Attacker's target and Provider allowlists.
- Load, chaos, penetration, and disaster-recovery evidence must be produced in the target
  environment before an Enterprise GA claim.
