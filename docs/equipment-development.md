# Attacker Equipment Development

Attacker equipment is an offline, deployment-owned extension mechanism for security evaluation. It is not a public marketplace and never downloads executable packages from an unknown URL.

## Package boundaries

- A **Capability Contract** is published by Core under `contracts/` and owns request/response schemas, risk, idempotency, Evidence, cleanup, limits, and error semantics.
- A **Provider Package** implements contracts. A separately persisted **Provider Instance** binds non-sensitive config, secret references, allowed hosts, and immutable config/secret revisions.
- A **Skill** depends on named contract bindings and receives only `SkillContext`; it does not receive a DB session, global settings, raw Provider config, Provider clients, or a generic Secret reader.
- A **Case Pack** is data-only.

Use `app.equipment.sdk` for the narrow Provider/Skill protocols. Manifests are validated without importing package Python.

## Local workflow

```powershell
attacker equipment validate equipment/providers/my-provider --type provider
attacker equipment reload
attacker equipment scaffold provider my-provider
attacker equipment import my-signed-provider.zip
attacker equipment contract-test equipment/providers/my-provider --type provider
attacker equipment list --type provider
attacker provider-instance healthcheck my-instance
attacker skill dry-run my-skill --payload '{\"evidence\":[]}'
attacker casepack validate equipment/casepacks/my-pack
```

Provider config and Skill input/output files use JSON Schema. The v1 runtime supports the JSON Schema object/array/scalar types, `required`, `properties`, `enum`, `items`, and `additionalProperties: false`.

## Trust and isolation

| Trust | Runtime | Guarantee |
|---|---|---|
| `trusted_builtin` | Core process | Shipped and reviewed with Core |
| `trusted_enterprise` | JSON subprocess | Crash, timeout, and protocol isolation only |
| `untrusted` | Strong OS/container sandbox | Rejected when the platform lacks that backend |

A subprocess is not a security sandbox. On Windows, `untrusted` packages are rejected. Linux deployment must supply a reviewed container backend with a non-root user, read-only root, resource limits, network default-deny, minimal mounts, seccomp/AppArmor, `no-new-privileges`, and teardown.

`trusted_builtin` is Core-owned provenance, not a permission an enterprise Manifest can
grant itself. Core accepts built-in packages only at a shipped package path with a pinned
ID and Core trust checksum. The trust checksum uses stable path ordering and canonical
UTF-8 text line endings, so Core provenance is identical on Windows and Linux. Persisted
package revision checksums remain byte-exact for compatibility with existing catalogs,
signatures, archives, Run snapshots, and revocation entries. Python bytecode caches are
ignored for legacy compatibility but never archived or used to load an entrypoint; bytecode
artifacts outside a cache directory are rejected. Offline and local executable packages
must use `trusted_enterprise` or, where available, `untrusted`.

## Supply-chain controls

Discovery rejects unsafe paths, symlinks, excessive depth/file count/size, missing entrypoints, malformed Python, incompatible versions, unknown contracts, and immutable version checksum conflicts.

When `EQUIPMENT__REQUIRE_SIGNATURE=true`, every package must contain `SIGNATURE.json`:

```json
{"publisher_id":"internal-security","signature_id":"release-2026-07","algorithm":"ed25519","signature":"BASE64_SIGNATURE"}
```

The signature covers the lowercase SHA-256 checksum string of all package files except `SIGNATURE.json`. `EQUIPMENT__TRUST_ROOTS_FILE` is a JSON object mapping publisher IDs to base64 Ed25519 public keys. A signature authenticates source and integrity; it does not make code safe and does not reduce the required runtime isolation.

`EQUIPMENT__REVOCATIONS_FILE` points to a local JSON revocation document. Its
`publisher_ids`, `checksums`, and `signature_ids` arrays are checked on every discovery.
A revoked package is invalid and cannot be selected for a new Run. Offline ZIP imports
retain an immutable archive source reference; reusing an existing package ID/version with
different content is rejected without modifying the previously registered package.

Provider Instance `secret_refs` may bind a deployment environment reference such as
`{"agent_token": "env:ENTERPRISE_AGENT_TOKEN"}`. Core resolves it only for the current
Provider call; the Skill context, package/config snapshot, Event, report, and replay retain
only the reference and `secret_binding_revision`.

## Run bindings and Capability Broker

At Run creation, Core freezes the selected Provider and Skill manifests, Case Pack,
Capability Contracts, checksums, Provider Instance, non-sensitive config, config revision,
secret-binding revision, test principal, and target binding. Catalog reloads and deployment
disable operations affect only new Runs. Active Runs load archived package bytes and the
exact frozen Provider Instance revision.

Skills request external work declaratively by returning a bounded
`capability_requests` list. Each request names a Manifest binding and stable request ID.
Core resolves the binding, enforces Run Policy and Contract schemas, invokes the Provider,
redacts the result, persists Evidence and Resource Leases, and re-enters the Skill with
`capability_results`. A Skill never receives a Provider object, database session, raw
secret, or unrestricted network handle. Reusing a request ID with a different payload is
rejected.

Provider completion, its bounded redacted result, and Resource Leases commit in one
database transaction. Reusing an equipment `operation_id` requires the same package,
instance, principal, Contract, and input fingerprint. A duplicate of an unfinished
operation is returned as `in_doubt` and is never invoked again implicitly.
Executions created before request fingerprints and structured results were introduced are
also returned as `in_doubt` with `legacy_result_unavailable`; Core does not guess an input
identity or claim an empty legacy result is complete. Repeated completed Provider calls
return the Resource Leases already committed with the original execution.

Resource cleanup is limited to persisted Resource Leases. Cleanup operation IDs are stable,
attempt counts and errors are retained, and startup recovery retries active or failed
leases using the frozen Provider revision. A cleanup failure remains visible in reports and
does not remove an already supported Finding.

Same-binding Replay rejects different non-secret Target behavior, including name, endpoint,
method, header names, authentication shape, timeout, and request template, while allowing
credential values to be resupplied or rotated. It preserves the source equipment target
and principal bindings. Upgrade Comparison reports changes by comparing persisted source
and replay Run facts, including Provider, Instance, config, Skill, Case Pack, Contract,
Target, semantic Policy, and Test Principal dimensions.

## CI contract

Package CI should run local validation and `contract-test` on Windows and Linux.
Contract checks validate the exact async adapter method signatures and every declared
Capability reference in addition to manifest and JSON Schema validation. Deployment-owned
offline scenarios should then exercise each declared Capability against its request and
response schemas. Repository checks remain:

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

Do not log complete Skill inputs, Provider responses, credentials, or secrets. Findings must be created by Core/evaluator logic from persisted Evidence, never directly by a Provider.

## Enterprise operations reference equipment

The built-in `enterprise-ops-provider` applies the reusable boundaries demonstrated by
`atlasclaw-providers` without importing AtlasClaw or SmartCMP implementation code:

- `enterprise.resource.read.v1` and `enterprise.alert.read.v1` are read-only datasource
  contracts that normalize upstream facts before a Skill sees them.
- `enterprise-resource-compliance-evaluator` and
  `enterprise-alert-triage-evaluator` use a two-step Capability Broker continuation and never
  call an external system directly.
- `enterprise-change-risk-evaluator` is a deterministic, zero-capability evaluator. It can
  recommend whether a proposed change is controlled, but cannot execute it.
- `enterprise.change.execute.v1` is a separate high-risk Provider contract. Core requires the
  capability to be in `approved_high_risk_capabilities`; a Skill or Provider cannot waive that
  approval.

Instances are disabled by default. Configure an HTTPS endpoint, keep its hostname inside both
the Provider manifest and instance allowlists, and bind `api_token` through `file:` or `vault:`.
The default production file reference is `file:enterprise-ops/api_token`.
