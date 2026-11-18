"""Google API clients and the small helpers shared by the deployment steps."""

import functools
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import agentplatform
import google.auth
from googleapiclient import discovery
from googleapiclient.errors import HttpError

from infra.config import BUILD_DIR, REQUIRED_APIS, ROOT, SERVICE_ACCOUNTS

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
REQUIREMENTS_FILE = "requirements.txt"
_ALREADY_EXISTS = 409
_MISSING = 404


def setting(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Set {name}, see .env.example.")
    return value


def project_id() -> str:
    return setting("GCP_PROJECT_ID")


def region() -> str:
    return setting("GCP_REGION")


def instance_connection_name() -> str:
    return f"{project_id()}:{region()}:{setting('CLOUD_SQL_INSTANCE')}"


def service_account_email(kind: str) -> str:
    return f"{SERVICE_ACCOUNTS[kind]}@{project_id()}.iam.gserviceaccount.com"


def database_user(kind: str) -> str:
    """The Cloud SQL user of a service account: its email without the domain's suffix."""
    return service_account_email(kind).removesuffix(".gserviceaccount.com")


@functools.cache
def _credentials() -> Any:
    # Without a quota project, several admin APIs refuse the credentials of `gcloud auth application-default login`.
    credentials, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE], quota_project_id=project_id())
    return credentials


@functools.cache
def client(service: str, version: str) -> Any:
    return discovery.build(service, version, credentials=_credentials(), cache_discovery=False)


@functools.cache
def agent_platform(location: str) -> agentplatform.Client:
    return agentplatform.Client(project=project_id(), location=location)


def execute(request: Any, retries: int = 5) -> dict:
    """Run an API request, retrying throttling and transient server errors."""
    return request.execute(num_retries=retries)


def enable_apis() -> None:
    service_usage = client("serviceusage", "v1")
    operation = execute(
        service_usage.services().batchEnable(
            parent=f"projects/{project_id()}", body={"serviceIds": list(REQUIRED_APIS)}
        )
    )
    wait_for(lambda: execute(service_usage.operations().get(name=operation["name"])), lambda state: state.get("done"))


def wait_for(describe: Callable[[], dict], is_done: Callable[[dict], Any], timeout: int = 1200) -> dict:
    """Poll a resource or operation until it is done, failing fast if it reports an error."""
    deadline = time.monotonic() + timeout
    while True:
        state = describe()
        if state.get("error"):
            raise RuntimeError(f"Operation failed: {state['error']}")
        if is_done(state):
            return state
        if time.monotonic() > deadline:
            raise TimeoutError(f"Timed out waiting for {state.get('name') or 'an operation'}.")
        time.sleep(5)


def wait_gone(probe: Callable[[], Any], timeout: int = 600) -> None:
    deadline = time.monotonic() + timeout
    while probe():
        if time.monotonic() > deadline:
            raise TimeoutError("Timed out waiting for deletion.")
        time.sleep(5)


def ignore_missing(request: Any) -> None:
    try:
        execute(request)
    except HttpError as error:
        if error.resp.status != _MISSING:
            raise


def create_unless_exists(request: Any) -> dict | None:
    """Run a create request; returns None if the resource already exists."""
    try:
        return execute(request)
    except HttpError as error:
        if error.resp.status != _ALREADY_EXISTS:
            raise
        return None


def run_gcloud(*arguments: str) -> str:
    """gcloud for the one thing the APIs make long-winded: building and deploying a service from source."""
    command = ["gcloud", *arguments, f"--project={project_id()}", "--quiet"]
    return subprocess.run(command, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()


def build_package(name: str, packages: Iterable[str], dependency_group: str) -> Path:
    """Assemble what one service is deployed from under build/<name>: its packages and a requirements.txt with the
    locked shared dependencies plus its own group. Nothing else in the repository (.env above all) is ever uploaded.
    """
    if shutil.which("uv") is None:
        sys.exit("uv is required to export the locked requirements: https://docs.astral.sh/uv/")
    package_dir = BUILD_DIR / name
    shutil.rmtree(package_dir, ignore_errors=True)
    package_dir.mkdir(parents=True)
    for package in packages:
        shutil.copytree(ROOT / package, package_dir / package, ignore=shutil.ignore_patterns("__pycache__"))
    export = ["uv", "export", "--frozen", "--no-default-groups", "--group", dependency_group]
    export += ["--no-hashes", "--no-emit-project", "-o", str(package_dir / REQUIREMENTS_FILE)]
    subprocess.run(export, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    return package_dir


def step(message: str) -> None:
    print(f"==> {message}", flush=True)
