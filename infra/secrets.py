"""Secret Manager: the Gemini API key of the router, the one credential in the stack.

Gemini Decision 1 is only served by the Gemini Developer API (Google AI Studio), which takes an API key where every
other call of the stack uses a service account. The key is read from .env on this machine, stored once as a secret, and
read back by the agent at runtime. Only the runtime's service account may read it.
"""

import base64

from googleapiclient.errors import HttpError

from infra.config import GEMINI_API_KEY_SECRET
from infra.gcp import client, create_unless_exists, execute, ignore_missing, project_id, service_account_email, setting

_MISSING = 404
_ACCESSOR = "roles/secretmanager.secretAccessor"


def deploy_gemini_secret() -> None:
    """Create the secret, store the key from GEMINI_API_KEY as its latest version unless it already is, and allow the
    runtime to read it."""
    secrets = client("secretmanager", "v1").projects().secrets()
    create_unless_exists(
        secrets.create(
            parent=f"projects/{project_id()}",
            secretId=GEMINI_API_KEY_SECRET,
            body={"replication": {"automatic": {}}},
        )
    )
    key = setting("GEMINI_API_KEY")
    if _latest_key() != key:
        payload = {"data": base64.b64encode(key.encode()).decode()}
        execute(secrets.addVersion(parent=_secret_name(), body={"payload": payload}))
    policy = {"bindings": [{"role": _ACCESSOR, "members": [f"serviceAccount:{service_account_email('runtime')}"]}]}
    execute(secrets.setIamPolicy(resource=_secret_name(), body={"policy": policy}))


def destroy_gemini_secret() -> None:
    ignore_missing(client("secretmanager", "v1").projects().secrets().delete(name=_secret_name()))


def _latest_key() -> str | None:
    versions = client("secretmanager", "v1").projects().secrets().versions()
    try:
        data = execute(versions.access(name=f"{_secret_name()}/versions/latest"))["payload"]["data"]
    except HttpError as error:
        if error.resp.status != _MISSING:  # no version yet
            raise
        return None
    return base64.b64decode(data).decode()


def _secret_name() -> str:
    return f"projects/{project_id()}/secrets/{GEMINI_API_KEY_SECRET}"
