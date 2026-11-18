"""Service accounts of the stack: one per service, allowed only what that service needs."""

import time
from collections.abc import Callable

from googleapiclient.errors import HttpError

from infra.config import SERVICE_ACCOUNTS
from infra.gcp import client, create_unless_exists, execute, ignore_missing, project_id, service_account_email

# None of these reach the sales data: only the tool server's database grants do, see infra/data_store.py.
PROJECT_ROLES = {
    "tools": (
        "roles/cloudsql.client",  # connect to the instance
        "roles/cloudsql.instanceUser",  # sign in to the database with IAM instead of a password
        "roles/cloudtrace.agent",
        "roles/logging.logWriter",
    ),
    "runtime": (
        "roles/aiplatform.user",  # call Gemini
        "roles/aiplatform.memoryUser",  # read and write Memory Bank
        "roles/cloudsql.client",
        "roles/cloudsql.instanceUser",
        "roles/cloudtrace.agent",
        "roles/logging.logWriter",
        "roles/serviceusage.serviceUsageConsumer",
    ),
    # Builds the tool server's image, instead of relying on a project's default build identity.
    "build": ("roles/run.builder",),
}
_CONFLICT = 409
_INVALID = 400  # also returned for a new service account that has not propagated yet


def deploy_service_accounts() -> None:
    iam = client("iam", "v1")
    for kind, account_id in SERVICE_ACCOUNTS.items():
        create_unless_exists(
            iam.projects()
            .serviceAccounts()
            .create(
                name=f"projects/{project_id()}",
                body={"accountId": account_id, "serviceAccount": {"displayName": f"SAP agent: {kind}"}},
            )
        )
    _update_project_policy(_add_project_roles)


def destroy_service_accounts() -> None:
    _update_project_policy(_remove_project_roles)
    iam = client("iam", "v1")
    for kind in SERVICE_ACCOUNTS:
        name = f"projects/{project_id()}/serviceAccounts/{service_account_email(kind)}"
        ignore_missing(iam.projects().serviceAccounts().delete(name=name))


def grant_invoker(service_name: str, members: list[str]) -> None:
    """Let exactly these identities call a Cloud Run service; everyone else is refused before reaching its code."""
    services = client("run", "v2").projects().locations().services()
    policy = {"bindings": [{"role": "roles/run.invoker", "members": members}]}
    execute(services.setIamPolicy(resource=service_name, body={"policy": policy}))


def _members_by_role() -> dict[str, set[str]]:
    members: dict[str, set[str]] = {}
    for kind, roles in PROJECT_ROLES.items():
        for role in roles:
            members.setdefault(role, set()).add(f"serviceAccount:{service_account_email(kind)}")
    return members


def _add_project_roles(policy: dict) -> None:
    bindings = {binding["role"]: binding for binding in policy["bindings"] if "condition" not in binding}
    for role, members in _members_by_role().items():
        binding = bindings.get(role)
        if binding is None:
            policy["bindings"].append({"role": role, "members": sorted(members)})
        else:
            binding["members"] = sorted({*binding["members"], *members})


def _remove_project_roles(policy: dict) -> None:
    for binding in policy["bindings"]:
        ours = _members_by_role().get(binding["role"], set())
        binding["members"] = [member for member in binding["members"] if member not in ours]
    policy["bindings"] = [binding for binding in policy["bindings"] if binding["members"]]


def _update_project_policy(change: Callable[[dict], None], attempts: int = 8) -> None:
    """Read, change and write the project's IAM policy, leaving other principals' bindings alone. The etag makes a
    concurrent change fail instead of being overwritten, and a new service account can take a moment to propagate."""
    projects = client("cloudresourcemanager", "v1").projects()
    for attempt in range(attempts):
        options = {"options": {"requestedPolicyVersion": 3}}
        policy = execute(projects.getIamPolicy(resource=project_id(), body=options))
        policy.setdefault("bindings", [])
        change(policy)
        try:
            execute(projects.setIamPolicy(resource=project_id(), body={"policy": policy}))
            return
        except HttpError as error:
            if error.resp.status not in (_CONFLICT, _INVALID) or attempt == attempts - 1:
                raise
            time.sleep(5)
