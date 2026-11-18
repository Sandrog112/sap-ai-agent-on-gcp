"""What the agent may do to sales data, decided in code at the tool server, where no prompt can reach it.

The system prompt holds none of these rules. Who may call the tools at all is decided earlier, by Cloud Run's IAM check;
what a caller may then touch is limited again by the database grants of infra/data_store.py.
"""

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from backend.errors import ToolError

# USD: the most the agent may commit on its own. Set by deploy.py from infra/config.py.
AUTO_APPROVAL_LIMIT = Decimal(os.getenv("AUTO_APPROVAL_LIMIT") or "100000.00")
DEFAULT_LEAD_DAYS = 45  # close to the 47-day median between creating an order and delivering it in the dataset
LATEST_DELIVERY_DAYS = 180
MAX_ORDER_LINES = 20  # the largest order in the dataset has 10
MAX_LINE_QUANTITY = 1000

IN_PROGRESS = "I"
STATUS_NAMES = {IN_PROGRESS: "In progress", "C": "Completed", "X": "Cancelled"}


def business_today() -> date:
    """The dataset's "today" when BUSINESS_DATE is set (see DATA_DICTIONARY.md), otherwise the real date."""
    configured = os.getenv("BUSINESS_DATE")
    return date.fromisoformat(configured) if configured else datetime.now(UTC).date()


def check_order_value(gross: Decimal) -> None:
    """Orders above the limit need a person, whether they are being placed, grown or cancelled."""
    if gross > AUTO_APPROVAL_LIMIT:
        raise ToolError(
            "POLICY_DENIED",
            f"An order worth {gross:,.2f} USD is above the {AUTO_APPROVAL_LIMIT:,.2f} USD the agent may handle "
            "automatically. A sales representative must review it.",
        )


def check_recent_exposure(committed: Decimal, increase: Decimal) -> None:
    """Caps the value added per partner in 24 hours, so a large order cannot be split into several allowed ones."""
    if committed + increase > AUTO_APPROVAL_LIMIT:
        raise ToolError(
            "POLICY_DENIED",
            f"This would add {increase:,.2f} USD on top of the {committed:,.2f} USD committed through the agent in the "
            f"last 24 hours, above the {AUTO_APPROVAL_LIMIT:,.2f} USD limit. A sales representative must review it.",
        )


def can_change_items(order: dict[str, Any]) -> bool:
    return _statuses(order) == (IN_PROGRESS, IN_PROGRESS, IN_PROGRESS)


def can_cancel(order: dict[str, Any]) -> bool:
    return can_change_items(order)


def can_reschedule(order: dict[str, Any]) -> bool:
    """Billing does not matter here: one open order in the dataset is billed ahead of its delivery."""
    return order["lifecycle_status"] == IN_PROGRESS and order["delivery_status"] == IN_PROGRESS


def require(allowed: bool, order: dict[str, Any], action: str) -> None:
    if not allowed:
        lifecycle, billing, delivery = (STATUS_NAMES[status] for status in _statuses(order))
        raise ToolError(
            "ORDER_NOT_CHANGEABLE",
            f"Order {order['sales_order_id']} cannot be {action}. Lifecycle: {lifecycle}, billing: {billing}, "
            f"delivery: {delivery}.",
        )


def delivery_date_or_default(requested: date | None, today: date) -> date:
    return requested or today + timedelta(days=DEFAULT_LEAD_DAYS)


def check_delivery_date(requested: date, today: date) -> None:
    latest = today + timedelta(days=LATEST_DELIVERY_DAYS)
    if not today <= requested <= latest:
        raise ToolError("DELIVERY_DATE_OUT_OF_RANGE", f"delivery_date must be between {today} and {latest}.")


def _statuses(order: dict[str, Any]) -> tuple[str, str, str]:
    return order["lifecycle_status"], order["billing_status"], order["delivery_status"]
