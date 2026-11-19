# Set these in terraform.tfvars (git-ignored), see terraform.tfvars.example. Secrets can come from the environment as
# TF_VAR_db_password and TF_VAR_gemini_api_key instead.

variable "project_id" {
  description = "The Google Cloud project, with billing enabled."
  type        = string
}

variable "region" {
  description = "Region of the Cloud SQL instance, the Cloud Run service, the Agent Runtime and the image registry."
  type        = string
}

variable "name_prefix" {
  description = "Prefix of the names of the stack's resources."
  type        = string
  default     = "sap-agent"
}

# ---- Cloud SQL ---------------------------------------------------------------------------------------------------

variable "create_sql_instance" {
  description = "Create the Cloud SQL instance. Set to false to use an existing one, which must be in `region` and have the database flag cloudsql.iam_authentication=on."
  type        = bool
  default     = true
}

variable "sql_instance_name" {
  type    = string
  default = "sap-sales"
}

variable "sql_tier" {
  description = "Machine type of a created instance; an existing one is left as it is."
  type        = string
  default     = "db-custom-1-3840"
}

variable "sql_deletion_protection" {
  description = "Stops `terraform destroy` from deleting a created instance, and with it the sales data. Set to false and apply before destroying."
  type        = bool
  default     = true
}

variable "db_name" {
  description = "The sales database."
  type        = string
  default     = "sap_sales"
}

variable "checkpoint_db_name" {
  description = "The agent's conversation state, apart from the sales data."
  type        = string
  default     = "agent_state"
}

variable "db_user" {
  description = "The database admin. Only Terraform's helper scripts use it, on this machine: the deployed services sign in with their service accounts."
  type        = string
  default     = "postgres"
}

variable "db_password" {
  description = "Password of the database admin; sets the password of a created instance."
  type        = string
  sensitive   = true
}

# ---- The router's API key ----------------------------------------------------------------------------------------

variable "gemini_api_key" {
  description = "Google AI Studio API key for the router, Gemini Decision 1 (https://aistudio.google.com/api-keys). Stored in Secret Manager; ephemeral and write-only, so it is not kept in the plan or the state."
  type        = string
  sensitive   = true
  ephemeral   = true
}

variable "gemini_api_key_version" {
  description = "Increase it to store the current gemini_api_key as a new version of the secret. Terraform cannot compare a write-only value."
  type        = number
  default     = 1
}

# ---- The agent and the tool server -------------------------------------------------------------------------------

variable "business_date" {
  description = "The dataset's \"today\", see data/processed/DATA_DICTIONARY.md. Keep it equal to BUSINESS_DATE in tests/conftest.py."
  type        = string
  default     = "2026-11-02"
}

variable "auto_approval_limit" {
  description = "USD the agent may commit on its own per order and per partner in 24 hours. Null keeps the default of backend/policy.py."
  type        = string
  default     = null
}

# Null keeps the defaults of agent/config.py.
variable "router_model" {
  type    = string
  default = null
}

variable "simple_model" {
  type    = string
  default = null
}

variable "complex_model" {
  type    = string
  default = null
}

variable "gemini_location" {
  type    = string
  default = null
}

variable "memory_location" {
  type    = string
  default = null
}

variable "python_version" {
  type    = string
  default = "3.13"
}

variable "buildpacks_builder" {
  description = "The Cloud Native Buildpacks builder that turns the tool server's source into an image; there is no Dockerfile."
  type        = string
  default     = "gcr.io/buildpacks/builder:google-24"
}

variable "tool_server_invokers" {
  description = "IAM members besides the runtime and the signed-in operator that may call the tool server, for example user:alice@example.com."
  type        = list(string)
  default     = []
}
