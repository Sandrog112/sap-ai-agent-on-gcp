# The router's Gemini API key, the one credential in the stack. Gemini Decision 1 is only served by the Gemini Developer
# API (Google AI Studio), which takes an API key where every other call of the stack uses a service account. Only the
# runtime's service account may read it.

resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = local.gemini_secret_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "gemini_api_key" {
  secret                 = google_secret_manager_secret.gemini_api_key.id
  secret_data_wo         = var.gemini_api_key
  secret_data_wo_version = var.gemini_api_key_version
}

resource "google_secret_manager_secret_iam_member" "runtime_reads_gemini_api_key" {
  secret_id = google_secret_manager_secret.gemini_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.this["runtime"].email}"
}
