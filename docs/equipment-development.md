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

## Supply-chain controls

Discovery rejects unsafe paths, symlinks, excessive depth/file count/size, missing entrypoints, malformed Python, incompatible versions, unknown contracts, and immutable version checksum conflicts.

When `EQUIPMENT__REQUIRE_SIGNATURE=true`, every package must contain `SIGNATURE.json`:

```json
{"publisher_id":"internal-security","algorithm":"ed25519","signature":"BASE64_SIGNATURE"}
```

The signature covers the lowercase SHA-256 checksum string of all package files except `SIGNATURE.json`. `EQUIPMENT__TRUST_ROOTS_FILE` is a JSON object mapping publisher IDs to base64 Ed25519 public keys. A signature authenticates source and integrity; it does not make code safe and does not reduce the required runtime isolation.

Provider Instance `secret_refs` may bind a deployment environment reference such as
`{"agent_token": "env:ENTERPRISE_AGENT_TOKEN"}`. Core resolves it only for the current
Provider call; the Skill context, package/config snapshot, Event, report, and replay retain
only the reference and `secret_binding_revision`.

## CI contract

Package CI should run local validation on Windows and Linux, then exercise every declared Capability against its Core contract request/response schemas. Repository checks remain:

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

Do not log complete Skill inputs, Provider responses, credentials, or secrets. Findings must be created by Core/evaluator logic from persisted Evidence, never directly by a Provider.
