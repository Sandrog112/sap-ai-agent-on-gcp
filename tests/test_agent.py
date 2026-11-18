"""The agent (agent/): request validation, the prompt, routing between model tiers, and the rules every tool call goes
through (binding to the signed-in partner, retries with backoff, timeouts, the loop guard). Runs without GCP: the models
are scripted and the tools are stubs."""

import asyncio
import json
import string
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_mcp_adapters.interceptors import MCPToolCallRequest
from langgraph.checkpoint.memory import InMemorySaver
from mcp.types import CallToolResult, TextContent

from agent import graph
from agent.config import COMPLEX_MODEL, MAX_HISTORY_TURNS, ROUTER_MODEL, SIMPLE_MODEL
from agent.graph import RouteDecision, _recent_turns, _system_prompt
from agent.main import SalesServiceAgent
from agent.prompts import SYSTEM_PROMPT
from agent.tools import (
    SERVER_NAME,
    InvocationContext,
    RequestBinding,
    ToolReliability,
    _without_platform_fields,
    is_transient,
)
from backend import server

ME = "0100000022"
VALID = {"prompt": "Where is order 318?", "actor_id": ME, "session_id": "session-1", "operation_id": "operation-1"}
# The tools the prompt describes: its placeholders, apart from the two that are not tool names.
TOOL_NAMES = [
    name
    for _, name, _, _ in string.Formatter().parse(SYSTEM_PROMPT)
    if name and name not in ("actor_id", "business_date")
]
TRANSIENT = {"error": {"code": "UPSTREAM_ERROR", "message": "HTTP 500", "retryable": True}}
PERMANENT = {"error": {"code": "POLICY_DENIED", "message": "above the limit", "retryable": False}}


# --- Entrypoint and prompt --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_",
    [
        {**VALID, "prompt": None},
        {**VALID, "prompt": "   "},
        {**VALID, "actor_id": None},
        {**VALID, "actor_id": "22"},
        {**VALID, "actor_id": "../0100000003"},
        {**VALID, "session_id": ""},
        {**VALID, "session_id": "a session"},
        {**VALID, "operation_id": None},
        {**VALID, "operation_id": "../operation"},
    ],
)
def test_invalid_requests_are_rejected_before_any_model_or_tool(request_):
    response = asyncio.run(SalesServiceAgent().async_query(**request_))

    assert set(response) == {"error"}


def test_prompt_names_exactly_the_tools_the_server_offers():
    offered = [tool.name for tool in asyncio.run(server.mcp.list_tools())]

    assert sorted(TOOL_NAMES) == sorted(offered)


def test_prompt_fails_when_a_tool_is_missing():
    with pytest.raises(KeyError):
        _system_prompt(ME, {"get_sales_order": "get_sales_order"}, [])


def test_remembered_facts_follow_the_stable_part_of_the_prompt():
    names = {name: name for name in TOOL_NAMES}

    prompt = _system_prompt(ME, names, ["Wants deliveries on Tuesdays."])

    assert prompt.startswith(_system_prompt(ME, names, []))  # so the start stays eligible for implicit caching
    assert prompt.endswith("<user_context>\n- Wants deliveries on Tuesdays.\n</user_context>")


def test_history_is_cut_at_a_partner_message_so_tool_calls_keep_their_results():
    turn = [HumanMessage("status?"), AIMessage(""), ToolMessage("{}", tool_call_id="call-1"), AIMessage("In progress.")]

    recent = _recent_turns(turn * (MAX_HISTORY_TURNS + 3))

    assert len(recent) == MAX_HISTORY_TURNS * len(turn)
    assert recent[0].type == "human"


# --- Routing and the tool loop ----------------------------------------------------------------------------------------


class ScriptedModel:
    """Stands in for a Gemini model: gives its scripted replies in order and records what it was sent."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.requests: list[list] = []

    def bind_tools(self, tools: list) -> "ScriptedModel":
        return self

    def with_structured_output(self, schema: type, include_raw: bool = False) -> "ScriptedModel":
        return self

    async def ainvoke(self, messages: list) -> Any:
        self.requests.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class RecordingMemory:
    def __init__(self) -> None:
        self.remembered: list[tuple[str, str, str]] = []

    async def recall(self, actor_id: str, query: str) -> list[str]:
        return ["Wants deliveries on Tuesdays."]

    async def remember(self, actor_id: str, user_text: str, answer: str) -> None:
        self.remembered.append((actor_id, user_text, answer))


def routed_to(tier: str) -> dict[str, Any]:
    return {"raw": AIMessage(""), "parsed": RouteDecision(tier=tier, reason="scripted")}


def lookup(order_id: str) -> AIMessage:
    call = {"name": "get_sales_order", "args": {"sales_order_id": order_id}, "id": f"call-{order_id}"}
    return AIMessage("", tool_calls=[call])


def build_agent(
    monkeypatch: pytest.MonkeyPatch, router: list, simple: list = (), complex_: list = ()
) -> SimpleNamespace:
    """The served agent with scripted models, stub tools and in-memory conversation state."""
    models = {
        ROUTER_MODEL: ScriptedModel(*router),
        SIMPLE_MODEL: ScriptedModel(*simple),
        COMPLEX_MODEL: ScriptedModel(*complex_),
    }
    monkeypatch.setattr(graph, "build_model", lambda model, *settings: models[model])
    tool_calls: list[tuple[str, dict]] = []

    def stub(name: str) -> StructuredTool:
        async def run(sales_order_id: str = "") -> str:
            tool_calls.append((name, {"sales_order_id": sales_order_id}))
            return json.dumps({"sales_order_id": sales_order_id, "lifecycle_status": "In progress"})

        return StructuredTool.from_function(coroutine=run, name=name, description=name)

    memory = RecordingMemory()
    agent = SalesServiceAgent()
    agent._graph = graph.build_graph([stub(name) for name in TOOL_NAMES], memory, InMemorySaver())
    return SimpleNamespace(agent=agent, models=models, tool_calls=tool_calls, memory=memory)


def ask(built: SimpleNamespace, prompt: str, **overrides: str) -> dict[str, Any]:
    return asyncio.run(built.agent.async_query(**{**VALID, "prompt": prompt, **overrides}))


def test_simple_message_is_answered_by_the_simple_model_with_a_tool(monkeypatch):
    built = build_agent(monkeypatch, [routed_to("simple")], simple=[lookup("0500000318"), AIMessage("In progress.")])

    answer = ask(built, "Where is order 318?")

    assert (answer["response"], answer["tier"], answer["model"]) == ("In progress.", "simple", SIMPLE_MODEL)
    assert answer["tool_calls"] == {"get_sales_order": 1}
    assert built.tool_calls == [("get_sales_order", {"sales_order_id": "0500000318"})]
    assert built.models[COMPLEX_MODEL].requests == []
    system_prompt = built.models[SIMPLE_MODEL].requests[0][0].content
    assert f"partner {ME}" in system_prompt and "Wants deliveries on Tuesdays." in system_prompt
    assert built.memory.remembered == [(ME, "Where is order 318?", "In progress.")]


def test_router_failure_falls_back_to_the_complex_model(monkeypatch):
    built = build_agent(monkeypatch, [RuntimeError("quota")], complex_=[AIMessage("Here is the comparison.")])

    answer = ask(built, "Compare my open orders.")

    assert (answer["response"], answer["tier"], answer["model"]) == (
        "Here is the comparison.",
        "complex",
        COMPLEX_MODEL,
    )
    assert answer["route_reason"] == "router failed: RuntimeError"


def test_conversation_continues_in_a_session_and_is_private_to_the_partner(monkeypatch):
    replies = [AIMessage("In progress."), AIMessage("On 2026-10-24."), AIMessage("Which order?")]
    built = build_agent(monkeypatch, [routed_to("simple")] * 3, simple=replies)

    ask(built, "Where is order 318?")
    answer = ask(built, "And when was it due?")
    ask(built, "And when was it due?", actor_id="0100000003")  # the same session id, sent by another partner

    assert answer["tool_calls"] == {}  # only this turn's calls are reported
    simple = built.models[SIMPLE_MODEL]
    assert [message.type for message in simple.requests[1]] == ["system", "human", "ai", "human"]
    assert [message.type for message in simple.requests[2]] == ["system", "human"]


def test_model_failure_is_reported_without_details(monkeypatch):
    built = build_agent(monkeypatch, [routed_to("simple")], simple=[RuntimeError("credentials=secret")])

    answer = ask(built, "Where is order 318?")

    assert "temporarily unavailable" in answer["error"]
    assert "secret" not in json.dumps(answer)


# --- Tool calls (agent/tools.py) --------------------------------------------------------------------------------------


def tool_request(name: str = "get_sales_order", context: InvocationContext | None = None, **arguments: Any) -> Any:
    runtime = SimpleNamespace(context=context or InvocationContext(actor_id=ME, operation_id="operation-1"))
    return MCPToolCallRequest(name=name, args=arguments, server_name=SERVER_NAME, runtime=runtime)


def tool_result(payload: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))], structuredContent=payload)


class Backend:
    """A tool handler that gives its scripted results in order and records the requests it received."""

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.requests: list[MCPToolCallRequest] = []

    async def __call__(self, request: MCPToolCallRequest) -> CallToolResult:
        self.requests.append(request)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return tool_result(result)


async def no_sleep(delay: float) -> None:
    pass


def call(interceptor: Any, request: MCPToolCallRequest, backend: Backend) -> CallToolResult:
    return asyncio.run(interceptor(request, backend))


def test_requester_and_operation_come_from_the_invocation_not_the_model():
    binding, backend = RequestBinding(), Backend({})
    binding.keyed_tools = {"cancel_sales_order"}
    injected = {"sales_order_id": "0500000327", "requester_id": "0100000003", "idempotency_key": "made-up"}

    call(binding, tool_request("cancel_sales_order", **injected), backend)
    call(binding, tool_request("get_sales_order", sales_order_id="0500000318"), backend)

    assert backend.requests[0].args == {**injected, "requester_id": ME, "idempotency_key": "operation-1"}
    assert backend.requests[1].args == {"sales_order_id": "0500000318", "requester_id": ME}  # lookups have no key


def test_platform_fields_are_not_offered_to_the_model():
    schema = {
        "type": "object",
        "properties": {"sales_order_id": {}, "reason": {}, "requester_id": {}, "idempotency_key": {}},
        "required": ["sales_order_id", "requester_id", "idempotency_key"],
    }

    visible = _without_platform_fields(schema)

    assert list(visible["properties"]) == ["sales_order_id", "reason"]
    assert visible["required"] == ["sales_order_id"]


def test_transient_failures_are_retried_with_exponential_backoff():
    delays = []

    async def record(delay: float) -> None:
        delays.append(delay)

    backend = Backend(TRANSIENT)
    reliability = ToolReliability(max_attempts=4, base_delay=1, max_delay=60, sleep=record)

    result = call(reliability, tool_request(), backend)

    assert len(backend.requests) == 4  # gives up after max_attempts
    assert result.isError is True
    for attempt, delay in enumerate(delays):  # base_delay * 2**attempt, with 50-100 % jitter
        assert 2**attempt / 2 <= delay <= 2**attempt


def test_permanent_failures_are_not_retried_and_marked_as_failed():
    backend = Backend(PERMANENT)

    result = call(ToolReliability(sleep=no_sleep), tool_request(), backend)

    assert len(backend.requests) == 1
    assert result.isError is True  # so the tool span shows up as failed in traces
    assert result.structuredContent == PERMANENT  # the model still reads why


@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), TimeoutError()])
def test_transport_failures_are_retried_until_the_tool_answers(failure):
    backend = Backend(failure, {"sales_order_id": "0500000318"})

    result = call(ToolReliability(sleep=no_sleep), tool_request(), backend)

    assert len(backend.requests) == 2
    assert result.isError is False


def test_hung_tool_is_cut_off_by_the_timeout():
    async def hang(request: MCPToolCallRequest) -> CallToolResult:
        await asyncio.sleep(60)

    reliability = ToolReliability(max_attempts=2, timeout_seconds=0.01, sleep=no_sleep)

    result = asyncio.run(reliability(tool_request(), hang))

    assert (result.isError, result.content[0].text) == (True, "The tool timed out.")


def test_loop_guard_refuses_repeated_identical_calls():
    reliability, backend = ToolReliability(max_identical_calls=2), Backend({"note": "call again"})
    context = InvocationContext(actor_id=ME, operation_id="operation-1")

    results = [call(reliability, tool_request(context=context, sales_order_id="0500000327"), backend) for _ in range(3)]

    assert [result.isError for result in results] == [False, False, True]
    assert len(backend.requests) == 2
    assert "Do not call tools again" in results[2].content[0].text


def test_loop_guard_limits_the_tool_calls_of_one_request_only():
    reliability, backend = ToolReliability(max_tool_calls=2), Backend({})
    context = InvocationContext(actor_id=ME, operation_id="operation-1")

    results = [call(reliability, tool_request(context=context, sales_order_id=str(n)), backend) for n in range(3)]
    next_request = call(reliability, tool_request(sales_order_id="0"), backend)  # a new invocation context

    assert [result.isError for result in results] == [False, False, True]
    assert next_request.isError is False


def test_retries_do_not_count_as_loop_iterations_but_a_failed_call_is_not_repeated():
    reliability, backend = ToolReliability(max_attempts=3, max_identical_calls=2, sleep=no_sleep), Backend(TRANSIENT)
    context = InvocationContext(actor_id=ME, operation_id="operation-1")

    call(reliability, tool_request(context=context), backend)
    again = call(reliability, tool_request(context=context), backend)

    assert context.budget.calls == 2
    assert len(backend.requests) == 3  # all from the first call: the model's repeat is refused right away
    assert "Do not call tools again" in again.content[0].text


def test_change_recovered_by_a_retry_is_reported_as_new():
    backend = Backend(TRANSIENT, {"sales_order_id": "0500000328", "status": "Cancelled", "replayed": True})

    result = call(ToolReliability(sleep=no_sleep), tool_request("cancel_sales_order"), backend)

    assert result.structuredContent["replayed"] is False
    assert json.loads(result.content[0].text)["replayed"] is False


def test_replay_of_an_earlier_request_stays_marked():
    backend = Backend({"sales_order_id": "0500000328", "status": "Cancelled", "replayed": True})

    result = call(ToolReliability(sleep=no_sleep), tool_request("cancel_sales_order"), backend)

    assert result.structuredContent["replayed"] is True


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ReadTimeout("slow"), True),
        (httpx.HTTPStatusError("", request=None, response=httpx.Response(503)), True),
        (httpx.HTTPStatusError("", request=None, response=httpx.Response(403)), False),
        (ExceptionGroup("mcp", [ValueError("x"), httpx.ConnectError("refused")]), True),
        (ValueError("bad arguments"), False),
    ],
)
def test_is_transient(error, expected):
    assert is_transient(error) is expected
