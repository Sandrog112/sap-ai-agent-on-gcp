"""The LangGraph agent: recall memories, route to a model tier, run that model and its tools, store new memories.

    recall -> route -> agent <-> tools
                         |
                      remember

The router picks one tier per customer message, and that model answers the whole message. Staying on one model within a
turn is required by Gemini: it validates the thought signatures of a turn's function calls against that turn's history.
"""

import logging
import os
from datetime import UTC, datetime
from typing import Any, Literal

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime
from opentelemetry import trace
from pydantic import BaseModel, Field

from agent.config import COMPLEX_MODEL, GEMINI_LOCATION, MAX_HISTORY_TURNS, ROUTER_MODEL, SIMPLE_MODEL
from agent.memory import PartnerMemory
from agent.prompts import ROUTER_PROMPT, SYSTEM_PROMPT
from agent.tools import InvocationContext

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

SIMPLE, COMPLEX = "simple", "complex"
Tier = Literal["simple", "complex"]
ROUTER_CONTEXT_MESSAGES = 6
ROUTER_MESSAGE_CHARACTERS = 500


class AgentState(MessagesState):
    tier: Tier
    route_reason: str
    memories: list[str]


class RouteDecision(BaseModel):
    """Which model should answer the latest message."""

    tier: Tier = Field(description="simple for one lookup and a short reply, complex for anything more.")
    reason: str = Field(description="Why, in at most 12 words.")


def build_model(model: str, thinking_level: str, max_tokens: int, project_id: str | None) -> ChatGoogleGenerativeAI:
    """A Gemini model on Agent Platform, called with the service account's credentials through the global endpoint."""
    return ChatGoogleGenerativeAI(
        model=model,
        vertexai=True,
        project=project_id,
        location=GEMINI_LOCATION,
        thinking_level=thinking_level,
        max_tokens=max_tokens,
        timeout=60,
        max_retries=3,
    )


def build_graph(
    tools: list[BaseTool],
    memory: PartnerMemory,
    checkpointer: BaseCheckpointSaver,
    project_id: str | None = None,
) -> CompiledStateGraph:
    router = build_model(ROUTER_MODEL, "minimal", 512, project_id).with_structured_output(
        RouteDecision, include_raw=True
    )
    # Thinking is billed as output, so each tier only gets the level its work needs.
    models = {
        SIMPLE: build_model(SIMPLE_MODEL, "low", 4096, project_id).bind_tools(tools),
        COMPLEX: build_model(COMPLEX_MODEL, "medium", 8192, project_id).bind_tools(tools),
    }
    tool_names = {tool.name: tool.name for tool in tools}

    async def recall(state: AgentState, runtime: Runtime[InvocationContext]) -> dict[str, Any]:
        return {"memories": await memory.recall(runtime.context.actor_id, state["messages"][-1].text)}

    async def route(state: AgentState) -> dict[str, Any]:
        """Pick the tier. A router failure must not fail the request, so it falls back to the stronger model."""
        with tracer.start_as_current_span("route_message", attributes={"gen_ai.request.model": ROUTER_MODEL}) as span:
            messages = [SystemMessage(ROUTER_PROMPT), HumanMessage(_router_transcript(state["messages"]))]
            try:
                decision = await router.ainvoke(messages)
                _record_usage(span, decision["raw"])
                tier, reason = decision["parsed"].tier, decision["parsed"].reason
            except Exception as error:
                logger.exception("router failed, using the complex tier")
                span.record_exception(error)
                tier, reason = COMPLEX, f"router failed: {type(error).__name__}"
            span.set_attributes({"routing.tier": tier, "routing.reason": reason})
            logger.info("model_route", extra={"tier": tier, "reason": reason})
            return {"tier": tier, "route_reason": reason}

    async def agent(state: AgentState, runtime: Runtime[InvocationContext]) -> dict[str, Any]:
        tier = state["tier"]
        system = _system_prompt(runtime.context.actor_id, tool_names, state.get("memories") or [])
        messages = [SystemMessage(system), *_recent_turns(state["messages"])]
        with tracer.start_as_current_span(f"chat {model_of(tier)}") as span:
            span.set_attributes({"gen_ai.request.model": model_of(tier), "routing.tier": tier})
            response = await models[tier].ainvoke(messages)
            _record_usage(span, response)
        return {"messages": [response]}

    async def remember(state: AgentState, runtime: Runtime[InvocationContext]) -> dict[str, Any]:
        user_text = next(message.text for message in reversed(state["messages"]) if message.type == "human")
        await memory.remember(runtime.context.actor_id, user_text, state["messages"][-1].text)
        return {}

    builder = StateGraph(AgentState, context_schema=InvocationContext)
    builder.add_node("recall", recall)
    builder.add_node("route", route)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(tools))
    builder.add_node("remember", remember)
    builder.add_edge(START, "recall")
    builder.add_edge("recall", "route")
    builder.add_edge("route", "agent")
    builder.add_conditional_edges("agent", _next_after_agent, ["tools", "remember"])
    builder.add_edge("tools", "agent")
    builder.add_edge("remember", END)
    return builder.compile(checkpointer=checkpointer)


def model_of(tier: Tier) -> str:
    return SIMPLE_MODEL if tier == SIMPLE else COMPLEX_MODEL


def _next_after_agent(state: AgentState) -> str:
    return "tools" if state["messages"][-1].tool_calls else "remember"


def _system_prompt(actor_id: str, tool_names: dict[str, str], memories: list[str]) -> str:
    """The prompt with each tool's name; a missing tool fails the invocation, not the model. The memories go last, so
    the stable start of the prompt stays eligible for Gemini's implicit caching."""
    prompt = SYSTEM_PROMPT.format(actor_id=actor_id, business_date=_business_date(), **tool_names)
    if memories:
        facts = "\n".join(f"- {fact}" for fact in memories)
        prompt += f"\n\n<user_context>\n{facts}\n</user_context>"
    return prompt


def _business_date() -> str:
    return os.getenv("BUSINESS_DATE") or datetime.now(UTC).date().isoformat()


def _recent_turns(messages: list[AnyMessage]) -> list[AnyMessage]:
    """The last turns of the conversation, cut at a customer message so every tool call keeps its result."""
    starts = [index for index, message in enumerate(messages) if message.type == "human"]
    return messages[starts[-MAX_HISTORY_TURNS] :] if len(starts) > MAX_HISTORY_TURNS else messages


def _router_transcript(messages: list[AnyMessage]) -> str:
    """The recent conversation as plain text. The router needs it to read follow-ups like "yes, go ahead", and leaving
    out tool results keeps this cheapest call cheap."""
    spoken = [message for message in messages if message.type == "human" or (message.type == "ai" and message.text)]
    lines = [
        f"{'Partner' if message.type == 'human' else 'Agent'}: {message.text[:ROUTER_MESSAGE_CHARACTERS]}"
        for message in spoken[-ROUTER_CONTEXT_MESSAGES:]
    ]
    return (
        "<conversation>\n" + "\n".join(lines) + "\n</conversation>\n\nChoose the tier for the latest Partner message."
    )


def _record_usage(span: trace.Span, message: AIMessage) -> None:
    """Token counts on the span, so a trace shows what each tier cost."""
    usage = message.usage_metadata or {}
    span.set_attributes(
        {
            "gen_ai.usage.input_tokens": usage.get("input_tokens", 0),
            "gen_ai.usage.output_tokens": usage.get("output_tokens", 0),
        }
    )
