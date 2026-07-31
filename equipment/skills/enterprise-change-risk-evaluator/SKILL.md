# Enterprise Change Risk Evaluator

This Skill evaluates ownership, approval, rollback, maintenance-window, criticality, and blast
radius facts. It deliberately declares no execution capability. A separate workflow may submit
an approved `enterprise.change.execute.v1` request, but Core will still require an explicit
high-risk approval before the Provider call.
