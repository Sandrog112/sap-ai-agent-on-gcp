"""Agent Runtime: the agent's source code is deployed directly, and the platform installs its locked dependencies."""

import contextlib

from infra.config import PYTHON_VERSION, RUNTIME_NAME
from infra.gcp import REQUIREMENTS_FILE, agent_platform, build_package, region, wait_gone

PACKAGES = ["agent", "shared"]
# The operations the runtime exposes; agent/main.py implements them.
CLASS_METHODS = [
    {
        "name": "async_query",
        "api_mode": "async",
        "description": "Answer one business partner message.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "actor_id": {"type": "string", "description": "The signed-in partner's id."},
                "session_id": {"type": "string", "description": "The conversation."},
                "operation_id": {"type": "string", "description": "The request; reuse it to retry the request."},
            },
            "required": ["prompt", "actor_id", "session_id", "operation_id"],
        },
    }
]


def deploy_runtime(service_account: str, environment: dict[str, str]) -> str:
    """Create or update the runtime; returns its resource name."""
    package_dir = build_package("runtime", PACKAGES, dependency_group="agent")
    settings = {
        "display_name": RUNTIME_NAME,
        "description": "Sales service agent for SAP-like sales data",
        "source_packages": [*PACKAGES, REQUIREMENTS_FILE],
        "entrypoint_module": "agent.main",
        "entrypoint_object": "root_agent",
        "requirements_file": REQUIREMENTS_FILE,
        "class_methods": CLASS_METHODS,
        "python_version": PYTHON_VERSION,
        "agent_framework": "custom",
        "service_account": service_account,
        "env_vars": environment,
        "min_instances": 0,  # no idle cost; the first request after a quiet period starts an instance
        "max_instances": 3,
        "resource_limits": {"cpu": "2", "memory": "2Gi"},
        "container_concurrency": 5,  # the recommended 2 * cpu + 1
    }
    runtimes = agent_platform(region()).runtimes
    existing = _find(runtimes)
    # The SDK reads the source packages relative to the working directory.
    with contextlib.chdir(package_dir):
        runtime = runtimes.update(name=existing, config=settings) if existing else runtimes.create(config=settings)
    return runtime.api_resource.name


def destroy_runtime() -> None:
    runtimes = agent_platform(region()).runtimes
    name = _find(runtimes)
    if name:
        runtimes.delete(name=name, force=True)
        wait_gone(lambda: _find(runtimes))  # its connections must close before its database can be deleted


def _find(runtimes) -> str | None:
    return next(
        (runtime.api_resource.name for runtime in runtimes.list() if runtime.api_resource.display_name == RUNTIME_NAME),
        None,
    )
