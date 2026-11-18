"""Settings shared by the agent and the deployment (infra/), kept free of heavy imports."""

import os

# The model tiers. Prices are USD per 1M input/output tokens on the paid tier, checked on 2026-11-05.
#
# Decides which tier answers. Gemini Decision 1 does not generate text: it scores a closed set of options with
# calibrated probabilities, which is all routing needs. It is only served by the Gemini Developer API (Google AI
# Studio), so unlike the models below it is called with an API key, see agent/router.py. $0.10 input / $0 output.
ROUTER_MODEL = os.getenv("ROUTER_MODEL") or "gemini-decision-1"
# The router sends a message to the simple tier only when it is at least this sure that is enough, and otherwise to the
# complex one: a wrongly simple answer costs more than a wrongly strong one.
ROUTER_MIN_CONFIDENCE = 0.7
# Lookups and short answers: one step up from the router, and its $2.50 output is what makes routing worthwhile.
SIMPLE_MODEL = os.getenv("SIMPLE_MODEL") or "gemini-3.5-flash-lite"  # $0.30 / $2.50
# Multi-step reasoning and anything that changes data. gemini-4-argon ($4 / $20 from December) is not needed for this.
COMPLEX_MODEL = os.getenv("COMPLEX_MODEL") or "gemini-3.8-flash"  # $0.75 / $3.75, $1.50 / $7.50 from 2027-01-01

# Gemini 3 models answer on the global and multi-region endpoints only: us-central1 returns 404 for them.
GEMINI_LOCATION = os.getenv("GEMINI_LOCATION") or "global"
# Keeps remembered customer facts at rest in one region group, next to the Cloud SQL data.
MEMORY_LOCATION = os.getenv("MEMORY_LOCATION") or "us"

# Longer than a Cloud Run cold start, so scale-to-zero does not look like an outage to the agent.
TOOL_TIMEOUT_SECONDS = 15
MAX_HISTORY_TURNS = 10  # earlier turns stay in the checkpoint but are not sent to the model again

# Long-term memory is kept per partner; Memory Bank isolates every scope from all others.
MEMORY_SCOPE_KEY = "actor_id"
MEMORY_TOPIC = "ordering_preferences"
MEMORY_TOPIC_DESCRIPTION = (
    "How this business partner likes to work: preferred contact person or channel, delivery windows and locations, "
    "product categories or models they usually order, and how they want answers presented. Not order details, "
    "amounts or statuses, which change and are always read from the order system."
)
