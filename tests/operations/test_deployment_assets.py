from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def _yaml(relative: str) -> dict:
    return yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))


def test_prometheus_routes_attacker_rules_to_alertmanager() -> None:
    prometheus = _yaml("deploy/prometheus.yml")
    assert any(path.endswith("/prometheus-alerts.yml") for path in prometheus["rule_files"])
    targets = prometheus["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
    assert targets == ["alertmanager:9093"]
    scrape_targets = {
        job["job_name"]: job["static_configs"][0]["targets"] for job in prometheus["scrape_configs"]
    }
    assert scrape_targets["attacker-worker"] == ["worker:9100"]

    rules = _yaml("deploy/prometheus-alerts.yml")
    names = {rule["alert"] for group in rules["groups"] for rule in group["rules"]}
    assert {
        "AttackerMetricsTargetDown",
        "AttackerReadinessUnavailable",
        "AttackerHighHttp5xxRate",
        "AttackerJobQueueStalled",
        "AttackerExpiredJobLease",
        "AttackerProviderFailures",
        "AttackerCleanupFailures",
    } <= names
    integrity = next(
        rule
        for group in rules["groups"]
        for rule in group["rules"]
        if rule["alert"] == "AttackerPackageIntegrityFailure"
    )
    assert "attacker_equipment_invalid_packages > 0" in integrity["expr"]


def test_alertmanager_uses_secret_backed_webhook_and_inhibition() -> None:
    config = _yaml("deploy/alertmanager.yml")
    receiver = next(item for item in config["receivers"] if item["name"] == "enterprise-webhook")
    assert (
        receiver["webhook_configs"][0]["url_file"]
        == "/run/attacker-alert-secrets/alert_webhook_url"
    )
    assert config["inhibit_rules"]


def test_otel_collector_sends_structured_file_logs_to_loki() -> None:
    collector = _yaml("deploy/otel-collector.yml")
    assert "filelog/attacker" in collector["receivers"]
    assert collector["receivers"]["filelog/attacker"]["start_at"] == "beginning"
    assert collector["exporters"]["otlphttp/loki"]["endpoint"] == "http://loki:3100/otlp"
    assert collector["extensions"]["file_storage/attacker"]["create_directory"] is True
    logs = collector["service"]["pipelines"]["logs"]
    assert "filelog/attacker" in logs["receivers"]
    assert "otlphttp/loki" in logs["exporters"]
    assert "resource/attacker" in logs["processors"]
    resource_attributes = collector["processors"]["resource/attacker"]["attributes"]
    assert {
        item["key"]: item["value"] for item in resource_attributes if item["action"] == "upsert"
    }["service.name"] == "attacker"

    loki = _yaml("deploy/loki.yml")
    assert loki["auth_enabled"] is False
    assert loki["storage_config"]["filesystem"]["directory"] == "/loki/chunks"


def test_vault_agent_refreshes_provider_and_alert_secrets() -> None:
    config = (ROOT / "deploy/vault-agent.hcl").read_text(encoding="utf-8")
    provider_template = (ROOT / "deploy/vault-agent-provider-secrets.ctmpl").read_text(
        encoding="utf-8"
    )
    alert_template = (ROOT / "deploy/vault-agent-alert-webhook.ctmpl").read_text(encoding="utf-8")
    assert 'type = "token_file"' in config
    assert "static_secret_render_interval" in config
    assert "error_on_missing_key = true" in config
    assert 'perms = "0440"' in config
    assert "enterprise-ops" in provider_template
    assert "api_token" in provider_template
    assert "alert_webhook_url" in alert_template


def test_compose_wires_loki_alertmanager_and_optional_vault_agent() -> None:
    compose = _yaml("docker-compose.production.yml")
    services = compose["services"]
    assert {"loki", "alertmanager", "otel-storage-init", "vault-agent"} <= services.keys()
    assert services["vault-agent"]["profiles"] == ["vault"]
    assert services["vault-agent"]["user"] == "10001:10001"
    assert services["alertmanager"]["user"] == "65534:10001"
    assert (
        services["otel-collector"]["depends_on"]["otel-storage-init"]["condition"]
        == "service_completed_successfully"
    )
    assert services["otel-storage-init"]["network_mode"] == "none"
    assert services["otel-storage-init"]["cap_add"] == ["CHOWN"]
    assert (
        "${ATTACKER_ALERT_SECRETS_DIR:-./deploy/alert-secrets}:/run/attacker-alert-secrets:ro"
    ) in services["alertmanager"]["volumes"]
    assert any(
        item.startswith("/var/lib/attacker-prometheus:") for item in services["api"]["tmpfs"]
    )
    assert "prometheus-multiproc" not in compose["volumes"]
    assert "alert_webhook_url" not in compose["secrets"]
    assert any("/health/ready" in item for item in services["api"]["healthcheck"]["test"])
    assert services["prometheus"]["depends_on"]["api"]["condition"] == "service_started"
    assert "--metrics-port" in services["worker"]["command"]
    assert "loki-data" in compose["volumes"]
    assert "alertmanager-data" in compose["volumes"]


def test_runtime_secret_directories_are_gitignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "deploy/secrets/" in gitignore
    assert "deploy/provider-secrets/" in gitignore
    assert "deploy/alert-secrets/" in gitignore
