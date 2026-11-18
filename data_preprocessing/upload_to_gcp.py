"""Load data/processed/ into the Cloud SQL for PostgreSQL database the agent works against.

Tables, keys and column comments are generated from the SCHEMA in preprocessing.py. The whole load is one transaction,
so a failed run leaves the database as it was. Existing tables are only replaced with --replace, because that discards
any changes the agent has made since the last load.

Usage:
    uv run --env-file .env python data_preprocessing/upload_to_gcp.py [--replace]
"""

import argparse
import csv
import logging
import os
from pathlib import Path

from google.cloud.sql.connector import Connector
from preprocessing import PROCESSED_DIR, RELATIONSHIPS, SALES_ORGS, SCHEMA, STATUS_CODES, STATUS_COLUMNS, Table

logger = logging.getLogger(__name__)

SQL_TYPES = {
    "text": "TEXT",
    "date": "DATE",
    "integer": "INTEGER",
    # Exact, unlike floats, and three decimals because rounding to two breaks the order totals.
    "decimal": "NUMERIC(14, 3)",
}


def _quoted_list(values: set[str]) -> str:
    return ", ".join(f"'{value}'" for value in sorted(values))


# The rules that must keep holding once the agent starts changing data, enforced by the database itself.
CHECKS = {
    "partners": (f"region IN ({_quoted_list(SALES_ORGS)})",),
    "employees": (f"sales_org IN ({_quoted_list(SALES_ORGS)})",),
    "sales_orders": (
        f"sales_org IN ({_quoted_list(SALES_ORGS)})",
        *(f"{column} IN ({_quoted_list(STATUS_CODES)})" for column in STATUS_COLUMNS),
        "changed_at >= created_at",
    ),
    "sales_order_items": ("quantity > 0", "unit_net_amount > 0"),
}

# Orders whose header disagrees with the sum of their lines, or that have no lines at all.
UNBALANCED_ORDERS = """
    SELECT count(*)
    FROM sales_orders o
    LEFT JOIN (
        SELECT sales_order_id,
               sum(line_gross_amount) AS gross,
               sum(line_net_amount) AS net,
               sum(line_tax_amount) AS tax
        FROM sales_order_items
        GROUP BY sales_order_id
    ) lines USING (sales_order_id)
    WHERE o.gross_amount IS DISTINCT FROM lines.gross
       OR o.net_amount IS DISTINCT FROM lines.net
       OR o.tax_amount IS DISTINCT FROM lines.tax
"""


class LoadError(RuntimeError):
    """Raised when the load can't go ahead or doesn't match the files, so that nothing gets committed."""


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def create_table_statements(spec: Table) -> list[str]:
    columns = [
        f"{column.name} {SQL_TYPES[column.type]}{'' if column.nullable else ' NOT NULL'}" for column in spec.columns
    ]
    constraints = [f"PRIMARY KEY ({', '.join(spec.key)})", *(f"CHECK ({rule})" for rule in CHECKS.get(spec.name, ()))]
    body = ",\n    ".join(columns + constraints)
    return [
        f"CREATE TABLE {spec.name} (\n    {body}\n)",
        # The comments let the agent, or anyone else, read the data dictionary from the database itself.
        f"COMMENT ON TABLE {spec.name} IS {_literal(spec.description)}",
        *(f"COMMENT ON COLUMN {spec.name}.{column.name} IS {_literal(column.description)}" for column in spec.columns),
    ]


def foreign_key_statements() -> list[str]:
    return [
        f"ALTER TABLE {child} ADD FOREIGN KEY ({child_column}) REFERENCES {parent} ({parent_column})"
        for child, child_column, parent, parent_column in RELATIONSHIPS
    ]


def _count_csv_rows(path: Path) -> int:
    with path.open(encoding="utf-8", newline="") as file:
        return sum(1 for _ in csv.reader(file)) - 1


def load(connection, data_dir: Path = PROCESSED_DIR, replace: bool = False) -> dict[str, int]:
    """Create and fill every table, then commit only if the result matches the files. Returns rows per table."""
    table_names = [spec.name for spec in SCHEMA]
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        existing = {row[0] for row in cursor.fetchall()} & set(table_names)
        if existing and not replace:
            raise LoadError(f"Tables already exist: {sorted(existing)}. Use --replace to discard them and reload.")
        if existing:
            # No CASCADE: if anything outside these tables depends on them, fail instead of silently dropping it.
            cursor.execute(f"DROP TABLE {', '.join(sorted(existing))}")
            logger.info("Dropped existing tables: %s", sorted(existing))

        for spec in SCHEMA:
            for statement in create_table_statements(spec):
                cursor.execute(statement)

        # Keys are added after the rows, so that the tables can be loaded in any order.
        rows = {}
        for spec in SCHEMA:
            path = data_dir / f"{spec.name}.csv"
            columns = ", ".join(column.name for column in spec.columns)
            with path.open("rb") as file:
                cursor.execute(f"COPY {spec.name} ({columns}) FROM STDIN WITH (FORMAT csv, HEADER true)", stream=file)

            cursor.execute(f"SELECT count(*) FROM {spec.name}")
            rows[spec.name] = cursor.fetchone()[0]
            expected = _count_csv_rows(path)
            if rows[spec.name] != expected:
                raise LoadError(f"{spec.name}: loaded {rows[spec.name]} rows, but the file has {expected}")

        for statement in foreign_key_statements():
            cursor.execute(statement)

        cursor.execute(UNBALANCED_ORDERS)
        unbalanced = cursor.fetchone()[0]
        if unbalanced:
            raise LoadError(f"{unbalanced} orders don't equal the sum of their lines")

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
    return rows


def _setting(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise LoadError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--replace", action="store_true", help="discard existing tables, including any agent changes")
    args = parser.parse_args()

    instance = f"{_setting('GCP_PROJECT_ID')}:{_setting('GCP_REGION')}:{_setting('CLOUD_SQL_INSTANCE')}"
    database = _setting("DB_NAME")

    with Connector() as connector:
        connection = connector.connect(
            instance,
            "pg8000",
            user=_setting("DB_USER"),
            password=_setting("DB_PASSWORD"),
            db=database,
        )
        try:
            rows = load(connection, replace=args.replace)
        finally:
            connection.close()

    for name, count in rows.items():
        logger.info("Loaded %-18s %5d rows", name, count)
    logger.info("Committed to %s, database %s", instance, database)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
