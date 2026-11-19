# One service account per service, each allowed only what that service needs. None of them reaches the sales data
# through IAM: only the tool server's database grants do, see provisioning/database.py.

locals {
  service_accounts = {
    tools   = "${var.name_prefix}-tools"
    runtime = "${var.name_prefix}-runtime"
    build   = "${var.name_prefix}-build"
  }

  project_roles = {
    tools = [
      "roles/cloudsql.client",       # connect to the instance
      "roles/cloudsql.instanceUser", # sign in to the database with IAM instead of a password
      "roles/cloudtrace.agent",
      "roles/logging.logWriter",
    ]
    runtime = [
      "roles/aiplatform.user",       # call Gemini
      "roles/aiplatform.memoryUser", # read and write Memory Bank
      "roles/cloudsql.client",
      "roles/cloudsql.instanceUser",
      "roles/cloudtrace.agent",
      "roles/logging.logWriter",
      "roles/serviceusage.serviceUsageConsumer",
    ]
    # Builds the tool server's image: writes its logs, and (below) its source and its image registry.
    build = ["roles/logging.logWriter"]
  }

  project_role_bindings = merge([
    for kind, roles in local.project_roles : {
      for role in roles : "${kind}/${role}" => { kind = kind, role = role }
    }
  ]...)
}

resource "google_service_account" "this" {
  for_each = local.service_accounts

  account_id   = each.value
  display_name = "SAP agent: ${each.key}"

  depends_on = [google_project_service.required]
}

# Adds the members to the project's policy and leaves every other principal's bindings alone.
resource "google_project_iam_member" "this" {
  for_each = local.project_role_bindings

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.this[each.value.kind].email}"
}

# The account gcloud is signed in with, which Terraform also uses to build the tool server's image, may call the tool
# server directly with `cli.py call-tool`.
data "external" "operator" {
  program = ["sh", "-c", "printf '{\"account\": \"%s\"}' \"$(gcloud config get-value account 2>/dev/null)\""]
}

locals {
  operator_account = data.external.operator.result.account
  operator_member  = "${endswith(local.operator_account, ".gserviceaccount.com") ? "serviceAccount" : "user"}:${local.operator_account}"
}
