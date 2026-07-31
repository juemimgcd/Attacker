# Enterprise Alert Triage Evaluator

This evaluator applies the SmartCMP alarm pattern at the Attacker boundary: acquire a normalized
read-only alert snapshot, classify it deterministically, and recommend an operator action. It
does not mute, resolve, reopen, or otherwise mutate the upstream alert.
