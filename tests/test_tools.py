"""The MCP tool server (backend/): lookups, ownership checks, the approval policy, idempotent changes, the database
grants. Needs a PostgreSQL server, see tests/conftest.py."""

import asyncio
import os

import asyncpg
import pytest

from backend.store import Store

ME = "0100000022"  # Tires On Fire: 8 orders
OPEN_ORDER = "0500000333"  # mine: 16,576 USD, in progress, 5 lines, delivery on 2026-11-10
LARGE_ORDER = "0500000318"  # mine: 124,157 USD, above the approval limit, in progress and overdue
COMPLETED_ORDER = "0500000271"  # mine: billed and delivered
OTHER = "0100000003"
OTHER_ORDER = "0500000327"  # theirs, in progress
BILLED_AHEAD = ("0100000004", "0500000310")  # billed but not delivered yet
BIKE = {"product_id": "RO-1001", "quantity": 2}  # 525 USD each
NEW_ORDER = {"requester_id": ME, "lines": [BIKE], "idempotency_key": "operation-1"}


def error_code(response: dict) -> str | None:
    return response.get("error", {}).get("code")


# --- Lookups ----------------------------------------------------------------------------------------------------------


def test_partner_account_names_the_sales_representative(call_tool):
    account = call_tool("get_partner_account", requester_id=ME)

    assert account["company_name"] == "Tires On Fire"
    assert account["sales_rep"] == "Javas Hegde"


def test_order_lookup_accepts_unpadded_ids_and_says_what_can_change(call_tool):
    order = call_tool("get_sales_order", requester_id=ME, sales_order_id="500000333")

    assert order["sales_order_id"] == OPEN_ORDER
    assert order["lifecycle_status"] == "In progress"
    assert [line["item_number"] for line in order["lines"]] == [10, 20, 30, 40, 50]
    assert (order["can_change_items"], order["can_cancel"], order["can_reschedule"]) == (True, True, True)
    assert order["overdue"] is False


def test_orders_are_listed_newest_first_and_filtered(call_tool):
    everything = call_tool("list_sales_orders", requester_id=ME, limit=2)
    overdue = call_tool("list_sales_orders", requester_id=ME, overdue_only=True)
    completed = call_tool("list_sales_orders", requester_id=ME, lifecycle_status="Completed", created_from="2026-01-01")

    assert everything["total_count"] == 8
    assert [order["sales_order_id"] for order in everything["orders"]] == [OPEN_ORDER, LARGE_ORDER]
    assert [order["sales_order_id"] for order in overdue["orders"]] == [LARGE_ORDER]
    assert [order["sales_order_id"] for order in completed["orders"]] == [COMPLETED_ORDER, "0500000218"]


def test_product_search_filters_by_category_and_price(call_tool):
    products = call_tool("search_products", requester_id=ME, category="eBike", max_price=1500)["products"]

    assert [product["product_id"] for product in products] == ["EB-1131"]
    assert "supplier_partner_id" not in products[0]  # suppliers are other partners, and not public


def test_account_summary_reports_spend_and_open_items(call_tool):
    summary = call_tool("get_account_summary", requester_id=ME, fiscal_year=2026)

    assert {row["status"]: row["orders"] for row in summary["by_status"]} == {"Completed": 2, "In progress": 2}
    assert summary["overdue_orders"] == 1
    assert len(summary["top_products"]) == 5


# --- Access -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_sales_order", {"sales_order_id": OTHER_ORDER}),
        ("update_order_item_quantity", {"sales_order_id": OTHER_ORDER, "item_number": 10, "quantity": 1}),
        ("change_delivery_date", {"sales_order_id": OTHER_ORDER, "delivery_date": "2026-12-01"}),
        ("cancel_sales_order", {"sales_order_id": OTHER_ORDER, "reason": "Not needed"}),
    ],
)
def test_partners_cannot_reach_other_partners_orders(call_tool, database, tool, arguments):
    response = call_tool(tool, requester_id=ME, idempotency_key="operation-1", **arguments)

    assert error_code(response) == "FORBIDDEN"
    assert database("SELECT count(*) FROM agent_operations") == [[0]]


@pytest.mark.parametrize("requester", ["", "22", "0100009999", "0100000022' OR '1'='1"])
def test_calls_must_be_bound_to_an_existing_partner(call_tool, requester):
    assert error_code(call_tool("get_partner_account", requester_id=requester)) == "FORBIDDEN"
    assert error_code(call_tool("list_sales_orders", requester_id=requester)) == "FORBIDDEN"


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_sales_order", {"sales_order_id": "abc"}),
        ("list_sales_orders", {"limit": 0}),
        ("list_sales_orders", {"created_from": "last week"}),
        ("search_products", {"limit": 500}),
        ("create_sales_order", {**NEW_ORDER, "lines": []}),
        ("create_sales_order", {**NEW_ORDER, "lines": [{**BIKE, "quantity": 0}]}),
        ("create_sales_order", {**NEW_ORDER, "lines": [{**BIKE, "product_id": "roady"}]}),
        ("create_sales_order", {**NEW_ORDER, "idempotency_key": "not a valid key"}),
        ("cancel_sales_order", {"sales_order_id": OPEN_ORDER, "reason": " ", "idempotency_key": "operation-1"}),
        ("update_partner_contact", {"idempotency_key": "operation-1"}),
        ("update_partner_contact", {"idempotency_key": "operation-1", "email": "not-an-address"}),
    ],
)
def test_invalid_arguments_are_rejected_as_not_retryable(call_tool, tool, arguments):
    response = call_tool(tool, **{"requester_id": ME, **arguments})

    assert error_code(response) == "INVALID_ARGUMENT"
    assert response["error"]["retryable"] is False


def test_unknown_orders_and_products_are_not_found(call_tool):
    assert error_code(call_tool("get_sales_order", requester_id=ME, sales_order_id="0599999999")) == "NOT_FOUND"
    unknown_product = {**NEW_ORDER, "lines": [{"product_id": "XX-0000", "quantity": 1}]}
    assert error_code(call_tool("create_sales_order", **unknown_product)) == "NOT_FOUND"


# --- Changes ----------------------------------------------------------------------------------------------------------


def test_new_order_is_priced_from_the_catalogue_and_balanced(call_tool, database):
    order = call_tool("create_sales_order", **NEW_ORDER)

    assert order["sales_order_id"] == "0500000334"  # continues after the highest id in the dataset
    assert (order["gross_amount"], order["net_amount"], order["tax_amount"]) == (1050.0, 918.75, 131.25)
    assert order["delivery_date"] == "2026-12-17"  # the standard lead time after the business date
    assert order["replayed"] is False
    stored = call_tool("get_sales_order", requester_id=ME, sales_order_id=order["sales_order_id"])
    assert stored["gross_amount"] == sum(line["line_gross_amount"] for line in stored["lines"]) == 1050.0
    assert database("SELECT tool, target, value_added FROM agent_operations") == [
        ["create_sales_order", "new-order", 1050]
    ]


def test_retried_change_returns_the_original_result(call_tool, database):
    first = call_tool("create_sales_order", **NEW_ORDER)
    retry = call_tool("create_sales_order", **NEW_ORDER)

    assert retry == {**first, "replayed": True}
    assert database("SELECT count(*) FROM sales_orders WHERE partner_id = %s", ME) == [[9]]
    assert database("SELECT count(*) FROM agent_operations") == [[1]]


def test_operation_cannot_be_reused_for_a_different_request(call_tool, database):
    call_tool("create_sales_order", **NEW_ORDER)
    response = call_tool("create_sales_order", **{**NEW_ORDER, "lines": [{**BIKE, "quantity": 3}]})

    assert error_code(response) == "IDEMPOTENCY_CONFLICT"
    assert database("SELECT count(*) FROM sales_orders WHERE partner_id = %s", ME) == [[9]]


def test_distinct_operations_are_separate_orders(call_tool):
    orders = [call_tool("create_sales_order", **{**NEW_ORDER, "idempotency_key": key}) for key in ("op-1", "op-2")]

    assert [order["replayed"] for order in orders] == [False, False]
    assert orders[0]["sales_order_id"] != orders[1]["sales_order_id"]


def test_quantity_change_recalculates_the_order(call_tool):
    change = {"sales_order_id": OPEN_ORDER, "item_number": 20, "quantity": 5, "idempotency_key": "operation-1"}

    result = call_tool("update_order_item_quantity", requester_id=ME, **change)

    assert result["line_gross_amount"] == 2245.0  # 5 x 449 instead of 3 x 449
    assert result["order_gross_amount"] == 16576.0 + 2 * 449.0
    assert call_tool("get_sales_order", requester_id=ME, sales_order_id=OPEN_ORDER)["gross_amount"] == 17474.0
    missing_line = {**change, "item_number": 99, "idempotency_key": "operation-2"}
    assert error_code(call_tool("update_order_item_quantity", requester_id=ME, **missing_line)) == "NOT_FOUND"


def test_delivery_date_moves_for_all_lines_within_the_allowed_window(call_tool, database):
    change = {"requester_id": ME, "sales_order_id": OPEN_ORDER, "idempotency_key": "operation-1"}

    assert call_tool("change_delivery_date", delivery_date="2026-12-01", **change)["delivery_date"] == "2026-12-01"
    assert database(
        "SELECT DISTINCT delivery_date::text FROM sales_order_items WHERE sales_order_id = %s", OPEN_ORDER
    ) == [["2026-12-01"]]
    too_late = {**change, "delivery_date": "2028-01-01", "idempotency_key": "operation-2"}
    assert error_code(call_tool("change_delivery_date", **too_late)) == "DELIVERY_DATE_OUT_OF_RANGE"


def test_cancellation_closes_the_order_and_records_the_reason(call_tool, database):
    cancel = {"requester_id": ME, "sales_order_id": OPEN_ORDER, "reason": "Ordered twice", "idempotency_key": "op-1"}

    assert call_tool("cancel_sales_order", **cancel)["status"] == "Cancelled"
    assert call_tool("cancel_sales_order", **cancel)["replayed"] is True
    order = call_tool("get_sales_order", requester_id=ME, sales_order_id=OPEN_ORDER)
    assert (order["lifecycle_status"], order["can_cancel"]) == ("Cancelled", False)
    assert database("SELECT reason FROM agent_operations") == [["Ordered twice"]]


def test_retried_cancellation_is_replayed_whatever_reason_the_model_words(call_tool, database):
    cancel = {"requester_id": ME, "sales_order_id": OPEN_ORDER, "idempotency_key": "operation-1"}

    first = call_tool("cancel_sales_order", reason="Ordered twice", **cancel)
    retry = call_tool("cancel_sales_order", reason="Duplicate order", **cancel)  # a new session words it differently

    assert (first["replayed"], retry["replayed"]) == (False, True)
    assert retry["reason"] == "Ordered twice"  # the original result, not the new wording
    assert database("SELECT reason FROM agent_operations") == [["Ordered twice"]]


def test_billed_or_delivered_orders_cannot_be_changed(call_tool):
    completed = {"requester_id": ME, "sales_order_id": COMPLETED_ORDER, "idempotency_key": "operation-1"}
    partner, billed_ahead = BILLED_AHEAD
    billed = {"requester_id": partner, "sales_order_id": billed_ahead, "idempotency_key": "operation-1"}

    assert error_code(call_tool("cancel_sales_order", reason="Not needed", **completed)) == "ORDER_NOT_CHANGEABLE"
    assert (
        error_code(call_tool("change_delivery_date", delivery_date="2026-12-01", **completed)) == "ORDER_NOT_CHANGEABLE"
    )
    assert error_code(call_tool("cancel_sales_order", reason="Not needed", **billed)) == "ORDER_NOT_CHANGEABLE"
    # Billing does not block a new delivery date.
    assert "error" not in call_tool("change_delivery_date", delivery_date="2026-12-01", **billed)


def test_contact_details_are_updated(call_tool):
    update = {"requester_id": ME, "phone": "+49 30 1234567", "idempotency_key": "operation-1"}

    assert call_tool("update_partner_contact", **update)["phone"] == "+49 30 1234567"
    account = call_tool("get_partner_account", requester_id=ME)
    assert (account["phone"], account["email"]) == ("+49 30 1234567", "pooja.singh@tiresonfire.co.in")


# --- Policy -----------------------------------------------------------------------------------------------------------


def test_changes_above_the_approval_limit_need_a_person(call_tool, database):
    too_large = {**NEW_ORDER, "lines": [{"product_id": "RO-1001", "quantity": 200}]}  # 105,000 USD
    cancel = {"requester_id": ME, "sales_order_id": LARGE_ORDER, "reason": "Late", "idempotency_key": "operation-2"}

    assert error_code(call_tool("create_sales_order", **too_large)) == "POLICY_DENIED"
    assert error_code(call_tool("cancel_sales_order", **cancel)) == "POLICY_DENIED"
    assert database("SELECT count(*) FROM agent_operations") == [[0]]
    assert database("SELECT lifecycle_status FROM sales_orders WHERE sales_order_id = %s", LARGE_ORDER) == [["I"]]


def test_approval_limit_cannot_be_bypassed_by_splitting_an_order(call_tool):
    def order(key: str, quantity: int) -> dict:
        return call_tool(
            "create_sales_order",
            requester_id=ME,
            idempotency_key=key,
            lines=[{"product_id": "RO-1001", "quantity": quantity}],
        )

    assert "sales_order_id" in order("part-1", 120)  # 63,000 USD
    assert error_code(order("part-2", 120)) == "POLICY_DENIED"  # 126,000 USD within 24 hours
    assert "sales_order_id" in order("part-3", 60)  # 94,500 USD in total


# --- Failures ---------------------------------------------------------------------------------------------------------


def test_data_store_failures_are_reported_as_retryable(call_tool, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise asyncpg.CannotConnectNowError("the database system is starting up")

    monkeypatch.setattr(Store, "get_order", unavailable)
    response = call_tool("get_sales_order", requester_id=ME, sales_order_id=OPEN_ORDER)

    assert error_code(response) == "BACKEND_UNAVAILABLE"
    assert response["error"]["retryable"] is True


def test_unexpected_failures_do_not_leak_details(call_tool, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("password=secret")

    monkeypatch.setattr(Store, "get_partner", broken)
    response = call_tool("get_partner_account", requester_id=ME)

    assert error_code(response) == "INTERNAL_ERROR"
    assert "secret" not in response["error"]["message"]


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM sales_orders WHERE sales_order_id = '0500000333'",
        "UPDATE products SET list_price = 1 WHERE product_id = 'RO-1001'",
        "UPDATE sales_orders SET partner_id = '0100000003' WHERE sales_order_id = '0500000333'",
        "UPDATE agent_operations SET value_added = 0",
        "SELECT login FROM employees",
    ],
)
def test_database_grants_stop_what_the_tools_never_do(database, statement):
    """The tool server's database user, as granted by provisioning/database.py, cannot go beyond what the tools need."""

    async def run() -> None:
        connection = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await connection.execute(statement)
        finally:
            await connection.close()

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        asyncio.run(run())
