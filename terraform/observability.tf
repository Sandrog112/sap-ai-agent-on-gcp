# Cloud Monitoring metrics from the services' structured logs. Traces and logs need no setup of their own: the services
# export spans to Cloud Trace themselves (shared/telemetry.py), and Cloud Logging collects their stdout.
#
# They answer the questions a trace cannot: how the traffic splits across model tiers, and how often tools fail or the
# policy says no (outcome="POLICY_DENIED"). Each is a count of one log line, with some of that line's fields as labels.

locals {
  log_metrics = {
    "sap_agent/tool_calls" = {
      description = "Tool calls handled by the tool server, by tool and outcome (OK or an error code)."
      filter      = "jsonPayload.message=\"tool_call\""
      labels      = { tool = "jsonPayload.tool", outcome = "jsonPayload.outcome" }
    }
    "sap_agent/model_routes" = {
      description = "Customer messages routed to each model tier."
      filter      = "jsonPayload.message=\"model_route\""
      labels      = { tier = "jsonPayload.tier" }
    }
  }
}

resource "google_logging_metric" "this" {
  for_each = local.log_metrics

  name        = each.key
  description = each.value.description
  filter      = each.value.filter

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"

    dynamic "labels" {
      for_each = each.value.labels
      content {
        key        = labels.key
        value_type = "STRING"
      }
    }
  }

  label_extractors = { for label, field in each.value.labels : label => "EXTRACT(${field})" }

  depends_on = [google_project_service.required]
}
