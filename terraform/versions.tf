terraform {
  # 1.11 for write-only attributes: the router's API key never reaches the state.
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.12"
    }
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  # Bills API calls to the project instead of the credentials' own, which several admin APIs insist on when you sign
  # in with `gcloud auth application-default login`.
  user_project_override = true
  billing_project       = var.project_id
}
