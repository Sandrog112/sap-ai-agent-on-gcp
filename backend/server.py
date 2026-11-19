"""MCP tool server on Cloud Run: the agent's only way to read or change sales data.

Business errors are returned as {"error": {...}} so the model can react to them; `retryable` tells the agent whether
repeating the identical call may succeed.

Every call carries `requester_id`, the signed-in partner. The agent runtime sets it in code from the authenticated
invocation, overwriting anything the model sends, and Cloud Run only admits IAM-authenticated callers with the invoker
role, so a tool only ever reads or changes the requester's own data, whatever the prompt says.

    uv run python -m backend.server     # locally, with DATABASE_URL set
"""

import asyncio
import functools
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

import asyncpg
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from opentelemetry import trace
from opentelemetry.instrumentation.starlette import StarletteInstrumentor
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, Field
from starlette.applications import Starlette

from backend import policy
from backend.errors import ToolError
from backend.policy import STATUS_NAMES
from backend.store import Store
from shared.telemetry import configure_telemetry

logger = logging.getLogger(__name__)

PARTNER_ID = re.compile(r"\d{10}")
ORDER_ID = re.compile(r"\d{1,10}")
PRODUCT_ID = re.compile(r"[A-Z]{2}-\d{4}")
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PHONE = re.compile(r"\+?[0-9][0-9 ()./-]{4,30}")
MAX_REASON_LENGTH = 500
MAX_PAGE_SIZE = 50
STATUS_CODES = {name: code for code, name in STATUS_NAMES.items()}
Status = Literal["In progress", "Completed", "Cancelled"]
# Failures that repeating the identical call may fix. A retried write is safe, see Store._once.
TRANSIENT_ERRORS = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.DeadlockDetectedError,
    asyncpg.SerializationError,
    asyncpg.TooManyConnectionsError,
    asyncpg.CannotConnectNowError,
    OSError,
    TimeoutError,
)

Requester = Annotated[str, Field(description="Set by the platform to the signed-in partner; leave it out.")]
IdempotencyKey = Annotated[
    str, Field(description="Unique key of this operation. Set by the platform; leave it out. Reuse it when retrying.")
]
SalesOrderId = Annotated[str, Field(description='Sales order id, for example "0500000318".')]

# The host is not used for binding, see main(); any host but localhost keeps FastMCP from rejecting Cloud Run's URL.
mcp = FastMCP("sap-sales-tools", host="0.0.0.0", stateless_http=True, json_response=True)
_store: Store | None = None
_store_lock = asyncio.Lock()


async def store() -> Store:
    """The connection pool, opened once on the server's event loop."""
    global _store
    async with _store_lock:
        if _store is None:
            _store = await Store.open()
    return _store


def business_tool(*, read_only: bool) -> Callable:
    """Register a tool: turn errors into {"error": {...}} results, and log and trace every call."""

    def decorate(handler: Callable[..., Awaitable[dict[str, Any]]]) -> Callable:
        @functools.wraps(handler)
        async def call(**arguments: Any) -> dict[str, Any]:
            started = time.perf_counter()
            span = trace.get_current_span()
            try:
                response, outcome = await handler(**arguments), "OK"
            except ToolError as error:
                response = {"error": {"code": error.code, "message": error.message, "retryable": error.retryable}}
                outcome = error.code
                span.set_status(StatusCode.ERROR, error.code)
            except Exception as error:
                # The details stay in the log and the trace; the model only learns whether trying again may help.
                retryable = isinstance(error, TRANSIENT_ERRORS)
                outcome = "BACKEND_UNAVAILABLE" if retryable else "INTERNAL_ERROR"
                logger.exception("Tool call failed", extra={"tool": handler.__name__})
                span.record_exception(error)
                span.set_status(StatusCode.ERROR, type(error).__name__)
                message = (
                    "Sales data is temporarily unavailable." if retryable else "The request could not be processed."
                )
                response = {"error": {"code": outcome, "message": message, "retryable": retryable}}
            logger.info(
                "tool_call",
                extra={
                    "tool": handler.__name__,
                    "outcome": outcome,
                    "requester_id": arguments.get("requester_id"),
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                },
            )
            return response

        return mcp.tool(annotations=ToolAnnotations(readOnlyHint=read_only))(call)

    return decorate


# --- Lookups ----------------------------------------------------------------------------------------------------------


@business_tool(read_only=True)
async def get_partner_account(requester_id: Requester) -> dict[str, Any]:
    """Look up the signed-in partner's company profile: name, contact details, address, sales region, preferred
    currency and the sales representative of the account. Use for questions about the account or who to contact."""
    partner = await (await store()).get_partner(await _requester(requester_id))
    if partner is None:
        raise ToolError("NOT_FOUND", f"Partner {requester_id} does not exist.")
    return partner


@business_tool(read_only=True)
async def list_sales_orders(
    requester_id: Requester,
    lifecycle_status: Annotated[Status | None, Field(description="Only orders with this overall status.")] = None,
    billing_status: Annotated[Status | None, Field(description="Only orders with this billing status.")] = None,
    delivery_status: Annotated[Status | None, Field(description="Only orders with this delivery status.")] = None,
    created_from: Annotated[str | None, Field(description="Only orders created on or after this YYYY-MM-DD.")] = None,
    created_to: Annotated[str | None, Field(description="Only orders created on or before this YYYY-MM-DD.")] = None,
    overdue_only: Annotated[bool, Field(description="Only undelivered orders past their delivery date.")] = False,
    limit: Annotated[int, Field(description=f"Page size, at most {MAX_PAGE_SIZE}.")] = 20,
    offset: Annotated[int, Field(description="Orders to skip, for paging.")] = 0,
) -> dict[str, Any]:
    """List the signed-in partner's sales orders, newest first, without their lines. Returns the total count and each
    order's amounts and statuses. Use to find orders, for example the open, overdue or unbilled ones."""
    if not 1 <= limit <= MAX_PAGE_SIZE or offset < 0:
        raise ToolError("INVALID_ARGUMENT", f"limit must be 1-{MAX_PAGE_SIZE} and offset must not be negative.")
    return await (await store()).list_orders(
        await _requester(requester_id),
        policy.business_today(),
        STATUS_CODES.get(lifecycle_status),
        STATUS_CODES.get(billing_status),
        STATUS_CODES.get(delivery_status),
        _optional_date(created_from, "created_from"),
        _optional_date(created_to, "created_to"),
        overdue_only,
        limit,
        offset,
    )


@business_tool(read_only=True)
async def get_sales_order(sales_order_id: SalesOrderId, requester_id: Requester) -> dict[str, Any]:
    """Look up one of the signed-in partner's sales orders. Returns amounts, statuses, its sales representative, whether
    it is overdue, each line with product and delivery date, and which changes are currently allowed."""
    return await _owned_order(sales_order_id, requester_id)


@business_tool(read_only=True)
async def search_products(
    requester_id: Requester,
    query: Annotated[str | None, Field(description="Words from a product name or category, or a product id.")] = None,
    category: Annotated[
        str | None, Field(description='Category name or two-letter code, e.g. "eBike" or "EB".')
    ] = None,
    min_price: Annotated[float | None, Field(description="Lowest gross list price in USD.")] = None,
    max_price: Annotated[float | None, Field(description="Highest gross list price in USD.")] = None,
    limit: Annotated[int, Field(description=f"Results to return, at most {MAX_PAGE_SIZE}.")] = 20,
) -> dict[str, Any]:
    """Search the bicycle catalogue. Returns product ids, names, categories, gross list prices (including 12.5% tax) in
    USD, and weights. Use for questions about products or prices, and to find product ids before placing an order."""
    await _requester(requester_id)
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ToolError("INVALID_ARGUMENT", f"limit must be 1-{MAX_PAGE_SIZE}.")
    products = await (await store()).search_products(
        query.strip() if query else None, category, _optional_decimal(min_price), _optional_decimal(max_price), limit
    )
    return {"products": products}


@business_tool(read_only=True)
async def get_account_summary(
    requester_id: Requester,
    fiscal_year: Annotated[int | None, Field(description="Only this fiscal year, for example 2026.")] = None,
) -> dict[str, Any]:
    """Summarise the signed-in partner's business: orders and value by status and by fiscal period (YYYY0MM), top five
    products by spend, delivered orders not billed yet, and overdue orders. Use for questions about spend and trends."""
    return await (await store()).account_summary(await _requester(requester_id), policy.business_today(), fiscal_year)


# --- Business actions -------------------------------------------------------------------------------------------------


class OrderLine(BaseModel):
    product_id: str = Field(description='Product id from search_products, for example "RO-1001".')
    quantity: int = Field(description="Units to order, at least 1.")


@business_tool(read_only=False)
async def create_sales_order(
    lines: Annotated[list[OrderLine], Field(description="The products and quantities to order.")],
    requester_id: Requester,
    idempotency_key: IdempotencyKey,
    delivery_date: Annotated[
        str | None, Field(description="Requested delivery date, YYYY-MM-DD. A standard lead time applies if left out.")
    ] = None,
) -> dict[str, Any]:
    """Place a new sales order for the signed-in partner. Prices come from the catalogue. Idempotent: repeating a call
    with the same idempotency_key returns the original order, with replayed=true, instead of creating another."""
    if not 1 <= len(lines) <= policy.MAX_ORDER_LINES:
        raise ToolError("INVALID_ARGUMENT", f"An order must have 1-{policy.MAX_ORDER_LINES} lines.")
    parsed = [_order_line(line) for line in lines]
    order, created = await (await store()).create_order(
        await _requester(requester_id),
        parsed,
        _optional_date(delivery_date, "delivery_date"),
        policy.business_today(),
        _idempotency_key(idempotency_key),
    )
    return {**order, "replayed": not created}


@business_tool(read_only=False)
async def update_order_item_quantity(
    sales_order_id: SalesOrderId,
    item_number: Annotated[int, Field(description="Line number on the order: 10, 20, 30 and so on.")],
    quantity: Annotated[int, Field(description="The new number of units, at least 1.")],
    requester_id: Requester,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    """Change the quantity of one line on an order that is neither billed nor delivered, recalculating the order total.
    Lines cannot be removed. Idempotent: repeating a call with the same idempotency_key returns the original result."""
    _check_quantity(quantity)
    order = await _owned_order(sales_order_id, requester_id)
    result, created = await (await store()).update_item_quantity(
        order["partner_id"],
        order["sales_order_id"],
        item_number,
        quantity,
        policy.business_today(),
        _idempotency_key(idempotency_key),
    )
    return {**result, "replayed": not created}


@business_tool(read_only=False)
async def change_delivery_date(
    sales_order_id: SalesOrderId,
    delivery_date: Annotated[str, Field(description="The new delivery date, YYYY-MM-DD.")],
    requester_id: Requester,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    """Move the delivery date of an undelivered order, for all its lines. Idempotent: repeating a call with the same
    idempotency_key returns the original result."""
    order = await _owned_order(sales_order_id, requester_id)
    result, created = await (await store()).change_delivery_date(
        order["partner_id"],
        order["sales_order_id"],
        _date(delivery_date, "delivery_date"),
        policy.business_today(),
        _idempotency_key(idempotency_key),
    )
    return {**result, "replayed": not created}


@business_tool(read_only=False)
async def cancel_sales_order(
    sales_order_id: SalesOrderId,
    reason: Annotated[str, Field(description="Short reason for the cancellation.")],
    requester_id: Requester,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    """Cancel an order that is neither billed nor delivered. Idempotent: repeating a call with the same idempotency_key
    returns the original result, with replayed=true."""
    reason = reason.strip()
    if not reason or len(reason) > MAX_REASON_LENGTH:
        raise ToolError("INVALID_ARGUMENT", f"reason must be 1-{MAX_REASON_LENGTH} characters.")
    order = await _owned_order(sales_order_id, requester_id)
    result, created = await (await store()).cancel_order(
        order["partner_id"], order["sales_order_id"], reason, policy.business_today(), _idempotency_key(idempotency_key)
    )
    return {**result, "replayed": not created}


@business_tool(read_only=False)
async def update_partner_contact(
    requester_id: Requester,
    idempotency_key: IdempotencyKey,
    email: Annotated[str | None, Field(description="New contact email.")] = None,
    phone: Annotated[str | None, Field(description="New contact phone number.")] = None,
) -> dict[str, Any]:
    """Change the signed-in partner's contact email or phone number. Idempotent: repeating a call with the same
    idempotency_key returns the original result."""
    if email is None and phone is None:
        raise ToolError("INVALID_ARGUMENT", "Provide an email, a phone number or both.")
    if email is not None and not EMAIL.fullmatch(email):
        raise ToolError("INVALID_ARGUMENT", "email is not a valid address.")
    if phone is not None and not PHONE.fullmatch(phone):
        raise ToolError("INVALID_ARGUMENT", "phone is not a valid number.")
    result, created = await (await store()).update_contact(
        await _requester(requester_id), email, phone, policy.business_today(), _idempotency_key(idempotency_key)
    )
    return {**result, "replayed": not created}


# --- Argument checks --------------------------------------------------------------------------------------------------


async def _requester(requester_id: str) -> str:
    if not PARTNER_ID.fullmatch(requester_id) or not await (await store()).partner_exists(requester_id):
        raise ToolError("FORBIDDEN", "The request is not bound to a signed-in partner.")
    return requester_id


async def _owned_order(sales_order_id: str, requester_id: str) -> dict[str, Any]:
    requester = await _requester(requester_id)
    order_id = sales_order_id.strip()
    if not ORDER_ID.fullmatch(order_id):
        raise ToolError("INVALID_ARGUMENT", 'sales_order_id must be numeric, for example "0500000318".')
    order_id = order_id.zfill(10)  # customers often leave out the zero padding
    order = await (await store()).get_order(order_id, policy.business_today())
    if order is None:
        raise ToolError("NOT_FOUND", f"Order {order_id} does not exist.")
    if order["partner_id"] != requester:
        raise ToolError("FORBIDDEN", f"Order {order_id} does not belong to the signed-in partner.")
    return order


def _order_line(line: OrderLine) -> dict[str, Any]:
    if not PRODUCT_ID.fullmatch(line.product_id):
        raise ToolError("INVALID_ARGUMENT", f'{line.product_id!r} is not a product id, which looks like "RO-1001".')
    _check_quantity(line.quantity)
    return {"product_id": line.product_id, "quantity": line.quantity}


def _check_quantity(quantity: int) -> None:
    if not 1 <= quantity <= policy.MAX_LINE_QUANTITY:
        raise ToolError("INVALID_ARGUMENT", f"quantity must be 1-{policy.MAX_LINE_QUANTITY}.")


def _idempotency_key(key: str) -> str:
    if not IDEMPOTENCY_KEY.fullmatch(key):
        raise ToolError("INVALID_ARGUMENT", "idempotency_key must be 1-64 letters, digits or . _ : - characters.")
    return key


def _date(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ToolError("INVALID_ARGUMENT", f"{name} must be a date written YYYY-MM-DD.") from error


def _optional_date(value: str | None, name: str) -> date | None:
    return _date(value, name) if value else None


def _optional_decimal(value: float | None) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def build_app() -> Starlette:
    app = mcp.streamable_http_app()
    StarletteInstrumentor().instrument_app(app)
    return app


def main() -> None:
    configure_telemetry("sap-sales-tools")
    # Cloud Run sends SIGTERM before stopping an instance; uvicorn turns it into a clean exit, which flushes the spans.
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")), log_config=None)


if __name__ == "__main__":
    main()
