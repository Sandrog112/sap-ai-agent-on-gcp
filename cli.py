"""Talk to the deployed sales agent. The stack itself is deployed with Terraform, see terraform/ and the README.

    uv run python cli.py invoke "Which of my orders are overdue?" --actor 0100000022
    uv run python cli.py call-tool get_sales_order '{"sales_order_id": "0500000318"}'

Where the stack is comes from `terraform output`, so run these after `terraform apply`.
"""

import argparse
import asyncio
import functools
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import agentplatform

from agent.tools import IDEMPOTENCY_FIELD, REQUESTER_FIELD, SERVER_NAME, tool_client

TERRAFORM_DIR = Path(os.getenv("TERRAFORM_DIR") or Path(__file__).resolve().parent / "terraform")


class NotDeployedError(RuntimeError):
    pass


@functools.cache
def load_outputs() -> dict[str, Any]:
    """The stack's outputs (terraform/outputs.tf), as Terraform last applied them."""
    try:
        result = subprocess.run(
            ["terraform", f"-chdir={TERRAFORM_DIR}", "output", "-json"], capture_output=True, text=True, check=True
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise NotDeployedError(f"Could not read the Terraform outputs of {TERRAFORM_DIR}: {error}") from error
    outputs = {name: output["value"] for name, output in json.loads(result.stdout).items()}
    if "runtime_name" not in outputs:
        raise NotDeployedError("The stack is not deployed: run `terraform apply` in terraform/ first.")
    return outputs


def invoke_agent(
    prompt: str, actor_id: str, session_id: str | None = None, operation_id: str | None = None
) -> dict[str, Any]:
    """Send one message to the deployed agent.

    `operation_id` is the idempotency key of the changes the message makes: pass the same one to retry a request in any
    session; a new one is generated otherwise.
    """
    outputs = load_outputs()
    client = agentplatform.Client(project=outputs["project_id"], location=outputs["region"])
    runtime = client.runtimes.get(name=outputs["runtime_name"])
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
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
    args = parser.parse_args()

    if args.command == "invoke":
        print(json.dumps(invoke_agent(args.prompt, args.actor, args.session, args.operation), indent=2))
    else:
        print(json.dumps(call_tool(args.tool, args.arguments, args.actor, args.operation), indent=2, default=str))


if __name__ == "__main__":
    main()
