locals {
  repository_root = abspath("${path.module}/..")

  tool_service_name   = "${var.name_prefix}-tools"
  runtime_name        = "${var.name_prefix}-runtime"
  memory_bank_name    = "${var.name_prefix}-memory"
  gemini_secret_id    = "${var.name_prefix}-gemini-api-key"
  repository_id       = var.name_prefix
  build_bucket_name   = "${var.project_id}-${var.name_prefix}-build"
  runtime_description = "Sales service agent for SAP-like sales data"
  tool_server_entry   = "python -m backend.server"
  tool_server_scaling = { min = 0, max = 3, concurrency = 20 }
  runtime_scaling     = { min = 0, max = 3, concurrency = 5 } # concurrency: the recommended 2 * cpu + 1
  sql_instance_name   = var.create_sql_instance ? google_sql_database_instance.main[0].name : data.google_sql_database_instance.existing[0].name
  sql_connection_name = var.create_sql_instance ? google_sql_database_instance.main[0].connection_name : data.google_sql_database_instance.existing[0].connection_name

  # Values the agent and the tool server read as environment variables, passed on only when set.
  agent_settings = { for name, value in {
    ROUTER_MODEL    = var.router_model
    SIMPLE_MODEL    = var.simple_model
    COMPLEX_MODEL   = var.complex_model
    GEMINI_LOCATION = var.gemini_location
    MEMORY_LOCATION = var.memory_location
  } : name => value if value != null }
}
