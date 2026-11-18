"""The deployment code that can be checked without a Google Cloud project: the router's secret (infra/secrets.py)."""

import base64
from types import SimpleNamespace
from typing import Any

import pytest
from googleapiclient.errors import HttpError

from infra import secrets
from infra.config import GEMINI_API_KEY_SECRET


class SecretManager:
    """Records the calls deploy_gemini_secret makes to the Secret Manager API, and plays one secret with `stored`
    versions (the values of its versions, oldest first)."""

    def __init__(self, stored: list[str]) -> None:
        self.stored = stored
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def projects(self) -> "SecretManager":
        return self

    def secrets(self) -> "SecretManager":
        return self

    def versions(self) -> "SecretManager":
        return self

    def _request(self, name: str, result: Any = None, **arguments: Any) -> SimpleNamespace:
        self.calls.append((name, arguments))
        return SimpleNamespace(execute=lambda num_retries=0: result)

    def create(self, **arguments: Any) -> SimpleNamespace:
        return self._request("create", {}, **arguments)

    def addVersion(self, **arguments: Any) -> SimpleNamespace:  # noqa: N802
        self.stored.append(base64.b64decode(arguments["body"]["payload"]["data"]).decode())
        return self._request("addVersion", {}, **arguments)

    def setIamPolicy(self, **arguments: Any) -> SimpleNamespace:  # noqa: N802
        return self._request("setIamPolicy", {}, **arguments)

    def access(self, **arguments: Any) -> SimpleNamespace:
        self.calls.append(("access", arguments))
        if not self.stored:
            missing = HttpError(SimpleNamespace(status=404, reason="Not Found"), b"{}")
            return SimpleNamespace(execute=lambda num_retries=0: (_ for _ in ()).throw(missing))
        data = base64.b64encode(self.stored[-1].encode()).decode()
        return SimpleNamespace(execute=lambda num_retries=0: {"payload": {"data": data}})

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def secret_manager(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "my-project")
    monkeypatch.setenv("GEMINI_API_KEY", "key-1")

    def use(stored: list[str]) -> SecretManager:
        manager = SecretManager(stored)
        monkeypatch.setattr(secrets, "client", lambda service, version: manager)
        return manager

    return use


def test_key_is_stored_and_only_the_runtime_may_read_it(secret_manager):
    manager = secret_manager([])

    secrets.deploy_gemini_secret()

    assert manager.stored == ["key-1"]
    name = f"projects/my-project/secrets/{GEMINI_API_KEY_SECRET}"
    policy = dict(manager.calls)["setIamPolicy"]
    assert policy["resource"] == name
    assert policy["body"]["policy"]["bindings"] == [
        {
            "role": "roles/secretmanager.secretAccessor",
            "members": ["serviceAccount:sap-agent-runtime@my-project.iam.gserviceaccount.com"],
        }
    ]


def test_redeploying_with_the_same_key_adds_no_new_version(secret_manager):
    manager = secret_manager(["key-1"])

    secrets.deploy_gemini_secret()

    assert "addVersion" not in manager.names()
    assert manager.stored == ["key-1"]


def test_a_changed_key_becomes_the_latest_version(secret_manager):
    manager = secret_manager(["old-key"])

    secrets.deploy_gemini_secret()

    assert manager.stored == ["old-key", "key-1"]


def test_deploy_needs_the_key_in_the_environment(secret_manager, monkeypatch):
    secret_manager([])
    monkeypatch.delenv("GEMINI_API_KEY")

    with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
        secrets.deploy_gemini_secret()
