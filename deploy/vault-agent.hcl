pid_file = "/tmp/vault-agent.pid"

auto_auth {
  method {
    type = "token_file"
    config = {
      token_file_path = "/run/secrets/vault_agent_token"
    }
  }

  sink "file" {
    config = {
      path = "/tmp/vault-agent-token"
      mode = 0400
    }
  }
}

template_config {
  static_secret_render_interval = "5m"
  exit_on_retry_failure = true
  max_connections_per_host = 4
}

template {
  source = "/vault/templates/provider-secrets.ctmpl"
  destination = "/rendered/provider-secrets/enterprise-ops/api_token"
  perms = "0440"
  error_on_missing_key = true
}

template {
  source = "/vault/templates/alert-webhook.ctmpl"
  destination = "/rendered/alert-secrets/alert_webhook_url"
  perms = "0440"
  error_on_missing_key = true
}
