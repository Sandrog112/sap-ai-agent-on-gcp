"""The router: Gemini Decision 1 picks the model tier that answers a message.

The Decisions API is not a chat model. It scores a closed set of options against an input in one pass and returns
calibrated probabilities, with no generated text, which is what routing needs and why it is cheap and fast. It is only
served by the Gemini Developer API, so it is called with an API key instead of the service account the other models
use. The key lives in Secret Manager, and only the runtime's service account may read it.
"""

import asyncio
import os
from dataclasses import dataclass
from typing import Literal

from google import genai
from google.cloud import secretmanager
from google.genai import types

from agent.config import ROUTER_MIN_CONFIDENCE, ROUTER_MODEL
from agent.prompts import ROUTER_OPTIONS, ROUTER_QUESTION

Tier = Literal["simple", "complex"]
SIMPLE, COMPLEX = "simple", "complex"
QUESTION_KEY = "tier"
# A slow router must not slow every message down: its failure only costs a stronger model.
REQUEST_TIMEOUT_MS = 10_000


@dataclass(frozen=True)
class RouteDecision:
    tier: Tier
    reason: str
    input_tokens: int = 0


class TierRouter:
    def __init__(self, api_key: str, model: str = ROUTER_MODEL, base_url: str | None = None) -> None:
        options = types.HttpOptions(
            api_version="v1beta",  # the Decisions API is not in the stable version yet
            timeout=REQUEST_TIMEOUT_MS,
            retry_options=types.HttpRetryOptions(attempts=2),
            base_url=base_url,
        )
        self._client = genai.Client(api_key=api_key, http_options=options)
        self._model = model

    async def route(self, conversation: str) -> RouteDecision:
        """Pick the tier for the latest message of `conversation`. Raises if the API fails; the caller falls back."""
        result = await self._client.aio.decisions.create(
            model=self._model,
            input=conversation,
            questions={QUESTION_KEY: {"type": "choice", "instructions": ROUTER_QUESTION, "criteria": ROUTER_OPTIONS}},
        )
        sure_it_is_simple = result.answers[QUESTION_KEY].probabilities.get(SIMPLE, 0.0)
        input_tokens = (result.usage and result.usage.total_input_tokens) or 0
        if sure_it_is_simple >= ROUTER_MIN_CONFIDENCE:
            return RouteDecision(SIMPLE, f"P(simple)={sure_it_is_simple:.2f}", input_tokens)
        return RouteDecision(COMPLEX, f"P(simple)={sure_it_is_simple:.2f}, below {ROUTER_MIN_CONFIDENCE}", input_tokens)


async def load_gemini_api_key(project_id: str) -> str:
    """The Gemini API key: GEMINI_API_KEY for a local run, otherwise the secret that Terraform created, read with the
    runtime's own service account."""
    if os.getenv("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    version = f"projects/{project_id}/secrets/{os.environ['GEMINI_API_KEY_SECRET']}/versions/latest"
    response = await asyncio.to_thread(secretmanager.SecretManagerServiceClient().access_secret_version, name=version)
    return response.payload.data.decode().strip()
