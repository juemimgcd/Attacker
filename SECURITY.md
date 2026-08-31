# Security Policy

## Supported versions

Security fixes are made on `master` and released from the current project version. Older tagged
releases are not maintained after a fixed release is available.

## Reporting a vulnerability

Do not publish credentials, exploit details, customer data, or a working proof of concept in a
public issue. Until GitHub private vulnerability reporting is enabled for this repository, open a
public issue containing only a request for a private reporting channel. A maintainer will provide
one before technical details are shared.

Include the affected version or commit, impact, required preconditions, and the smallest safe
reproduction. Reports about authorization, secret exposure, target validation, equipment package
execution, checkpoint isolation, or evidence integrity receive priority.

## Safe research boundary

Only test systems you own or are explicitly authorized to assess. Do not scan public targets,
access another user's data, retain secrets, or perform destructive actions. Use synthetic data and
the isolated profiles supplied with Attacker whenever possible.

## Disclosure

Please allow maintainers time to validate and release a fix before public disclosure. Security
advisories should identify affected versions, the fixed version, and any required remediation or
credential rotation.
