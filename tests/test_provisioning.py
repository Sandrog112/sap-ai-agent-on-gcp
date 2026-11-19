"""The Python that Terraform calls (provisioning/), and the one value Terraform and the tests must agree on."""

import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pg8000.dbapi
import pytest
from conftest import BUSINESS_DATE, DATABASE, TOOLS_ROLE

from backend.store import OPERATIONS_TABLE, ORDER_ID_SEQUENCE
from provisioning import database as db_setup
from provisioning import memory_bank, package_source

ROOT = Path(__file__).resolve().parent.parent


def tar_names(path: Path) -> list[str]:
    with tarfile.open(path) as tar:
        return tar.getnames()


@pytest.fixture
def build_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(package_source, "BUILD_DIR", tmp_path)
    return tmp_path


def test_a_service_is_assembled_with_its_own_locked_dependencies(build_dir):
    directory = package_source.assemble("tools", ["backend", "shared"], "tools")

    assert sorted(path.name for path in directory.iterdir()) == ["backend", "requirements.txt", "shared"]
    requirements = (directory / "requirements.txt").read_text()
    assert re.search(r"^asyncpg==", requirements, re.MULTILINE)
    assert "langgraph" not in requirements  # the agent's dependency, not the tool server's
    assert not list(directory.rglob("__pycache__"))


def test_the_archive_is_reproducible_and_has_the_files_at_its_root(build_dir):
    directory = package_source.assemble("runtime", ["agent", "shared"], "agent")

    first = package_source.archive(directory, build_dir / "first.tar.gz")
    (directory / "agent" / "__pycache__").mkdir()  # whatever the interpreter leaves around must not matter
    second = package_source.archive(directory, build_dir / "second.tar.gz")

    assert first == second
    names = tar_names(build_dir / "first.tar.gz")
    assert {"agent/main.py", "shared/telemetry.py", "requirements.txt"} <= set(names)
    assert "langgraph" in (directory / "requirements.txt").read_text()


def test_the_archive_changes_with_the_source(build_dir):
    directory = package_source.assemble("runtime", ["agent", "shared"], "agent")
    before = package_source.archive(directory, build_dir / "before.tar.gz")

    (directory / "agent" / "main.py").write_text("# changed\n")

    assert package_source.archive(directory, build_dir / "after.tar.gz") != before


def test_terraform_reads_the_packager_through_the_external_data_source(build_dir):
    """The program speaks JSON on stdin and stdout, and nothing else on stdout."""
    query = {"service": "tools", "packages": "backend,shared", "dependency_group": "tools"}

    result = subprocess.run(
        [sys.executable, "-m", "provisioning.package_source"],
        input=json.dumps(query),
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )

    output = json.loads(result.stdout)
    assert set(output) == {"directory", "archive", "sha256"}
    assert Path(output["archive"]).is_file()
    with tarfile.open(fileobj=io.BytesIO(Path(output["archive"]).read_bytes())) as tar:
        assert "backend/server.py" in tar.getnames()


def test_the_business_date_of_the_tests_is_the_one_terraform_deploys():
    variables = (ROOT / "terraform" / "variables.tf").read_text()
    default = re.search(r'variable "business_date" \{.*?default\s*=\s*"([^"]+)"', variables, re.DOTALL)

    assert default and default.group(1) == BUSINESS_DATE


class FakeMemoryBanks:
    def __init__(self, names: dict[str, str]) -> None:
        self.names = names

    def list(self):
        return [SimpleNamespace(name=name, display_name=display) for display, name in self.names.items()]


def test_memory_bank_is_found_by_its_display_name(monkeypatch):
    monkeypatch.setenv("MEMORY_BANK_DISPLAY_NAME", "mine")
    banks = FakeMemoryBanks({"other": "memoryBanks/1", "mine": "memoryBanks/2"})

    assert memory_bank.find(banks) == "memoryBanks/2"


def test_a_missing_memory_bank_is_none(monkeypatch):
    monkeypatch.setenv("MEMORY_BANK_DISPLAY_NAME", "mine")

    assert memory_bank.find(FakeMemoryBanks({"other": "memoryBanks/1"})) is None


def test_an_existing_memory_bank_is_never_recreated_or_changed(monkeypatch):
    monkeypatch.setenv("MEMORY_BANK_DISPLAY_NAME", "mine")
    banks = FakeMemoryBanks({"mine": "memoryBanks/2"})
    banks.create = banks.update = lambda **_: pytest.fail("a bank with memories must be left alone")
    monkeypatch.setattr(memory_bank, "_memory_banks", lambda: banks)

    assert memory_bank.ensure() == "memoryBanks/2"


def test_memory_bank_settings_must_come_from_terraform(monkeypatch):
    monkeypatch.delenv("MEMORY_BANK_DISPLAY_NAME", raising=False)

    with pytest.raises(SystemExit, match="MEMORY_BANK_DISPLAY_NAME"):
        memory_bank.find(FakeMemoryBanks({}))


def test_teardown_removes_the_ledger_and_every_grant_of_the_tool_server(database_server, database):
    """Runs against the template of the fixtures: the tool server's grants and the ledger, as `apply` leaves them."""
    parts = urlsplit(database_server)
    connection = pg8000.dbapi.connect(
        user=parts.username, password=parts.password, host=parts.hostname, port=parts.port or 5432, database=DATABASE
    )
    connection.autocommit = True
    cursor = connection.cursor()

    def scalar(sql: str):
        cursor.execute(sql)
        return cursor.fetchone()[0]

    can_read = f"SELECT has_table_privilege('{TOOLS_ROLE}', 'partners', 'SELECT')"
    assert scalar(can_read) is True
    assert scalar(f"SELECT to_regclass('{OPERATIONS_TABLE}')::text") is not None

    db_setup.remove_tool_server_access(connection, db_setup.quoted(TOOLS_ROLE))

    assert scalar(can_read) is False
    assert scalar(f"SELECT to_regclass('{OPERATIONS_TABLE}')::text") is None
    assert scalar(f"SELECT to_regclass('{ORDER_ID_SEQUENCE}')::text") is None
    assert scalar("SELECT count(*) FROM partners") > 0  # the sales data stays
    connection.close()


STATE_DATABASE = "sap_agent_test_state"
RUNTIME_ROLE = "sap_agent_test_runtime"


@pytest.fixture
def local_stack(database_server, database, monkeypatch):
    """What Terraform hands to `python -m provisioning.database`, with the Cloud SQL connector replaced by the local
    server: the sales database as copied from the template, a conversation database, and both service roles."""
    import contextlib

    import psycopg
    from conftest import _connect, _execute

    parts = urlsplit(database_server)
    _execute(database_server, f"DROP DATABASE IF EXISTS {STATE_DATABASE}", f"DROP ROLE IF EXISTS {RUNTIME_ROLE}")
    _execute(database_server, f"CREATE DATABASE {STATE_DATABASE}", f"CREATE ROLE {RUNTIME_ROLE} LOGIN")
    for name, value in {
        "DB_NAME": DATABASE,
        "CHECKPOINT_DB_NAME": STATE_DATABASE,
        "TOOLS_DB_USER": TOOLS_ROLE,
        "RUNTIME_DB_USER": RUNTIME_ROLE,
    }.items():
        monkeypatch.setenv(name, value)

    @contextlib.contextmanager
    def local_connection(name, driver="pg8000", **options):
        if driver == "pg8000":
            connection = pg8000.dbapi.connect(user=parts.username, host=parts.hostname, port=parts.port, database=name)
        else:
            connection = psycopg.connect(
                host=parts.hostname, port=parts.port, user=parts.username, dbname=name, **options
            )
        try:
            yield connection
        finally:
            connection.close()

    monkeypatch.setattr(db_setup, "admin_connection", local_connection)

    def scalar(database_name: str, sql: str):
        connection = _connect(database_server, database_name)
        try:
            cursor = connection.cursor()
            cursor.execute(sql)
            return cursor.fetchone()[0]
        finally:
            connection.close()

    yield scalar
    _execute(
        database_server, f"DROP DATABASE IF EXISTS {STATE_DATABASE} WITH (FORCE)", f"DROP ROLE IF EXISTS {RUNTIME_ROLE}"
    )


def test_apply_gives_each_service_only_its_own_database_and_can_be_repeated(local_stack):
    scalar = local_stack
    partners = scalar(DATABASE, "SELECT count(*) FROM partners")

    db_setup.apply()
    db_setup.apply()  # Terraform runs it again whenever the grants change

    assert scalar(DATABASE, f"SELECT has_table_privilege('{TOOLS_ROLE}', 'partners', 'SELECT')") is True
    assert scalar(DATABASE, f"SELECT has_table_privilege('{TOOLS_ROLE}', 'partners', 'DELETE')") is False
    assert scalar(DATABASE, f"SELECT has_table_privilege('{RUNTIME_ROLE}', 'partners', 'SELECT')") is False
    assert scalar(STATE_DATABASE, f"SELECT has_table_privilege('{RUNTIME_ROLE}', 'checkpoints', 'INSERT')") is True
    assert scalar(STATE_DATABASE, f"SELECT has_table_privilege('{RUNTIME_ROLE}', 'checkpoints', 'TRUNCATE')") is False
    assert scalar(DATABASE, f"SELECT to_regclass('{OPERATIONS_TABLE}')::text") is not None
    assert scalar(DATABASE, "SELECT count(*) FROM partners") == partners  # nothing was reloaded or overwritten


def test_teardown_leaves_the_sales_data_and_lets_terraform_delete_the_users(local_stack):
    scalar = local_stack
    db_setup.apply()
    orders = scalar(DATABASE, "SELECT count(*) FROM sales_orders")

    db_setup.teardown()

    assert scalar(DATABASE, f"SELECT to_regclass('{OPERATIONS_TABLE}')::text") is None
    assert scalar(DATABASE, f"SELECT has_table_privilege('{TOOLS_ROLE}', 'sales_orders', 'SELECT')") is False
    assert scalar(DATABASE, "SELECT count(*) FROM sales_orders") == orders
