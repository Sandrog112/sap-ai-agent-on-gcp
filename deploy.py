"""Deploy, exercise and tear down the sales agent stack.

    uv run --env-file .env python deploy.py deploy
    uv run --env-file .env python deploy.py invoke "Which of my orders are overdue?" --actor 0100000022
    uv run --env-file .env python deploy.py call-tool get_sales_order '{"sales_order_id": "0500000318"}'
    uv run --env-file .env python deploy.py destroy

`deploy` enables the APIs and creates or updates the service accounts, the Cloud SQL databases and grants, the MCP tool
server on Cloud Run, Memory Bank, the log-based metrics and the agent on Agent Runtime, and writes the resource
identifiers to deploy-outputs.json. Each step lives in infra/, one module per service.
"""

import argparse
import asyncio
import json
import os
import subprocess
import uuid
from typing import Any

from agent.config import COMPLEX_MODEL, GEMINI_LOCATION, MEMORY_LOCATION, ROUTER_MODEL, SIMPLE_MODEL
from agent.tools import IDEMPOTENCY_FIELD, REQUESTER_FIELD, SERVER_NAME, tool_client
from infra.config import AUTO_APPROVAL_LIMIT, BUSINESS_DATE, CHECKPOINT_DATABASE, OUTPUTS_FILE, TOOL_SERVICE
from infra.data_store import deploy_data_store, destroy_data_store
from infra.gcp import (
    agent_platform,
    database_user,
    enable_apis,
    instance_connection_name,
    project_id,
    region,
    service_account_email,
    setting,
    step,
)
from infra.iam import deploy_service_accounts, destroy_service_accounts
from infra.memory import deploy_memory_bank, destroy_memory_bank
from infra.observability import destroy_observability, enable_observability
from infra.runtime import deploy_runtime, destroy_runtime
from infra.tool_server import deploy_tool_server, destroy_tool_server

# Optional settings the agent reads as well, passed on only when set in .env.
OPTIONAL_AGENT_SETTINGS = ("ROUTER_MODEL", "SIMPLE_MODEL", "COMPLEX_MODEL", "GEMINI_LOCATION", "MEMORY_LOCATION")


def deploy() -> None:
    step("Google Cloud APIs")
    enable_apis()

    step("Service accounts")
    deploy_service_accounts()

    step("Cloud SQL: databases, sales data, ledger and grants")
    deploy_data_store()

    step("MCP tool server on Cloud Run (builds from source; takes a few minutes)")
    tool_server_url = deploy_tool_server(_operator())

    step("Memory Bank")
    memory_bank = deploy_memory_bank()

    step("Observability (log-based metrics)")
    enable_observability()

    step("Agent Runtime (installs the dependencies; takes a few minutes)")
    environment = {
        "GCP_PROJECT_ID": project_id(),
        "TOOL_SERVER_URL": tool_server_url,
        "MEMORY_BANK_NAME": memory_bank,
        "CLOUD_SQL_CONNECTION": instance_connection_name(),
        "CHECKPOINT_DB_NAME": CHECKPOINT_DATABASE,
        "DB_IAM_USER": database_user("runtime"),
        "BUSINESS_DATE": BUSINESS_DATE,
        "TELEMETRY_ENABLED": "true",
        **{name: os.environ[name] for name in OPTIONAL_AGENT_SETTINGS if os.getenv(name)},
    }
    runtime_name = deploy_runtime(service_account_email("runtime"), environment)

    outputs = {
        "project_id": project_id(),
        "region": region(),
        "runtime_name": runtime_name,
        "tool_server_url": tool_server_url,
        "tool_service": TOOL_SERVICE,
        "memory_bank_name": memory_bank,
        "memory_location": MEMORY_LOCATION,
        "cloud_sql_instance": instance_connection_name(),
        "database": setting("DB_NAME"),
        "checkpoint_database": CHECKPOINT_DATABASE,
        "models": {"router": ROUTER_MODEL, "simple": SIMPLE_MODEL, "complex": COMPLEX_MODEL},
        "gemini_location": GEMINI_LOCATION,
        "auto_approval_limit": AUTO_APPROVAL_LIMIT,
    }
    OUTPUTS_FILE.write_text(json.dumps(outputs, indent=2) + "\n")
    print(json.dumps(outputs, indent=2))


def destroy() -> None:
    """Delete the stack. The Cloud SQL instance and the sales data are kept, because they existed before the stack and a
    deleted instance cannot be recovered, and so are logs and traces, which remain available as evidence."""
    step("Agent Runtime")
    destroy_runtime()
    step("MCP tool server")
    destroy_tool_server()
    step("Memory Bank")
    destroy_memory_bank()
    step("Log-based metrics")
    destroy_observability()
    step("Cloud SQL: ledger, conversation state and database users")
    destroy_data_store()
    step("Service accounts")
    destroy_service_accounts()
    OUTPUTS_FILE.unlink(missing_ok=True)


def load_outputs() -> dict[str, Any]:
    if not OUTPUTS_FILE.exists():
        raise FileNotFoundError(f"{OUTPUTS_FILE.name} not found; run `python deploy.py deploy` first.")
    return json.loads(OUTPUTS_FILE.read_text())


def invoke_agent(
    prompt: str, actor_id: str, session_id: str | None = None, operation_id: str | None = None
) -> dict[str, Any]:
    """Send one message to the deployed agent.

    `operation_id` is the idempotency key of the changes the message makes: pass the same one to retry a request in any
    session; a new one is generated otherwise.
    """
    runtime = agent_platform(region()).runtimes.get(name=load_outputs()["runtime_name"])
    return asyncio.run(
        runtime.async_query(
            prompt=prompt,
            actor_id=actor_id,
            session_id=session_id or f"session-{uuid.uuid4()}",
            operation_id=operation_id or f"operation-{uuid.uuid4()}",
        )
    )


def call_tool(tool: str, arguments: dict[str, Any], actor_id: str, operation_id: str | None = None) -> dict[str, Any]:
    """Call a tool directly over MCP, bypassing the model; Cloud Run's IAM check and the policy still apply.

    Like the agent runtime, this trusted caller binds the call to `actor_id` as the requester.
    """

    async def call() -> dict[str, Any]:
        bound = {**arguments, REQUESTER_FIELD: actor_id}
        if operation_id:
            bound[IDEMPOTENCY_FIELD] = operation_id
        async with tool_client(load_outputs()["tool_server_url"]).session(SERVER_NAME) as session:
            return (await session.call_tool(tool, bound)).model_dump(mode="json")

    return asyncio.run(call())


def _operator() -> str:
    """The deploying identity, as an IAM member: it may call the tool server directly with `call-tool`."""
    command = ["gcloud", "config", "get-value", "account"]
    account = subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()
    kind = "serviceAccount" if account.endswith(".gserviceaccount.com") else "user"
    return f"{kind}:{account}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("deploy", help="create or update the whole stack")
    chat = commands.add_parser("invoke", help="send one message to the deployed agent")
    chat.add_argument("prompt")
    chat.add_argument("--actor", default="0100000022", help="signed-in partner id (default: 0100000022)")
    chat.add_argument("--session", help="session id, to continue a conversation; a new session if omitted")
    chat.add_argument("--operation", help="operation id: reuse it to retry a request, a new one if omitted")
    direct = commands.add_parser("call-tool", help="call a tool directly, without the model")
    direct.add_argument("tool", help="for example get_sales_order or cancel_sales_order")
    direct.add_argument("arguments", type=json.loads, help="tool arguments as a JSON object")
    direct.add_argument("--actor", default="0100000022", help="signed-in partner id (default: 0100000022)")
    direct.add_argument("--operation", help="idempotency key, required by the tools that change data")
    commands.add_parser("destroy", help="delete the stack, keeping the Cloud SQL instance and its sales data")
    args = parser.parse_args()

    if args.command == "deploy":
        deploy()
    elif args.command == "invoke":
        print(json.dumps(invoke_agent(args.prompt, args.actor, args.session, args.operation), indent=2))
    elif args.command == "call-tool":
        print(json.dumps(call_tool(args.tool, args.arguments, args.actor, args.operation), indent=2, default=str))
    else:
        destroy()


if __name__ == "__main__":
    main()
