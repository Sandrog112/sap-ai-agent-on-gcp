"""Names, paths and limits of the stack's resources."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"
OUTPUTS_FILE = ROOT / "deploy-outputs.json"

PREFIX = "sap-agent"
TOOL_SERVICE = f"{PREFIX}-tools"
RUNTIME_NAME = f"{PREFIX}-runtime"
MEMORY_BANK_NAME = f"{PREFIX}-memory"
# One identity per service, each allowed only what that service needs; see infra/iam.py and infra/data_store.py.
SERVICE_ACCOUNTS = {"tools": f"{PREFIX}-tools", "runtime": f"{PREFIX}-runtime", "build": f"{PREFIX}-build"}
CHECKPOINT_DATABASE = "agent_state"  # conversation state, apart from the sales data
INSTANCE_TIER = "db-custom-1-3840"  # only used when the Cloud SQL instance does not exist yet
PYTHON_VERSION = "3.13"
# USD: the most the agent may commit on its own per order, and per partner in 24 hours, see backend/policy.py. It
# leaves 83% of the dataset's orders within reach of the agent and the largest 17% to a person.
AUTO_APPROVAL_LIMIT = "100000.00"
BUSINESS_DATE = "2026-11-02"  # the dataset's "today", see data/processed/DATA_DICTIONARY.md

REQUIRED_APIS = (
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudtrace.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
)
