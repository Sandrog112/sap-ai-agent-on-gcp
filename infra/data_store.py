"""Cloud SQL: the sales data, the agent's ledger and conversation state, and exactly what each service may do with them.

The two services sign in with their service accounts (IAM database authentication) and get table privileges only:
the tool server on the sales database, the runtime on its conversation state. Neither holds any on the other's.

Nothing here replaces business data. It is only loaded into an empty database, by data_preprocessing/upload_to_gcp.py;
rerun deploy.py after reloading with --replace, because dropping the tables also drops their grants.
"""

import contextlib
import subprocess
import sys
from collections.abc import Iterable, Iterator
from typing import Any

from google.cloud.sql.connector import Connector
from googleapiclient.errors import HttpError
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

from backend.store import OPERATIONS_TABLE, ORDER_ID_SEQUENCE
from infra.config import CHECKPOINT_DATABASE, INSTANCE_TIER, ROOT
from infra.gcp import (
    client,
    database_user,
    execute,
    instance_connection_name,
    project_id,
    region,
    setting,
    wait_for,
)

IAM_USER_KINDS = ("tools", "runtime")
_MISSING = 404

# Who did what through the agent. It is deliberately not tied to the business tables with foreign keys, so reloading
# those stays possible, and the tool server may only add to it.
AGENT_SCHEMA = (
    f"""
    CREATE TABLE IF NOT EXISTS {OPERATIONS_TABLE} (
        partner_id TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        target TEXT NOT NULL,
        request_fingerprint TEXT NOT NULL,
        result JSONB NOT NULL,
        value_added NUMERIC(14, 3) NOT NULL DEFAULT 0,
        reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (partner_id, operation_id, tool, target)
    )
    """,
    f"CREATE INDEX IF NOT EXISTS {OPERATIONS_TABLE}_recent_idx ON {OPERATIONS_TABLE} (partner_id, created_at)",
    f"CREATE SEQUENCE IF NOT EXISTS {ORDER_ID_SEQUENCE}",
    # Order ids continue after the highest existing one and never go back, even after a reload.
    f"""
    SELECT setval('{ORDER_ID_SEQUENCE}', GREATEST(
        (SELECT coalesce(max(sales_order_id::bigint), 1) FROM sales_orders),
        (SELECT last_value FROM {ORDER_ID_SEQUENCE})
    ))
    """,
)

# What the tool server's identity may do: {user} is replaced with its quoted database user. There is no DELETE anywhere,
# and employees' logins and phone numbers stay out of reach. Column lists keep a bug or a compromised server from
# rewriting a customer's address, a product's price or an order's owner. tests/conftest.py runs the tools under
# exactly these grants.
TOOL_SERVER_GRANTS = (
    "GRANT SELECT ON partners, products, sales_orders, sales_order_items TO {user}",
    "GRANT SELECT (employee_id, first_name, last_name, email, sales_org) ON employees TO {user}",
    "GRANT INSERT ON sales_orders, sales_order_items TO {user}",
    "GRANT UPDATE (gross_amount, net_amount, tax_amount, lifecycle_status, billing_status, delivery_status, "
    "changed_at) ON sales_orders TO {user}",
    "GRANT UPDATE (quantity, line_gross_amount, line_net_amount, line_tax_amount, delivery_date) "
    "ON sales_order_items TO {user}",
    "GRANT UPDATE (email, phone, changed_by, changed_at) ON partners TO {user}",
    f"GRANT SELECT, INSERT ON {OPERATIONS_TABLE} TO {{user}}",
    f"GRANT USAGE ON SEQUENCE {ORDER_ID_SEQUENCE} TO {{user}}",
)
# Takes the business tables back, so a rerun leaves exactly the grants above and a destroy leaves none.
TOOL_SERVER_REVOKE = "REVOKE ALL ON partners, products, employees, sales_orders, sales_order_items FROM {user}"
# The runtime's identity only reaches its own conversation state, and cannot change the schema of it.
RUNTIME_GRANT = "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {user}"


def deploy_data_store() -> None:
    ensure_instance()
    ensure_database(setting("DB_NAME"))
    ensure_database(CHECKPOINT_DATABASE)
    _load_business_data_if_empty()
    _ensure_iam_users()

    with _admin_connection(setting("DB_NAME")) as connection:
        apply_tool_server_access(connection, _quoted(database_user("tools")))
    with _admin_connection(CHECKPOINT_DATABASE, "psycopg", autocommit=True, row_factory=dict_row) as connection:
        # Creates or migrates the checkpoint tables here, which is why the runtime needs no DDL rights.
        PostgresSaver(connection).setup()
        connection.execute(RUNTIME_GRANT.format(user=_quoted(database_user("runtime"))))


def destroy_data_store() -> None:
    """Delete what deploy added. The instance and the sales data are kept, including changes the agent made to them."""
    sqladmin = client("sqladmin", "v1")
    instance = {"project": project_id(), "instance": setting("CLOUD_SQL_INSTANCE")}
    try:
        execute(sqladmin.instances().get(**instance))
    except HttpError as error:
        if error.resp.status == _MISSING:
            return
        raise
    with _admin_connection(setting("DB_NAME")) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (database_user("tools"),))
        tools_user_exists = cursor.fetchone() is not None
        # A database with connections cannot be deleted, and the runtime's pool may still be closing.
        cursor.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (CHECKPOINT_DATABASE,)
        )
        cursor.close()
        cleanup = [f"DROP TABLE IF EXISTS {OPERATIONS_TABLE}", f"DROP SEQUENCE IF EXISTS {ORDER_ID_SEQUENCE}"]
        if tools_user_exists:  # a user with grants left cannot be deleted
            cleanup.append(TOOL_SERVER_REVOKE)
        _run(connection, cleanup, _quoted(database_user("tools")))
    if _exists(sqladmin.databases().list(**instance), CHECKPOINT_DATABASE):
        _wait(execute(sqladmin.databases().delete(database=CHECKPOINT_DATABASE, **instance)))
    for kind in IAM_USER_KINDS:
        if _exists(sqladmin.users().list(**instance), database_user(kind)):
            _wait(execute(sqladmin.users().delete(name=database_user(kind), **instance)))


def ensure_instance() -> None:
    """Create the instance if there is none yet. An existing one is left as it is, but must allow IAM sign-in."""
    sqladmin = client("sqladmin", "v1")
    name = setting("CLOUD_SQL_INSTANCE")
    try:
        instance = execute(sqladmin.instances().get(project=project_id(), instance=name))
    except HttpError as error:
        if error.resp.status != _MISSING:
            raise
        body = {
            "name": name,
            "region": region(),
            "databaseVersion": "POSTGRES_18",
            "rootPassword": setting("DB_PASSWORD"),
            "settings": {
                "tier": INSTANCE_TIER,
                "edition": "ENTERPRISE",
                "databaseFlags": [{"name": "cloudsql.iam_authentication", "value": "on"}],
                "ipConfiguration": {"sslMode": "ENCRYPTED_ONLY"},
            },
        }
        _wait(execute(sqladmin.instances().insert(project=project_id(), body=body)))
        return
    flags = {flag["name"]: flag["value"] for flag in instance["settings"].get("databaseFlags", [])}
    if flags.get("cloudsql.iam_authentication") != "on":
        sys.exit(
            f"Set the database flag cloudsql.iam_authentication=on on instance {name}; changing flags restarts it."
        )


def ensure_database(name: str) -> None:
    sqladmin = client("sqladmin", "v1")
    instance = {"project": project_id(), "instance": setting("CLOUD_SQL_INSTANCE")}
    if not _exists(sqladmin.databases().list(**instance), name):
        _wait(execute(sqladmin.databases().insert(body={"name": name}, **instance)))


def apply_tool_server_access(connection: Any, user: str) -> None:
    """The ledger and the order id sequence, then exactly the tool server's grants, in one transaction."""
    _run(connection, [*AGENT_SCHEMA, TOOL_SERVER_REVOKE, *TOOL_SERVER_GRANTS], user)


def _run(connection: Any, statements: Iterable[str], user: str) -> None:
    cursor = connection.cursor()
    try:
        for statement in statements:
            cursor.execute(statement.format(user=user))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def _load_business_data_if_empty() -> None:
    with _admin_connection(setting("DB_NAME")) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT to_regclass('public.sales_orders')::text")
        loaded = cursor.fetchone()[0] is not None
        cursor.close()
    if not loaded:
        subprocess.run([sys.executable, str(ROOT / "data_preprocessing" / "upload_to_gcp.py")], check=True)


def _ensure_iam_users() -> None:
    sqladmin = client("sqladmin", "v1")
    instance = {"project": project_id(), "instance": setting("CLOUD_SQL_INSTANCE")}
    for kind in IAM_USER_KINDS:
        if not _exists(sqladmin.users().list(**instance), database_user(kind)):
            body = {"name": database_user(kind), "type": "CLOUD_IAM_SERVICE_ACCOUNT"}
            _wait(execute(sqladmin.users().insert(body=body, **instance)))


def _exists(list_request: Any, name: str) -> bool:
    return any(item["name"] == name for item in execute(list_request).get("items", []))


def _wait(operation: dict) -> None:
    operations = client("sqladmin", "v1").operations()
    wait_for(
        lambda: _sql_operation_state(execute(operations.get(project=project_id(), operation=operation["name"]))),
        lambda state: state["status"] == "DONE",
    )


def _sql_operation_state(operation: dict) -> dict:
    """Cloud SQL reports a failed operation under `error.errors`; wait_for expects it non-empty under `error`."""
    errors = (operation.get("error") or {}).get("errors")
    return {**operation, "error": errors}


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@contextlib.contextmanager
def _admin_connection(database: str, driver: str = "pg8000", **options: Any) -> Iterator[Any]:
    """A connection as the database admin of .env, used on this machine only."""
    with Connector() as connector:
        connection = connector.connect(
            instance_connection_name(),
            driver,
            user=setting("DB_USER"),
            password=setting("DB_PASSWORD"),
            db=database,
            **options,
        )
        try:
            yield connection
        finally:
            connection.close()
