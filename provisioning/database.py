"""Cloud SQL's contents: the agent's ledger and conversation state, and exactly what each service may do with them.

Terraform creates the instance, the databases and the IAM database users (terraform/cloud_sql.tf) and runs this module
for what is inside them, which no Google provider resource covers:

    python -m provisioning.database apply      after the users and databases exist
    python -m provisioning.database teardown   before they are deleted

The two services sign in with their service accounts (IAM database authentication) and get table privileges only:
the tool server on the sales database, the runtime on its conversation state. Neither holds any on the other's.

Nothing here replaces business data. It is only loaded into an empty database, by data_preprocessing/upload_to_gcp.py;
apply again after reloading with --replace, because dropping the tables also drops their grants.
tests/conftest.py runs the tool server's tests under exactly these grants.

The settings come from the environment, where Terraform puts them: GCP_PROJECT_ID, GCP_REGION, CLOUD_SQL_INSTANCE,
DB_NAME, CHECKPOINT_DB_NAME, DB_USER and DB_PASSWORD (the admin, used on this machine only), TOOLS_DB_USER and
RUNTIME_DB_USER (the IAM database users).
"""

import argparse
import contextlib
import os
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from backend.store import OPERATIONS_TABLE, ORDER_ID_SEQUENCE

ROOT = Path(__file__).resolve().parent.parent

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
# rewriting a customer's address, a product's price or an order's owner.
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
# Takes the business tables back, so a rerun leaves exactly the grants above and a teardown leaves none.
TOOL_SERVER_REVOKE = "REVOKE ALL ON partners, products, employees, sales_orders, sales_order_items FROM {user}"
# The runtime's identity only reaches its own conversation state, and cannot change the schema of it.
RUNTIME_GRANT = "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {user}"


def apply_tool_server_access(connection: Any, user: str) -> None:
    """The ledger and the order id sequence, then exactly the tool server's grants, in one transaction."""
    _run(connection, [*AGENT_SCHEMA, TOOL_SERVER_REVOKE, *TOOL_SERVER_GRANTS], user)


def remove_tool_server_access(connection: Any, user: str, role_exists: bool = True) -> None:
    """Drop the ledger and the sequence, and take back every grant, because a user with grants left cannot be deleted.
    The sales data stays, including the changes the agent made to it."""
    statements = [f"DROP TABLE IF EXISTS {OPERATIONS_TABLE}", f"DROP SEQUENCE IF EXISTS {ORDER_ID_SEQUENCE}"]
    if role_exists:
        statements.append(TOOL_SERVER_REVOKE)
    _run(connection, statements, user)


def apply() -> None:
    with admin_connection(setting("DB_NAME")) as connection:
        _load_business_data_if_empty(connection)
    with admin_connection(setting("DB_NAME")) as connection:
        apply_tool_server_access(connection, quoted(setting("TOOLS_DB_USER")))

    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    with admin_connection(
        setting("CHECKPOINT_DB_NAME"), "psycopg", autocommit=True, row_factory=dict_row
    ) as connection:
        # Creates or migrates the checkpoint tables here, which is why the runtime needs no DDL rights.
        PostgresSaver(connection).setup()
        connection.execute(RUNTIME_GRANT.format(user=quoted(setting("RUNTIME_DB_USER"))))


def teardown() -> None:
    """Undo apply as far as the database users and the conversation database need it before Terraform deletes them."""
    tools_user = setting("TOOLS_DB_USER")
    with admin_connection(setting("DB_NAME")) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (tools_user,))
        role_exists = cursor.fetchone() is not None
        # A database with connections cannot be deleted, and the runtime's pool may still be closing.
        cursor.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (setting("CHECKPOINT_DB_NAME"),),
        )
        cursor.close()
        remove_tool_server_access(connection, quoted(tools_user), role_exists)


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


def _load_business_data_if_empty(connection: Any) -> None:
    cursor = connection.cursor()
    cursor.execute("SELECT to_regclass('public.sales_orders')::text")
    loaded = cursor.fetchone()[0] is not None
    cursor.close()
    if not loaded:
        subprocess.run([sys.executable, str(ROOT / "data_preprocessing" / "upload_to_gcp.py")], check=True)


def setting(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Set {name}: Terraform passes it to this module, see terraform/database.tf.")
    return value


def quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@contextlib.contextmanager
def admin_connection(database: str, driver: str = "pg8000", **options: Any) -> Iterator[Any]:
    """A connection as the database admin, used on this machine only."""
    from google.cloud.sql.connector import Connector

    instance = f"{setting('GCP_PROJECT_ID')}:{setting('GCP_REGION')}:{setting('CLOUD_SQL_INSTANCE')}"
    with Connector() as connector:
        connection = connector.connect(
            instance, driver, user=setting("DB_USER"), password=setting("DB_PASSWORD"), db=database, **options
        )
        try:
            yield connection
        finally:
            connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["apply", "teardown"])
    {"apply": apply, "teardown": teardown}[parser.parse_args().command]()


if __name__ == "__main__":
    main()
