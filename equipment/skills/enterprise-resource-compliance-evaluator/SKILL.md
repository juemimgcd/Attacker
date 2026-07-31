# Enterprise Resource Compliance Evaluator

This evaluator follows the datasource-first pattern used by the SmartCMP resource-compliance
skill. It requests one normalized resource snapshot, then evaluates five deterministic controls.
It never calls the enterprise API directly and never performs remediation.
