"""The MCP tool server on Cloud Run: deployed from source, reachable only by the identities allowed to invoke it."""

import os

from infra.config import AUTO_APPROVAL_LIMIT, BUSINESS_DATE, PYTHON_VERSION, TOOL_SERVICE
from infra.gcp import (
    build_package,
    client,
    database_user,
    execute,
    ignore_missing,
    instance_connection_name,
    project_id,
    region,
    run_gcloud,
    service_account_email,
    setting,
)
from infra.iam import grant_invoker

# There is no Dockerfile: Cloud Run builds the source with Google Cloud's buildpacks, which install requirements.txt
# on a Google-maintained Python base image and start this command as a non-root user.
BUILD_SETTINGS = {"GOOGLE_PYTHON_VERSION": f"{PYTHON_VERSION}.x", "GOOGLE_ENTRYPOINT": "python -m backend.server"}


def deploy_tool_server(operator: str) -> str:
    """Deploy the server, and let only the runtime and the deploying operator call it. Returns its MCP URL."""
    source = build_package("tools", ["backend", "shared"], dependency_group="tools")
    environment = {
        "GCP_PROJECT_ID": project_id(),
        "CLOUD_SQL_CONNECTION": instance_connection_name(),
        "DB_NAME": setting("DB_NAME"),
        "DB_IAM_USER": database_user("tools"),
        "AUTO_APPROVAL_LIMIT": AUTO_APPROVAL_LIMIT,
        "BUSINESS_DATE": BUSINESS_DATE,
        "FAULT_INJECTION": os.getenv("FAULT_INJECTION") or "false",
        "TELEMETRY_ENABLED": "true",
    }
    run_gcloud(
        "run",
        "deploy",
        TOOL_SERVICE,
        f"--source={source}",
        f"--region={region()}",
        f"--service-account={service_account_email('tools')}",
        f"--build-service-account=projects/{project_id()}/serviceAccounts/{service_account_email('build')}",
        f"--set-build-env-vars={_pairs(BUILD_SETTINGS)}",
        f"--set-env-vars={_pairs(environment)}",
        "--no-allow-unauthenticated",
        "--cpu=1",
        "--memory=512Mi",
        "--min-instances=0",
        "--max-instances=3",
        "--concurrency=20",
        "--timeout=60",
    )
    grant_invoker(_service_name(), [f"serviceAccount:{service_account_email('runtime')}", operator])
    service = execute(client("run", "v2").projects().locations().services().get(name=_service_name()))
    return f"{service['uri']}/mcp"


def destroy_tool_server() -> None:
    ignore_missing(client("run", "v2").projects().locations().services().delete(name=_service_name()))


def _pairs(values: dict[str, str]) -> str:
    return ",".join(f"{name}={value}" for name, value in values.items())


def _service_name() -> str:
    return f"projects/{project_id()}/locations/{region()}/services/{TOOL_SERVICE}"
