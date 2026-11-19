# Memory Bank: long-term partner preferences, extracted from conversations and isolated per partner.
#
# No Terraform resource creates one, and the service assigns its id, so provisioning/memory_bank.py does, and the
# `external` data source reads the name back. The bank is only created, never changed or replaced by an edit of the
# script: a replacement would delete the partners' memories. It is deleted when the stack is destroyed.

locals {
  memory_bank_environment = merge(
    { for name in ["MEMORY_LOCATION", "SIMPLE_MODEL"] : name => local.agent_settings[name] if contains(keys(local.agent_settings), name) },
    {
      GCP_PROJECT_ID           = var.project_id
      MEMORY_BANK_DISPLAY_NAME = local.memory_bank_name
    },
  )
}

resource "terraform_data" "memory_bank" {
  input = local.memory_bank_environment

  provisioner "local-exec" {
    working_dir = local.repository_root
    command     = "uv run --quiet python -m provisioning.memory_bank ensure"
    environment = local.memory_bank_environment
  }

  provisioner "local-exec" {
    when        = destroy
    working_dir = abspath("${path.module}/..")
    command     = "uv run --quiet python -m provisioning.memory_bank delete"
    environment = self.input
  }

  depends_on = [google_project_service.required]
}

# Deferred to apply on the first run, when the bank does not exist yet.
data "external" "memory_bank" {
  program     = ["uv", "run", "--quiet", "python", "-m", "provisioning.memory_bank", "lookup"]
  working_dir = local.repository_root

  query = {}

  depends_on = [terraform_data.memory_bank]
}
