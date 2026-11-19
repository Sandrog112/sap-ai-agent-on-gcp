# What is inside the databases, by provisioning/database.py: the sales data (loaded once, into an empty database), the
# agent's ledger, the conversation tables, and exactly what each service's database user may do with them. They stay
# in Python because tests/conftest.py runs the tool server's tests under these very grants, and because they belong
# to the schema in backend/store.py.

locals {
  database_environment = {
    GCP_PROJECT_ID     = var.project_id
    GCP_REGION         = var.region
    CLOUD_SQL_INSTANCE = local.sql_instance_name
    DB_NAME            = google_sql_database.sales.name
    CHECKPOINT_DB_NAME = google_sql_database.checkpoints.name
    DB_USER            = var.db_user
    DB_PASSWORD        = var.db_password
    TOOLS_DB_USER      = local.database_users["tools"]
    RUNTIME_DB_USER    = local.database_users["runtime"]
  }
}

resource "terraform_data" "database_access" {
  # Runs again when the grants or the schema change; it is idempotent, and nothing here replaces business data.
  triggers_replace = [
    filesha256("${local.repository_root}/provisioning/database.py"),
    filesha256("${local.repository_root}/backend/store.py"),
    local.database_environment.DB_NAME,
    local.database_environment.CHECKPOINT_DB_NAME,
    local.database_environment.TOOLS_DB_USER,
    local.database_environment.RUNTIME_DB_USER,
  ]

  provisioner "local-exec" {
    working_dir = local.repository_root
    command     = "uv run --quiet python -m provisioning.database apply"
    environment = local.database_environment
  }

  depends_on = [google_sql_user.iam, google_sql_database.sales, google_sql_database.checkpoints]
}

# Does nothing until the stack is destroyed. Then it runs after the runtime is gone and before the users and the
# conversation database are deleted, which need the ledger and the grants removed first. It is apart from the resource
# above so that changing the grants never drops the ledger.
resource "terraform_data" "database_teardown" {
  input = local.database_environment

  provisioner "local-exec" {
    when        = destroy
    working_dir = abspath("${path.module}/..")
    command     = "uv run --quiet python -m provisioning.database teardown"
    environment = self.input
  }

  depends_on = [google_sql_user.iam, google_sql_database.sales, google_sql_database.checkpoints]
}
