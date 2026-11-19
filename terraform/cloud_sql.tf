# Cloud SQL for PostgreSQL: the instance, its two databases and the services' IAM database users. What is inside the
# databases (the ledger, the data and the grants) is database.tf.

resource "google_sql_database_instance" "main" {
  count = var.create_sql_instance ? 1 : 0

  name             = var.sql_instance_name
  region           = var.region
  database_version = "POSTGRES_18"
  root_password    = var.db_password

  # The sales data lives here, and a deleted instance cannot be recovered.
  deletion_protection = var.sql_deletion_protection

  settings {
    tier    = var.sql_tier
    edition = "ENTERPRISE"

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    ip_configuration {
      ssl_mode = "ENCRYPTED_ONLY"
    }
  }

  depends_on = [google_project_service.required]
}

# An existing instance is used as it is.
data "google_sql_database_instance" "existing" {
  count = var.create_sql_instance ? 0 : 1

  name = var.sql_instance_name

  lifecycle {
    postcondition {
      condition     = self.region == var.region
      error_message = "Instance ${var.sql_instance_name} is in ${self.region}, but region is ${var.region}."
    }
    postcondition {
      condition = anytrue([
        for flag in self.settings[0].database_flags : flag.name == "cloudsql.iam_authentication" && flag.value == "on"
      ])
      error_message = "Set the database flag cloudsql.iam_authentication=on on instance ${var.sql_instance_name}; changing flags restarts it."
    }
  }

  depends_on = [google_project_service.required]
}

# The Cloud SQL user of a service account is its email without the domain's suffix.
locals {
  database_users = {
    for kind in ["tools", "runtime"] : kind => trimsuffix(google_service_account.this[kind].email, ".gserviceaccount.com")
  }
}

resource "google_sql_user" "iam" {
  for_each = local.database_users

  instance = local.sql_instance_name
  name     = each.value
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"

  depends_on = [google_project_iam_member.this]
}

# The sales data. Destroying the stack must not delete it, including the changes the agent made to it, so Terraform
# only forgets the database. If it exists already, `terraform import` it, see the README.
resource "google_sql_database" "sales" {
  instance = local.sql_instance_name
  name     = var.db_name

  deletion_policy = "ABANDON"
}

# The conversation state of the agent. It goes away with the stack, after the users, whose grants must go first.
resource "google_sql_database" "checkpoints" {
  instance = local.sql_instance_name
  name     = var.checkpoint_db_name

  depends_on = [google_sql_user.iam]
}
