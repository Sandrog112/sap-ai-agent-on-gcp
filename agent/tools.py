"""Business tools through the MCP server on Cloud Run, and the reliability rules for calling them.

Cloud Run's IAM check is the inbound auth: every MCP request carries an identity token of the caller's service account
(the runtime's own on Agent Runtime), so no secrets or tokens are stored anywhere.
"""

import asyncio
import json
import logging
import random
import subprocess
from collections import Counter
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from google.auth.exceptions import DefaultCredentialsError
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.interceptors import MCPToolCallRequest
from mcp.types import CallToolResult, TextContent
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from agent.config import TOOL_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

SERVER_NAME = "sap-sales"
REQUESTER_FIELD = "requester_id"  # tool argument the backend authorizes against, see backend/server.py
IDEMPOTENCY_FIELD = "idempotency_key"  # tool argument that makes changes idempotent, see backend/store.py
_PLATFORM_FIELDS = {REQUESTER_FIELD, IDEMPOTENCY_FIELD}
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


class IamAuth(httpx.Auth):
    """httpx auth flow that sends an identity token of the current service account, for Cloud Run's IAM check."""

    def __init__(self, url: str) -> None:
        parts = urlsplit(url)
        self._audience = f"{parts.scheme}://{parts.netloc}"
        self._credentials: Any = None
        self._operator_token: str | None = None

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {self._token()}"
        yield request

    def _token(self) -> str:
        if self._operator_token:
            return self._operator_token
        try:
            if self._credentials is None:
                self._credentials = id_token.fetch_id_token_credentials(self._audience, request=Request())
        except DefaultCredentialsError:
            # An operator's user account cannot mint tokens for an audience, but Cloud Run accepts the gcloud one.
            self._operator_token = _gcloud_identity_token()
            return self._operator_token
        if not self._credentials.valid:
            self._credentials.refresh(Request())
        return self._credentials.token


def _gcloud_identity_token() -> str:
    command = ["gcloud", "auth", "print-identity-token"]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()


@dataclass
class ToolBudget:
    """What one request has spent on tools so far, for the loop guard."""

    calls: int = 0
    signatures: Counter[str] = field(default_factory=Counter)


@dataclass
class InvocationContext:
    """The LangGraph runtime context of one request: set in code from the authenticated invocation, out of reach of
    the model."""

    actor_id: str
    operation_id: str
    budget: ToolBudget = field(default_factory=ToolBudget)


def _context(request: MCPToolCallRequest) -> InvocationContext:
    return request.runtime.context


def tool_client(url: str, interceptors: list[Callable] | None = None) -> MultiServerMCPClient:
    """MCP client for the tool server. A local server needs no token."""
    connection: dict[str, Any] = {"transport": "streamable_http", "url": url}
    if urlsplit(url).hostname not in _LOCAL_HOSTS:
        connection["auth"] = IamAuth(url)
    return MultiServerMCPClient({SERVER_NAME: connection}, tool_interceptors=interceptors or [])


async def load_tools(url: str) -> list[BaseTool]:
    """The server's tools as the model should see them, with binding and reliability around every call.

    The fields the platform sets are removed from the schemas, so the model is not even offered them.
    """
    binding = RequestBinding()
    tools = await tool_client(url, [ToolReliability(), binding]).get_tools()
    binding.keyed_tools = {tool.name for tool in tools if IDEMPOTENCY_FIELD in tool.args_schema["properties"]}
    for tool in tools:
        tool.args_schema = _without_platform_fields(tool.args_schema)
    return tools


def _without_platform_fields(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        **schema,
        "properties": {name: spec for name, spec in schema["properties"].items() if name not in _PLATFORM_FIELDS},
        "required": [name for name in schema.get("required", []) if name not in _PLATFORM_FIELDS],
    }


class RequestBinding:
    """Binds every tool call to the signed-in partner and to the caller's operation, instead of trusting the model.

    The requester comes from the authenticated invocation, so no prompt can make a tool act for another partner; the
    backend checks ownership against it. The operation id is the idempotency key of every change: repeating a change
    within an operation, however the model words it and in whichever session, carries the same key, so the backend
    returns the existing result instead of applying it twice. Another operation can make the same change again.
    """

    def __init__(self) -> None:
        self.keyed_tools: set[str] = set()

    async def __call__(self, request: MCPToolCallRequest, handler: Callable[..., Awaitable[Any]]) -> Any:
        context = _context(request)
        arguments = {**request.args, REQUESTER_FIELD: context.actor_id}
        if request.name in self.keyed_tools:
            arguments[IDEMPOTENCY_FIELD] = context.operation_id
        return await handler(request.override(args=arguments))


class ToolReliability:
    """Times out and retries transient tool failures with exponential backoff, stops runaway tool-call loops and
    reports backend errors as failed tool calls.

    A retry re-sends the identical tool call, including the idempotency key, so the backend returns the original result
    instead of applying the change a second time, and that result is reported as the call's own.
    """

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        max_delay: float = 4.0,
        max_tool_calls: int = 15,
        max_identical_calls: int = 2,
        timeout_seconds: float = TOOL_TIMEOUT_SECONDS,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_tool_calls = max_tool_calls
        self.max_identical_calls = max_identical_calls
        self.timeout_seconds = timeout_seconds
        self._sleep = sleep

    async def __call__(self, request: MCPToolCallRequest, handler: Callable[..., Awaitable[Any]]) -> Any:
        with tracer.start_as_current_span(
            f"execute_tool {request.name}", attributes={"gen_ai.tool.name": request.name}
        ) as span:
            refusal = self._stop_loops(request, span)
            if refusal is not None:
                return refusal
            result = await self._call_with_retries(request, handler, span)
            return self._flag_backend_error(request, result, span)

    def _stop_loops(self, request: MCPToolCallRequest, span: trace.Span) -> CallToolResult | None:
        budget = _context(request).budget
        budget.calls += 1
        signature = _signature(request)
        budget.signatures[signature] += 1
        if budget.calls <= self.max_tool_calls and budget.signatures[signature] <= self.max_identical_calls:
            return None
        logger.warning("tool=<%s>, calls=<%d> | loop guard cancelled tool call", request.name, budget.calls)
        span.add_event("tool.loop_guard", {"tool.name": request.name})
        return _failure(
            "Tool call limit reached for this request. Do not call tools again; "
            "answer with the information you already have."
        )

    async def _call_with_retries(
        self, request: MCPToolCallRequest, handler: Callable[..., Awaitable[Any]], span: trace.Span
    ) -> CallToolResult:
        retries = 0
        while True:
            result, retryable = await self._attempt(request, handler, span)
            if not retryable:
                return _report_recovered_replay(result, span) if retries else result
            retries += 1
            if retries >= self.max_attempts:
                logger.warning("tool=<%s>, attempts=<%d> | giving up on transient failure", request.name, retries)
                # The same call from the model would only repeat every attempt, so the loop guard refuses it right away.
                _context(request).budget.signatures[_signature(request)] = self.max_identical_calls
                return result
            delay = min(self.max_delay, self.base_delay * 2 ** (retries - 1)) * random.uniform(0.5, 1.0)
            logger.warning(
                "tool=<%s>, retry=<%d>, delay=<%.2fs> | retrying transient failure", request.name, retries, delay
            )
            span.add_event(
                "tool.retry", {"tool.name": request.name, "retry.attempt": retries, "retry.delay_seconds": delay}
            )
            await self._sleep(delay)

    async def _attempt(
        self, request: MCPToolCallRequest, handler: Callable[..., Awaitable[Any]], span: trace.Span
    ) -> tuple[CallToolResult, bool]:
        """One call of the tool: its result, and whether repeating the identical call may succeed."""
        try:
            result = await asyncio.wait_for(handler(request), self.timeout_seconds)
        except Exception as error:
            span.record_exception(error)
            transient = is_transient(error)
            message = "The tool timed out." if isinstance(error, TimeoutError) else "The tool is unavailable."
            return _failure(message if transient else "The tool could not be called."), transient
        error = backend_error(result)
        return result, error is not None and error.get("retryable") is True

    def _flag_backend_error(
        self, request: MCPToolCallRequest, result: CallToolResult, span: trace.Span
    ) -> CallToolResult:
        """Mark backend errors as failed results; the backend returns them as normal MCP responses, so the tool span
        would otherwise end with status OK and the failure would not show up in trace searches."""
        error = backend_error(result)
        if error is None:
            if result.isError:
                span.set_status(StatusCode.ERROR, "tool_error")
            return result
        span.set_status(StatusCode.ERROR, str(error.get("code")))
        span.add_event(
            "tool.backend_error",
            {
                "tool.name": request.name,
                "error.code": str(error.get("code")),
                "error.retryable": error.get("retryable") is True,
            },
        )
        return result.model_copy(update={"isError": True})


def _signature(request: MCPToolCallRequest) -> str:
    return f"{request.name}:{json.dumps(request.args, sort_keys=True, default=str)}"


def is_transient(error: BaseException) -> bool:
    """Whether a failed call may succeed if repeated. MCP's transport wraps failures in exception groups."""
    if isinstance(error, BaseExceptionGroup):
        return any(is_transient(inner) for inner in error.exceptions)
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in _TRANSIENT_STATUS_CODES
    return isinstance(error, TimeoutError | httpx.TransportError)


def backend_error(result: CallToolResult) -> dict[str, Any] | None:
    """The {"error": {...}} object the backend returned as the tool result, if any."""
    error = _payload(result).get("error")
    return error if isinstance(error, dict) else None


def _payload(result: CallToolResult) -> dict[str, Any]:
    """The JSON value of a tool result, sent as structured content or as text."""
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    for block in result.content:
        if isinstance(block, TextContent):
            try:
                payload = json.loads(block.text)
            except ValueError:
                continue
            return payload if isinstance(payload, dict) else {}
    return {}


def _failure(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)


def _report_recovered_replay(result: CallToolResult, span: trace.Span) -> CallToolResult:
    """Report replayed=false when a retry gets back the change that an earlier attempt of the same call committed
    (e.g. its response was lost), so the model does not tell the partner it had been made before."""
    payload = _payload(result)
    if payload.get("replayed") is not True:
        return result
    span.add_event("tool.replay_recovered")
    recovered = {**payload, "replayed": False}
    return result.model_copy(
        update={
            "content": [TextContent(type="text", text=json.dumps(recovered))],
            "structuredContent": recovered if result.structuredContent is not None else None,
        }
    )
