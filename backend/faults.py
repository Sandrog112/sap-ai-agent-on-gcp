"""Injected failures for the observability scenarios, active only when FAULT_INJECTION=true. Each fault is bound to one
open demo order, so asking about that order as its partner triggers it."""

import asyncio
import os
from typing import Any

from backend.errors import ToolError

SLOW_DEPENDENCY_SECONDS = 20  # longer than the agent's per-call timeout, see agent/config.py

ORDER_FAULTS = {
    "0500000314": "http_500",  # partner 0100000039: an upstream system fails, retried with backoff
    "0500000316": "timeout",  # partner 0100000029: an upstream system hangs, per-call timeout
    "0500000327": "stale",  # partner 0100000003: the result asks for another lookup, loop guard
    "0500000328": "lost_response",  # partner 0100000036: fails after committing a cancellation, idempotent retry
}


async def check_lookup(order: dict[str, Any]) -> dict[str, Any]:
    """Simulated upstream call of get_sales_order; returns the order unchanged unless a fault applies."""
    match _fault(order["sales_order_id"]):
        case "http_500":
            raise ToolError("UPSTREAM_ERROR", "The logistics system returned HTTP 500.", retryable=True)
        case "timeout":
            await asyncio.sleep(SLOW_DEPENDENCY_SECONDS)
        case "stale":
            return {**order, "note": "Delivery data is still syncing. Call get_sales_order again for the final status."}
    return order


def confirm_cancellation(sales_order_id: str) -> None:
    """Simulated downstream confirmation, called after a new cancellation is committed."""
    if _fault(sales_order_id) == "lost_response":
        # The change is committed but its response is lost; only an idempotent retry can recover it.
        raise ToolError("UPSTREAM_ERROR", "The fulfilment system returned HTTP 500 after the change.", retryable=True)


def _fault(sales_order_id: str) -> str | None:
    return ORDER_FAULTS.get(sales_order_id) if os.getenv("FAULT_INJECTION") == "true" else None
