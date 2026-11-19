# Agent Runtime: the agent's source code is deployed directly, and the platform installs its locked dependencies.

locals {
  runtime_environment = merge(
    {
      GCP_PROJECT_ID        = var.project_id
      TOOL_SERVER_URL       = "${google_cloud_run_v2_service.tools.uri}/mcp"
      MEMORY_BANK_NAME      = data.external.memory_bank.result.name
      GEMINI_API_KEY_SECRET = google_secret_manager_secret.gemini_api_key.secret_id
      CLOUD_SQL_CONNECTION  = local.sql_connection_name
      CHECKPOINT_DB_NAME    = google_sql_database.checkpoints.name
      DB_IAM_USER           = local.database_users["runtime"]
      BUSINESS_DATE         = var.business_date
      TELEMETRY_ENABLED     = "true"
    },
    local.agent_settings,
  )

  # The operations the runtime exposes; agent/main.py implements them.
  runtime_class_methods = [
    {
      name        = "async_query"
      api_mode    = "async"
      description = "Answer one business partner message."
      parameters = {
        type = "object"
        properties = {
          prompt       = { type = "string" }
          actor_id     = { type = "string", description = "The signed-in partner's id." }
          session_id   = { type = "string", description = "The conversation." }
          operation_id = { type = "string", description = "The request; reuse it to retry the request." }
        }
        required = ["prompt", "actor_id", "session_id", "operation_id"]
      }
    }
  ]
}

resource "google_vertex_ai_reasoning_engine" "agent" {
  display_name = local.runtime_name
  description  = local.runtime_description
  region       = var.region

  # Deleting it must not wait for its sessions to be removed first.
  deletion_policy = "FORCE"

  spec {
    agent_framework = "custom"
    service_account = google_service_account.this["runtime"].email
    class_methods   = jsonencode(local.runtime_class_methods)

    source_code_spec {
      inline_source {
        source_archive = filebase64(data.external.runtime_source.result.archive)
      }

      python_spec {
        entrypoint_module = "agent.main"
        entrypoint_object = "root_agent"
        requirements_file = "requirements.txt"
        version           = var.python_version
      }
    }

    deployment_spec {
      min_instances         = local.runtime_scaling.min # no idle cost; the first request after a quiet period starts an instance
      max_instances         = local.runtime_scaling.max
      container_concurrency = local.runtime_scaling.concurrency
      resource_limits       = { cpu = "2", memory = "2Gi" }

      dynamic "env" {
        for_each = local.runtime_environment
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  timeouts {
    create = "30m"
    update = "30m"
    delete = "30m"
  }

  lifecycle {
    precondition {
      condition     = data.external.memory_bank.result.name != ""
      error_message = "Memory Bank was not found: see the output of provisioning/memory_bank.py."
    }
  }

  depends_on = [
    google_project_iam_member.this,
    google_secret_manager_secret_version.gemini_api_key,
    google_secret_manager_secret_iam_member.runtime_reads_gemini_api_key,
    google_cloud_run_v2_service_iam_binding.invokers,
    terraform_data.database_access,
    terraform_data.database_teardown, # destroyed after the runtime, so its connections are closed first
  ]
}
