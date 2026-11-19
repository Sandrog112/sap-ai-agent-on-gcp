# Read by cli.py and tests/test_live.py.

output "project_id" {
  value = var.project_id
}

output "region" {
  value = var.region
}

output "runtime_name" {
  description = "Resource name of the agent on Agent Runtime."
  value       = google_vertex_ai_reasoning_engine.agent.name
}

output "tool_server_url" {
  description = "MCP URL of the tool server; only its invokers can call it."
  value       = "${google_cloud_run_v2_service.tools.uri}/mcp"
}

output "tool_service" {
  value = google_cloud_run_v2_service.tools.name
}

output "memory_bank_name" {
  value = data.external.memory_bank.result.name
}

output "gemini_api_key_secret" {
  value = google_secret_manager_secret.gemini_api_key.secret_id
}

output "cloud_sql_instance" {
  value = local.sql_connection_name
}

output "database" {
  value = google_sql_database.sales.name
}

output "checkpoint_database" {
  value = google_sql_database.checkpoints.name
}

output "service_accounts" {
  value = { for kind, account in google_service_account.this : kind => account.email }
}
