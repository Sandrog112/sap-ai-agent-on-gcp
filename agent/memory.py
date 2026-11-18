"""Long-term memory per partner in Memory Bank, and conversation state per session in Cloud SQL.

Both are outside the sales database on purpose: the agent's service account can reach neither the orders nor any other
partner's memories directly.
"""

import asyncio
import logging
import os
from typing import Any

import agentplatform
import psycopg
from google.cloud.sql.connector import Connector
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from agent.config import MEMORY_LOCATION, MEMORY_SCOPE_KEY
from shared.telemetry import current_trace_id

logger = logging.getLogger(__name__)

MEMORIES_PER_REQUEST = 5
# What the Postgres checkpointer requires of its connections.
_CONNECTION_SETTINGS = {"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0}


class PartnerMemory:
    """Memory Bank, scoped to one partner per call. A memory outage degrades answers but never fails a request."""

    def __init__(self, memory_bank_name: str | None, project_id: str | None = None) -> None:
        self._name = memory_bank_name
        self._client = (
            agentplatform.Client(project=project_id, location=MEMORY_LOCATION).aio if memory_bank_name else None
        )

    async def recall(self, actor_id: str, query: str) -> list[str]:
        """The remembered facts most relevant to this message."""
        if self._client is None:
            return []
        try:
            pager = await self._client.memory_banks.memories.retrieve(
                name=self._name,
                scope={MEMORY_SCOPE_KEY: actor_id},
                similarity_search_params={"search_query": query, "top_k": MEMORIES_PER_REQUEST},
            )
            return [item.memory.fact async for item in pager]
        except Exception:
            logger.exception("trace=<%s> | could not read memories", current_trace_id())
            return []

    async def remember(self, actor_id: str, user_text: str, answer: str) -> None:
        """Start extracting what is worth keeping from this exchange, without waiting for the result. Tool results
        stay out of it: order data belongs in the order system, not in memory."""
        if self._client is None:
            return
        try:
            await self._client.memory_banks.memories.generate(
                name=self._name,
                direct_contents_source={
                    "events": [
                        {"content": {"role": "user", "parts": [{"text": user_text}]}},
                        {"content": {"role": "model", "parts": [{"text": answer}]}},
                    ]
                },
                scope={MEMORY_SCOPE_KEY: actor_id},
                config={"wait_for_completion": False},
            )
        except Exception:
            logger.exception("trace=<%s> | could not store memories", current_trace_id())


class ThreadedPostgresSaver(PostgresSaver):
    """The official Postgres checkpointer, usable from the async graph.

    The Cloud SQL connector only offers psycopg's sync driver, so the three async methods a graph run calls (read the
    latest checkpoint, write one, write a step's pending writes) run the sync ones in a worker thread, which keeps the
    event loop free.
    """

    async def aget_tuple(self, config: Any) -> Any:
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config: Any, writes: Any, task_id: str, task_path: str = "") -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)


def _cloud_sql_connection_class() -> type[psycopg.Connection]:
    """A connection class for the pool, whose connections sign in with the service account through IAM database
    authentication, so no password exists to leak. It tells the pool how to connect; the connector builds them."""
    # "lazy" refreshes certificates on demand, because serverless CPU is throttled between requests.
    connector = Connector(refresh_strategy="lazy")

    class CloudSqlConnection(psycopg.Connection):
        @classmethod
        def connect(cls, conninfo: str = "", **kwargs: Any) -> psycopg.Connection:
            return connector.connect(
                os.environ["CLOUD_SQL_CONNECTION"],
                "psycopg",
                user=os.environ["DB_IAM_USER"],
                db=os.environ["CHECKPOINT_DB_NAME"],
                enable_iam_auth=True,
                **kwargs,
            )

    return CloudSqlConnection


async def open_checkpointer() -> ThreadedPostgresSaver:
    """The checkpointer on a small connection pool. Its tables already exist, see infra/data_store.py: the runtime has
    no right to change the schema. CHECKPOINT_DATABASE_URL points a local run at any other Postgres instead."""
    settings: dict[str, Any] = {"min_size": 1, "max_size": 4, "kwargs": _CONNECTION_SETTINGS, "open": False}
    settings["check"] = ConnectionPool.check_connection
    url = os.getenv("CHECKPOINT_DATABASE_URL")
    if url:
        pool = ConnectionPool(url, **settings)
    else:
        pool = ConnectionPool(connection_class=_cloud_sql_connection_class(), **settings)
    await asyncio.to_thread(pool.open, wait=True, timeout=60)
    return ThreadedPostgresSaver(pool)
