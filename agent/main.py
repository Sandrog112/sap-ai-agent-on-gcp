"""Sales service agent: LangGraph on Agent Runtime, Gemini Decision 1 routing each message to one of two Gemini model
tiers, business tools through the MCP server on Cloud Run, conversation state in Cloud SQL and partner preferences in
Memory Bank."""

import asyncio
import logging
import os
import re
from collections import Counter
from typing import Any

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from agent.graph import build_graph, model_of
from agent.memory import PartnerMemory, open_checkpointer
from agent.router import TierRouter, load_gemini_api_key
from agent.tools import InvocationContext, load_tools
from shared.telemetry import configure_telemetry, current_trace_id, flush_telemetry

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

SERVICE_NAME = "sap-sales-agent"
ACTOR_ID = re.compile(r"\d{10}")
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
MAX_PROMPT_LENGTH = 4000  # characters; a longer message costs tokens on every model call and is not a business request
# Each model call and tool call is a graph step. The tool loop guard stops the model long before this is reached.
RECURSION_LIMIT = 40


class SalesServiceAgent:
    """The object Agent Runtime serves, see infra/runtime.py."""

    def __init__(self) -> None:
        self._graph: CompiledStateGraph | None = None
        self._lock: asyncio.Lock | None = None

    def set_up(self) -> None:
        """Called once by Agent Runtime when an instance starts."""
        configure_telemetry(SERVICE_NAME)

    def register_operations(self) -> dict[str, list[str]]:
        return {"async": ["async_query"]}

    async def async_query(self, prompt: str, actor_id: str, session_id: str, operation_id: str) -> dict[str, Any]:
        """Answer one partner message.

        Only IAM-authenticated callers with query access reach this method (the front end, later), and they
        authenticate the partner and pass their id as actor_id. It scopes the conversation, the remembered preferences
        and, through RequestBinding, every tool call.

        operation_id identifies the partner's request and is the idempotency key of every change: the caller reuses it
        when it retries the request, in any session, and uses a new one for a new request. A change repeated under the
        same operation_id returns the original result; under another one it is a separate change.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            return {"error": "'prompt' must be a non-empty string."}
        if len(prompt) > MAX_PROMPT_LENGTH:
            return {"error": f"'prompt' must be at most {MAX_PROMPT_LENGTH} characters."}
        if not isinstance(actor_id, str) or not ACTOR_ID.fullmatch(actor_id):
            return {"error": "'actor_id' must be a 10-digit partner id."}
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            return {"error": "'session_id' must be 1-128 letters, digits or . _ : - characters."}
        if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
            return {"error": "'operation_id' must be 1-64 letters, digits or . _ : - characters."}

        attributes = {"session.id": session_id, "actor.id": actor_id, "operation.id": operation_id}
        with tracer.start_as_current_span("invoke_agent", attributes=attributes) as span:
            trace_id = current_trace_id()
            try:
                return await self._answer(prompt, actor_id, session_id, operation_id, trace_id)
            except Exception as error:
                logger.exception("session=<%s>, trace=<%s> | invocation failed", session_id, trace_id)
                # The caller gets a normal response, so mark the span failed to keep it findable in traces.
                span.record_exception(error)
                span.set_status(StatusCode.ERROR, type(error).__name__)
                return {"error": "The sales agent is temporarily unavailable. Please try again.", "trace_id": trace_id}
            finally:
                await asyncio.to_thread(flush_telemetry)

    async def _answer(
        self, prompt: str, actor_id: str, session_id: str, operation_id: str, trace_id: str | None
    ) -> dict[str, Any]:
        graph = await self._ready_graph()
        # The partner is part of the thread id, so a session id from one partner can never open another's conversation.
        config = {"configurable": {"thread_id": f"{actor_id}:{session_id}"}, "recursion_limit": RECURSION_LIMIT}
        state = await graph.ainvoke(
            {"messages": [HumanMessage(prompt)]},
            config,
            context=InvocationContext(actor_id=actor_id, operation_id=operation_id),
        )
        turn = _latest_turn(state["messages"])
        return {
            "response": state["messages"][-1].text.strip(),
            "tier": state["tier"],
            "model": model_of(state["tier"]),
            "route_reason": state["route_reason"],
            "tool_calls": dict(
                Counter(call["name"] for message in turn for call in getattr(message, "tool_calls", []))
            ),
            "session_id": session_id,
            "trace_id": trace_id,
        }

    async def _ready_graph(self) -> CompiledStateGraph:
        """Build the graph on first use: connections belong to the event loop that serves requests, which does not
        exist yet in set_up. A failure is not cached, so the next request tries again."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._graph is None:
                project_id = os.environ["GCP_PROJECT_ID"]
                tools = await load_tools(os.environ["TOOL_SERVER_URL"])
                memory = PartnerMemory(os.getenv("MEMORY_BANK_NAME"), project_id)
                router = TierRouter(await load_gemini_api_key(project_id))
                self._graph = build_graph(tools, memory, await open_checkpointer(), router, project_id)
        return self._graph


def _latest_turn(messages: list[AnyMessage]) -> list[AnyMessage]:
    start = max(index for index, message in enumerate(messages) if message.type == "human")
    return messages[start:]


root_agent = SalesServiceAgent()
