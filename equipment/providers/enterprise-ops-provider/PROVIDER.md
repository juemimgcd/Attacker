# Enterprise Operations Provider

This built-in reference Provider demonstrates the enterprise package boundary used by Attacker.
It exposes read-only resource and alert datasource capabilities separately from a high-risk
change execution capability. Instances are disabled by default until an operator supplies an
HTTPS endpoint, an allowlisted host, and a call-scoped `api_token` Secret reference.

The Provider normalizes external payloads before they enter Skills. It never persists bearer
credentials, follows redirects, or bypasses Core Policy Gate approval for
`enterprise.change.execute.v1`.
