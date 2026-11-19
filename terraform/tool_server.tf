# The MCP tool server on Cloud Run, reachable only by the identities allowed to invoke it.
#
# There is no Dockerfile: Cloud Build builds the source with Google Cloud's buildpacks, which install requirements.txt
# on a Google-maintained Python base image and start GOOGLE_ENTRYPOINT as a non-root user. The build runs as its own
# service account, and when the source changes, the image gets a new tag and the service a new revision.

resource "google_artifact_registry_repository" "images" {
  repository_id = local.repository_id
  location      = var.region
  format        = "DOCKER"
  description   = "Images of the SAP agent stack"

  depends_on = [google_project_service.required]
}

# Where `gcloud builds submit` stages the source, readable by the build service account only.
resource "google_storage_bucket" "build_source" {
  name                        = local.build_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true

  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "build_reads_source" {
  bucket = google_storage_bucket.build_source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.this["build"].email}"
}

resource "google_artifact_registry_repository_iam_member" "build_pushes_images" {
  repository = google_artifact_registry_repository.images.name
  location   = google_artifact_registry_repository.images.location
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.this["build"].email}"
}

locals {
  tool_server_image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/tool-server:${substr(data.external.tool_server_source.result.sha256, 0, 16)}"
}

resource "terraform_data" "tool_server_image" {
  triggers_replace = [local.tool_server_image]

  provisioner "local-exec" {
    working_dir = local.repository_root
    command     = <<-EOT
      gcloud builds submit "$SOURCE_DIRECTORY" \
        --project "$PROJECT_ID" --region "$REGION" --quiet \
        --config terraform/cloudbuild/tool-server.yaml \
        --service-account "projects/$PROJECT_ID/serviceAccounts/$BUILD_SERVICE_ACCOUNT" \
        --gcs-source-staging-dir "$STAGING_DIRECTORY" \
        --substitutions "_IMAGE=$IMAGE,_BUILDER=$BUILDER,_PYTHON_VERSION=$PYTHON_VERSION,_ENTRYPOINT=$ENTRYPOINT"
    EOT
    environment = {
      SOURCE_DIRECTORY      = data.external.tool_server_source.result.directory
      PROJECT_ID            = var.project_id
      REGION                = var.region
      BUILD_SERVICE_ACCOUNT = google_service_account.this["build"].email
      STAGING_DIRECTORY     = "gs://${google_storage_bucket.build_source.name}/source"
      IMAGE                 = local.tool_server_image
      BUILDER               = var.buildpacks_builder
      PYTHON_VERSION        = "${var.python_version}.x"
      ENTRYPOINT            = local.tool_server_entry
    }
  }

  depends_on = [
    google_project_iam_member.this,
    google_storage_bucket_iam_member.build_reads_source,
    google_artifact_registry_repository_iam_member.build_pushes_images,
  ]
}

resource "google_cloud_run_v2_service" "tools" {
  name     = local.tool_service_name
  location = var.region

  # `terraform destroy` removes the service; the default would refuse to.
  deletion_protection = false

  template {
    service_account                  = google_service_account.this["tools"].email
    timeout                          = "60s"
    max_instance_request_concurrency = local.tool_server_scaling.concurrency

    scaling {
      min_instance_count = local.tool_server_scaling.min
      max_instance_count = local.tool_server_scaling.max
    }

    containers {
      image = local.tool_server_image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      dynamic "env" {
        for_each = merge(
          {
            GCP_PROJECT_ID       = var.project_id
            CLOUD_SQL_CONNECTION = local.sql_connection_name
            DB_NAME              = google_sql_database.sales.name
            DB_IAM_USER          = local.database_users["tools"]
            BUSINESS_DATE        = var.business_date
            TELEMETRY_ENABLED    = "true"
          },
          var.auto_approval_limit == null ? {} : { AUTO_APPROVAL_LIMIT = var.auto_approval_limit },
        )
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  depends_on = [
    terraform_data.tool_server_image,
    terraform_data.database_access, # its tables and grants exist before the first request
    google_project_iam_member.this,
  ]
}

# Only these identities may call the service; everyone else is refused before reaching its code.
resource "google_cloud_run_v2_service_iam_binding" "invokers" {
  name     = google_cloud_run_v2_service.tools.name
  location = google_cloud_run_v2_service.tools.location
  role     = "roles/run.invoker"
  members = distinct(concat(
    ["serviceAccount:${google_service_account.this["runtime"].email}"],
    local.operator_account == "" ? [] : [local.operator_member],
    var.tool_server_invokers,
  ))
}
