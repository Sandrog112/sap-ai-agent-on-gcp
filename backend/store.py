"""Cloud SQL data access: reads of the partner's own data, and idempotent, policy-checked changes to orders.

Every write runs in one transaction with its entry in the agent_operations ledger, so a retry can never apply a change
twice or lose its result, and the ledger says what the agent did and why. Prices always come from the catalogue.
"""

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Iterable
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg
from google.cloud.sql.connector import Connector

from backend import policy
from backend.errors import ToolError
from backend.policy import STATUS_NAMES

OPERATIONS_TABLE = "agent_operations"
ORDER_ID_SEQUENCE = "sales_order_id_seq"
POOL_MAX_SIZE = 5
TAX_RATE = Decimal("0.125")  # share of the gross price, see data_preprocessing/preprocessing.py
AMOUNT_PRECISION = Decimal("0.001")
NEW_ORDER_TARGET = "new-order"
CONTACT_TARGET = "contact"

_ORDER = """
    SELECT o.sales_order_id, o.partner_id, o.sales_org, o.fiscal_year_period, o.currency,
           o.gross_amount, o.net_amount, o.tax_amount, o.lifecycle_status, o.billing_status, o.delivery_status,
           o.created_at, o.changed_at, e.first_name || ' ' || e.last_name AS sales_rep, e.email AS sales_rep_email
    FROM sales_orders o
    JOIN employees e ON e.employee_id = o.created_by
"""
_OVERDUE = """
    o.delivery_status = 'I' AND EXISTS (
        SELECT 1 FROM sales_order_items i WHERE i.sales_order_id = o.sales_order_id AND i.delivery_date < $%d
    )
"""
_SYNC_ORDER_TOTALS = """
    UPDATE sales_orders o
    SET gross_amount = lines.gross, net_amount = lines.net, tax_amount = lines.tax,
        changed_at = GREATEST($2, o.created_at)
    FROM (
        SELECT sum(line_gross_amount) AS gross, sum(line_net_amount) AS net, sum(line_tax_amount) AS tax
        FROM sales_order_items WHERE sales_order_id = $1
    ) lines
    WHERE o.sales_order_id = $1
    RETURNING o.gross_amount, o.net_amount, o.tax_amount
"""


class Store:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def open(cls) -> "Store":
        """Connect with the service account through IAM database authentication, so no password exists to leak.
        DATABASE_URL points a local run at any other Postgres instead."""
        if os.getenv("DATABASE_URL"):
            return cls(await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=0, max_size=POOL_MAX_SIZE))
        # "lazy" refreshes certificates on demand, because Cloud Run throttles the CPU between requests. The connector
        # must share the event loop of the pool that calls it.
        connector = Connector(loop=asyncio.get_running_loop(), refresh_strategy="lazy")

        async def connect(instance: str, **kwargs: Any) -> asyncpg.Connection:
            return await connector.connect_async(
                instance,
                "asyncpg",
                user=os.environ["DB_IAM_USER"],
                db=os.environ["DB_NAME"],
                enable_iam_auth=True,
                **kwargs,
            )

        instance = os.environ["CLOUD_SQL_CONNECTION"]
        return cls(await asyncpg.create_pool(instance, connect=connect, min_size=0, max_size=POOL_MAX_SIZE))

    async def close(self) -> None:
        await self._pool.close()

    # --- Reads --------------------------------------------------------------------------------------------------------

    async def partner_exists(self, partner_id: str) -> bool:
        return await self._pool.fetchval("SELECT EXISTS (SELECT 1 FROM partners WHERE partner_id = $1)", partner_id)

    async def get_partner(self, partner_id: str) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(
            """
            SELECT partner_id, company_name, legal_form, email, phone, website, street, building, city, postal_code,
                   country, region, preferred_currency
            FROM partners WHERE partner_id = $1
            """,
            partner_id,
        )
        if row is None:
            return None
        rep = await self._account_rep(self._pool, partner_id)
        return {**_public(row), "sales_rep": rep["name"], "sales_rep_email": rep["email"]}

    async def get_order(self, sales_order_id: str, today: date) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(f"{_ORDER} WHERE o.sales_order_id = $1", sales_order_id)
        if row is None:
            return None
        lines = await self._pool.fetch(
            """
            SELECT i.item_number, i.product_id, p.name AS product_name, i.quantity, i.quantity_unit,
                   i.unit_gross_amount, i.line_gross_amount, i.delivery_date
            FROM sales_order_items i
            JOIN products p ON p.product_id = i.product_id
            WHERE i.sales_order_id = $1
            ORDER BY i.item_number
            """,
            sales_order_id,
        )
        overdue = row["delivery_status"] == "I" and any(
            line["delivery_date"] is not None and line["delivery_date"] < today for line in lines
        )
        return {
            **_order_view(row),
            "overdue": overdue,
            "lines": [_public(line) for line in lines],
        }

    async def list_orders(
        self,
        partner_id: str,
        today: date,
        lifecycle_status: str | None,
        billing_status: str | None,
        delivery_status: str | None,
        created_from: date | None,
        created_to: date | None,
        overdue_only: bool,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        where = f"""
            WHERE o.partner_id = $1
              AND ($2::text IS NULL OR o.lifecycle_status = $2)
              AND ($3::text IS NULL OR o.billing_status = $3)
              AND ($4::text IS NULL OR o.delivery_status = $4)
              AND ($5::date IS NULL OR o.created_at >= $5)
              AND ($6::date IS NULL OR o.created_at <= $6)
              AND (NOT $7::boolean OR ({_OVERDUE % 8}))
        """
        filters = (partner_id, lifecycle_status, billing_status, delivery_status, created_from, created_to)
        filters = (*filters, overdue_only, today)
        total = await self._pool.fetchval(f"SELECT count(*) FROM sales_orders o {where}", *filters)
        rows = await self._pool.fetch(
            f"{_ORDER} {where} ORDER BY o.created_at DESC, o.sales_order_id DESC LIMIT $9 OFFSET $10",
            *filters,
            limit,
            offset,
        )
        return {"total_count": total, "orders": [_order_view(row) for row in rows]}

    async def search_products(
        self,
        query: str | None,
        category: str | None,
        min_price: Decimal | None,
        max_price: Decimal | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """The supplier is left out on purpose: partners both buy and supply, and suppliers are not public."""
        rows = await self._pool.fetch(
            """
            SELECT product_id, name, description, category_id, category_name, list_price, currency, quantity_unit,
                   weight, weight_unit
            FROM products
            WHERE ($1::text IS NULL
                   OR name ILIKE '%' || $1 || '%' OR description ILIKE '%' || $1 || '%'
                   OR category_name ILIKE '%' || $1 || '%' OR product_id ILIKE '%' || $1 || '%')
              AND ($2::text IS NULL OR category_name ILIKE '%' || $2 || '%' OR category_id ILIKE $2)
              AND ($3::numeric IS NULL OR list_price >= $3)
              AND ($4::numeric IS NULL OR list_price <= $4)
            ORDER BY category_name, list_price, product_id
            LIMIT $5
            """,
            query,
            category,
            min_price,
            max_price,
            limit,
        )
        return [_public(row) for row in rows]

    async def account_summary(self, partner_id: str, today: date, fiscal_year: int | None) -> dict[str, Any]:
        period = f"{fiscal_year}%" if fiscal_year else "%"
        by_status = await self._pool.fetch(
            """
            SELECT lifecycle_status, count(*) AS orders, sum(gross_amount) AS gross_amount
            FROM sales_orders WHERE partner_id = $1 AND fiscal_year_period LIKE $2
            GROUP BY lifecycle_status ORDER BY lifecycle_status
            """,
            partner_id,
            period,
        )
        by_period = await self._pool.fetch(
            """
            SELECT fiscal_year_period, count(*) AS orders, sum(gross_amount) AS gross_amount
            FROM sales_orders WHERE partner_id = $1 AND fiscal_year_period LIKE $2 AND lifecycle_status <> 'X'
            GROUP BY fiscal_year_period ORDER BY fiscal_year_period
            """,
            partner_id,
            period,
        )
        top_products = await self._pool.fetch(
            """
            SELECT i.product_id, p.name AS product_name, sum(i.quantity) AS units, sum(i.line_gross_amount) AS spend
            FROM sales_order_items i
            JOIN sales_orders o USING (sales_order_id)
            JOIN products p ON p.product_id = i.product_id
            WHERE o.partner_id = $1 AND o.fiscal_year_period LIKE $2 AND o.lifecycle_status <> 'X'
            GROUP BY i.product_id, p.name ORDER BY spend DESC, i.product_id LIMIT 5
            """,
            partner_id,
            period,
        )
        open_items = await self._pool.fetchrow(
            f"""
            SELECT count(*) FILTER (WHERE o.delivery_status = 'C' AND o.billing_status = 'I') AS delivered_unbilled,
                   coalesce(sum(o.gross_amount) FILTER (WHERE o.delivery_status = 'C' AND o.billing_status = 'I'), 0)
                       AS delivered_unbilled_amount,
                   count(*) FILTER (WHERE {_OVERDUE % 3}) AS overdue_orders
            FROM sales_orders o
            WHERE o.partner_id = $1 AND o.fiscal_year_period LIKE $2
            """,
            partner_id,
            period,
            today,
        )
        return {
            "currency": "USD",
            "fiscal_year": fiscal_year,
            "by_status": [
                {
                    "status": STATUS_NAMES[row["lifecycle_status"]],
                    "orders": row["orders"],
                    "gross_amount": _plain(row["gross_amount"]),
                }
                for row in by_status
            ],
            "by_fiscal_period": [_public(row) for row in by_period],
            "top_products": [_public(row) for row in top_products],
            **_public(open_items),
        }

    # --- Writes -------------------------------------------------------------------------------------------------------

    async def create_order(
        self,
        partner_id: str,
        lines: list[dict[str, Any]],
        delivery_date: date | None,
        today: date,
        key: str,
    ) -> tuple[dict[str, Any], bool]:
        delivery = policy.delivery_date_or_default(delivery_date, today)
        policy.check_delivery_date(delivery, today)

        async def apply(connection: asyncpg.Connection) -> tuple[dict[str, Any], Decimal]:
            priced = await _priced_lines(connection, lines)
            gross = sum(line["line_gross_amount"] for line in priced)
            net = sum(line["line_net_amount"] for line in priced)
            policy.check_order_value(gross)
            policy.check_recent_exposure(await _recent_exposure(connection, partner_id), gross)

            partner = await connection.fetchrow("SELECT region FROM partners WHERE partner_id = $1", partner_id)
            rep = await self._account_rep(connection, partner_id)
            sequence = await connection.fetchval(f"SELECT nextval('{ORDER_ID_SEQUENCE}')")
            order_id = f"{sequence:010d}"
            await connection.execute(
                """
                INSERT INTO sales_orders (sales_order_id, partner_id, sales_org, fiscal_year_period, currency,
                    gross_amount, net_amount, tax_amount, lifecycle_status, billing_status, delivery_status,
                    created_by, created_at, changed_by, changed_at)
                VALUES ($1, $2, $3, $4, 'USD', $5, $6, $7, 'I', 'I', 'I', $8, $9, $8, $9)
                """,
                order_id,
                partner_id,
                partner["region"],
                f"{today.year}0{today.month:02d}",
                gross,
                net,
                gross - net,
                rep["employee_id"],
                today,
            )
            await connection.executemany(
                """
                INSERT INTO sales_order_items (sales_order_id, item_number, product_id, quantity, quantity_unit,
                    currency, unit_gross_amount, unit_net_amount, unit_tax_amount,
                    line_gross_amount, line_net_amount, line_tax_amount, delivery_date)
                VALUES ($1, $2, $3, $4, $5, 'USD', $6, $7, $8, $9, $10, $11, $12)
                """,
                [
                    (
                        order_id,
                        line["item_number"],
                        line["product_id"],
                        line["quantity"],
                        line["quantity_unit"],
                        line["unit_gross_amount"],
                        line["unit_net_amount"],
                        line["unit_tax_amount"],
                        line["line_gross_amount"],
                        line["line_net_amount"],
                        line["line_tax_amount"],
                        delivery,
                    )
                    for line in priced
                ],
            )
            result = {
                "sales_order_id": order_id,
                "status": STATUS_NAMES["I"],
                "gross_amount": float(gross),
                "net_amount": float(net),
                "tax_amount": float(gross - net),
                "currency": "USD",
                "delivery_date": delivery.isoformat(),
                "sales_rep": rep["name"],
                "lines": [
                    {
                        key_: _plain(line[key_])
                        for key_ in ("item_number", "product_id", "quantity", "line_gross_amount")
                    }
                    for line in priced
                ],
            }
            return result, gross

        # The date as requested, not as resolved: a default that moves with the clock must not fail a later retry.
        request = {"lines": lines, "delivery_date": delivery_date.isoformat() if delivery_date else None}
        return await self._once(partner_id, key, "create_sales_order", NEW_ORDER_TARGET, request, apply)

    async def update_item_quantity(
        self, partner_id: str, sales_order_id: str, item_number: int, quantity: int, today: date, key: str
    ) -> tuple[dict[str, Any], bool]:
        async def apply(connection: asyncpg.Connection) -> tuple[dict[str, Any], Decimal]:
            order = await _locked_order(connection, sales_order_id)
            policy.require(policy.can_change_items(order), order, "changed")
            updated = await connection.fetchrow(
                """
                UPDATE sales_order_items
                SET quantity = $3::integer, line_gross_amount = unit_gross_amount * $3::integer,
                    line_net_amount = unit_net_amount * $3::integer, line_tax_amount = unit_tax_amount * $3::integer
                WHERE sales_order_id = $1 AND item_number = $2
                RETURNING product_id, line_gross_amount
                """,
                sales_order_id,
                item_number,
                quantity,
            )
            if updated is None:
                raise ToolError("NOT_FOUND", f"Order {sales_order_id} has no item {item_number}.")
            totals = await connection.fetchrow(_SYNC_ORDER_TOTALS, sales_order_id, today)
            increase = totals["gross_amount"] - order["gross_amount"]
            if increase > 0:
                policy.check_order_value(totals["gross_amount"])
                policy.check_recent_exposure(await _recent_exposure(connection, partner_id), increase)
            result = {
                "sales_order_id": sales_order_id,
                "item_number": item_number,
                "product_id": updated["product_id"],
                "quantity": quantity,
                "line_gross_amount": float(updated["line_gross_amount"]),
                "order_gross_amount": float(totals["gross_amount"]),
                "order_net_amount": float(totals["net_amount"]),
                "order_tax_amount": float(totals["tax_amount"]),
                "currency": "USD",
            }
            return result, max(increase, Decimal(0))

        request = {"sales_order_id": sales_order_id, "item_number": item_number, "quantity": quantity}
        target = f"{sales_order_id}:{item_number}"
        return await self._once(partner_id, key, "update_order_item_quantity", target, request, apply)

    async def change_delivery_date(
        self, partner_id: str, sales_order_id: str, delivery_date: date, today: date, key: str
    ) -> tuple[dict[str, Any], bool]:
        policy.check_delivery_date(delivery_date, today)

        async def apply(connection: asyncpg.Connection) -> tuple[dict[str, Any], Decimal]:
            order = await _locked_order(connection, sales_order_id)
            policy.require(policy.can_reschedule(order), order, "rescheduled")
            await connection.execute(
                "UPDATE sales_order_items SET delivery_date = $2 WHERE sales_order_id = $1",
                sales_order_id,
                delivery_date,
            )
            await connection.execute(
                "UPDATE sales_orders SET changed_at = GREATEST($2, created_at) WHERE sales_order_id = $1",
                sales_order_id,
                today,
            )
            return {"sales_order_id": sales_order_id, "delivery_date": delivery_date.isoformat()}, Decimal(0)

        request = {"sales_order_id": sales_order_id, "delivery_date": delivery_date.isoformat()}
        return await self._once(partner_id, key, "change_delivery_date", sales_order_id, request, apply)

    async def cancel_order(
        self, partner_id: str, sales_order_id: str, reason: str, today: date, key: str
    ) -> tuple[dict[str, Any], bool]:
        async def apply(connection: asyncpg.Connection) -> tuple[dict[str, Any], Decimal]:
            order = await _locked_order(connection, sales_order_id)
            policy.require(policy.can_cancel(order), order, "cancelled")
            policy.check_order_value(order["gross_amount"])
            # Cancelled orders keep their amounts but have no delivery date, see DATA_DICTIONARY.md.
            await connection.execute(
                """
                UPDATE sales_orders
                SET lifecycle_status = 'X', billing_status = 'X', delivery_status = 'X',
                    changed_at = GREATEST($2, created_at)
                WHERE sales_order_id = $1
                """,
                sales_order_id,
                today,
            )
            await connection.execute(
                "UPDATE sales_order_items SET delivery_date = NULL WHERE sales_order_id = $1", sales_order_id
            )
            return {"sales_order_id": sales_order_id, "status": STATUS_NAMES["X"], "reason": reason}, Decimal(0)

        # Not the reason: it is free text, and a retry in a new session may word it differently.
        request = {"sales_order_id": sales_order_id}
        return await self._once(partner_id, key, "cancel_sales_order", sales_order_id, request, apply, reason)

    async def update_contact(
        self, partner_id: str, email: str | None, phone: str | None, today: date, key: str
    ) -> tuple[dict[str, Any], bool]:
        async def apply(connection: asyncpg.Connection) -> tuple[dict[str, Any], Decimal]:
            rep = await self._account_rep(connection, partner_id)
            row = await connection.fetchrow(
                """
                UPDATE partners
                SET email = coalesce($2, email), phone = coalesce($3, phone), changed_by = $4,
                    changed_at = GREATEST($5, created_at)
                WHERE partner_id = $1
                RETURNING partner_id, email, phone
                """,
                partner_id,
                email,
                phone,
                rep["employee_id"],
                today,
            )
            return dict(row), Decimal(0)

        request = {"email": email, "phone": phone}
        return await self._once(partner_id, key, "update_partner_contact", CONTACT_TARGET, request, apply)

    async def _once(
        self,
        partner_id: str,
        key: str,
        tool: str,
        target: str,
        request: dict[str, Any],
        apply: Callable[[asyncpg.Connection], Awaitable[tuple[dict[str, Any], Decimal]]],
        reason: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Apply a change at most once per partner, operation, tool and target. Returns (result, created).

        Repeating the same request returns the stored result with created=False; reusing the key for a different
        request raises IDEMPOTENCY_CONFLICT. Locking the partner first serialises that partner's writes, which keeps
        concurrent retries and the 24-hour exposure total correct.
        """
        fingerprint = _fingerprint(request)
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.fetchval("SELECT partner_id FROM partners WHERE partner_id = $1 FOR UPDATE", partner_id)
            existing = await connection.fetchrow(
                f"""
                SELECT request_fingerprint, result FROM {OPERATIONS_TABLE}
                WHERE partner_id = $1 AND operation_id = $2 AND tool = $3 AND target = $4
                """,
                partner_id,
                key,
                tool,
                target,
            )
            if existing:
                if existing["request_fingerprint"] != fingerprint:
                    raise ToolError(
                        "IDEMPOTENCY_CONFLICT", f"Operation {key!r} was already used for a different {tool} request."
                    )
                return json.loads(existing["result"]), False
            result, value_added = await apply(connection)
            await connection.execute(
                f"""
                INSERT INTO {OPERATIONS_TABLE} (partner_id, operation_id, tool, target, request_fingerprint, result,
                    value_added, reason)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                partner_id,
                key,
                tool,
                target,
                fingerprint,
                json.dumps(result),
                value_added,
                reason,
            )
            return result, True

    @staticmethod
    async def _account_rep(connection: Any, partner_id: str) -> dict[str, str]:
        """The employee who handled the partner's latest order; orders and partners require one, and the employee's
        sales org always matches the partner's. Changes made through the agent are attributed to them, and the
        agent_operations ledger records that the agent made them."""
        row = await connection.fetchrow(
            """
            SELECT e.employee_id, e.first_name || ' ' || e.last_name AS name, e.email
            FROM sales_orders o
            JOIN employees e ON e.employee_id = o.created_by
            WHERE o.partner_id = $1
            ORDER BY o.created_at DESC, o.sales_order_id DESC
            LIMIT 1
            """,
            partner_id,
        )
        if row is None:
            raise ToolError("NOT_FOUND", f"Partner {partner_id} has no sales representative yet.")
        return dict(row)


async def _locked_order(connection: asyncpg.Connection, sales_order_id: str) -> dict[str, Any]:
    row = await connection.fetchrow(
        """
        SELECT sales_order_id, gross_amount, lifecycle_status, billing_status, delivery_status
        FROM sales_orders WHERE sales_order_id = $1 FOR UPDATE
        """,
        sales_order_id,
    )
    if row is None:
        raise ToolError("NOT_FOUND", f"Order {sales_order_id} does not exist.")
    return dict(row)


async def _recent_exposure(connection: asyncpg.Connection, partner_id: str) -> Decimal:
    return await connection.fetchval(
        f"""
        SELECT coalesce(sum(value_added), 0) FROM {OPERATIONS_TABLE}
        WHERE partner_id = $1 AND created_at > now() - interval '24 hours'
        """,
        partner_id,
    )


async def _priced_lines(connection: asyncpg.Connection, lines: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order lines priced from the catalogue, numbered like the dataset's: 10, 20, 30 and so on."""
    priced = []
    for position, line in enumerate(lines, start=1):
        product = await connection.fetchrow(
            "SELECT list_price, quantity_unit FROM products WHERE product_id = $1", line["product_id"]
        )
        if product is None:
            raise ToolError("NOT_FOUND", f"Product {line['product_id']} does not exist.")
        unit_gross = product["list_price"]
        unit_tax = (unit_gross * TAX_RATE).quantize(AMOUNT_PRECISION)
        unit_net = unit_gross - unit_tax
        quantity = line["quantity"]
        priced.append(
            {
                "item_number": position * 10,
                "product_id": line["product_id"],
                "quantity": quantity,
                "quantity_unit": product["quantity_unit"],
                "unit_gross_amount": unit_gross,
                "unit_net_amount": unit_net,
                "unit_tax_amount": unit_tax,
                "line_gross_amount": unit_gross * quantity,
                "line_net_amount": unit_net * quantity,
                "line_tax_amount": unit_tax * quantity,
            }
        )
    return priced


def _order_view(row: asyncpg.Record) -> dict[str, Any]:
    """The order as the agent sees it: statuses in words, and the actions the policy currently allows."""
    order = dict(row)
    return {
        **_public(row),
        "lifecycle_status": STATUS_NAMES[order["lifecycle_status"]],
        "billing_status": STATUS_NAMES[order["billing_status"]],
        "delivery_status": STATUS_NAMES[order["delivery_status"]],
        "can_change_items": policy.can_change_items(order),
        "can_cancel": policy.can_cancel(order),
        "can_reschedule": policy.can_reschedule(order),
    }


def _fingerprint(request: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True, default=str).encode()).hexdigest()


def _public(row: Any) -> dict[str, Any]:
    """JSON-serialisable copy of a row."""
    return {key: _plain(value) for key, value in dict(row).items()}


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    return value
