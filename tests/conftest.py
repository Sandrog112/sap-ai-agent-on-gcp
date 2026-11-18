"""Fixtures for the tool server: a real PostgreSQL database loaded with data/processed, reached with exactly the grants
of infra/data_store.py, so the tests run the same SQL under the same privileges as the deployed server.

The tests that use it are skipped unless TEST_DATABASE_URL points at a PostgreSQL server whose user may create
databases and roles, for example:

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=test postgres:18
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:5433/postgres uv run pytest
"""

import asyncio
import os
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlsplit

import pg8000.dbapi
import pytest
import upload_to_gcp

from backend import server
from infra.config import BUSINESS_DATE
from infra.data_store import apply_tool_server_access

TEMPLATE = "sap_agent_test_template"
DATABASE = "sap_agent_test"
TOOLS_ROLE = "sap_agent_test_tools"


def _connect(url: str, database: str) -> Any:
    parts = urlsplit(url)
    connection = pg8000.dbapi.connect(
        user=parts.username, password=parts.password, host=parts.hostname, port=parts.port or 5432, database=database
    )
    connection.autocommit = True
    return connection


def _execute(url: str, *statements: str) -> None:
    connection = _connect(url, urlsplit(url).path.lstrip("/"))
    try:
        for statement in statements:
            connection.cursor().execute(statement)
    finally:
        connection.close()


@pytest.fixture(scope="session")
def database_server() -> Iterator[str]:
    """The server's URL, with a template database that holds the sales data, the ledger and the tool server's grants."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("needs a PostgreSQL server: set TEST_DATABASE_URL, see tests/conftest.py")
    drop = (
        f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)",
        f"DROP DATABASE IF EXISTS {TEMPLATE}",
        f"DROP ROLE IF EXISTS {TOOLS_ROLE}",
    )
    _execute(url, *drop, f"CREATE ROLE {TOOLS_ROLE} LOGIN PASSWORD '{TOOLS_ROLE}'", f"CREATE DATABASE {TEMPLATE}")
    template = _connect(url, TEMPLATE)
    try:
        template.autocommit = False  # the loader commits once everything checks out
        upload_to_gcp.load(template)
        apply_tool_server_access(template, TOOLS_ROLE)
    finally:
        template.close()
    yield url
    _execute(url, *drop)


@pytest.fixture
def empty_database(database_server: str) -> Iterator[Any]:
    """A connection to a new, empty database, for the tests of the loader itself."""
    _execute(database_server, f"CREATE DATABASE {DATABASE}")
    connection = _connect(database_server, DATABASE)
    connection.autocommit = False  # the loader commits or rolls back itself
    yield connection
    connection.close()
    _execute(database_server, f"DROP DATABASE {DATABASE} WITH (FORCE)")


@pytest.fixture
def database(database_server: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., list]]:
    """A fresh copy of the template for one test, and the settings the tool server reads. Returns a function that runs
    a query as the database owner, to check what a tool call really stored."""
    _execute(database_server, f"CREATE DATABASE {DATABASE} TEMPLATE {TEMPLATE}")
    parts = urlsplit(database_server)
    tools_url = f"postgresql://{TOOLS_ROLE}:{TOOLS_ROLE}@{parts.hostname}:{parts.port or 5432}/{DATABASE}"
    monkeypatch.setenv("DATABASE_URL", tools_url)
    monkeypatch.setenv("BUSINESS_DATE", BUSINESS_DATE)
    monkeypatch.delenv("FAULT_INJECTION", raising=False)
    owner = _connect(database_server, DATABASE)

    def query(sql: str, *parameters: Any) -> list:
        cursor = owner.cursor()
        cursor.execute(sql, parameters)
        return list(cursor.fetchall())

    yield query
    owner.close()
    _execute(database_server, f"DROP DATABASE {DATABASE} WITH (FORCE)")


@pytest.fixture
def call_tool(database: Callable[..., list], monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., dict]]:
    """Call a tool as the MCP server does, argument validation included; returns the tool's JSON result."""
    monkeypatch.setattr(server, "_store", None)  # a connection pool belongs to one test's event loop
    with asyncio.Runner() as runner:

        def call(tool: str, **arguments: Any) -> dict[str, Any]:
            _, result = runner.run(server.mcp.call_tool(tool, arguments))
            return result

        yield call
        if server._store is not None:
            runner.run(server._store.close())
