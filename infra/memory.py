"""Memory Bank: long-term partner preferences, extracted from conversations and isolated per partner."""

from agent.config import MEMORY_LOCATION, MEMORY_TOPIC, MEMORY_TOPIC_DESCRIPTION, SIMPLE_MODEL
from infra.config import MEMORY_BANK_NAME
from infra.gcp import agent_platform, project_id

# One example of what to keep and one of what to ignore, so a question about an order does not become a "preference".
EXTRACTION_EXAMPLES = [
    {
        "conversation_source": {
            "events": [
                {
                    "content": {
                        "role": "user",
                        "parts": [{"text": "Please always deliver on Tuesdays, our warehouse is closed otherwise."}],
                    }
                }
            ]
        },
        "generated_memories": [{"fact": "The partner wants deliveries on Tuesdays because their warehouse closes."}],
    },
    {
        "conversation_source": {
            "events": [{"content": {"role": "user", "parts": [{"text": "What is the status of order 0500000318?"}]}}]
        },
        "generated_memories": [],
    },
]


def deploy_memory_bank() -> str:
    """Create the Memory Bank if there is none yet; returns its resource name."""
    memory_banks = agent_platform(MEMORY_LOCATION).memory_banks
    existing = _find(memory_banks)
    if existing:
        return existing
    model = f"projects/{project_id()}/locations/{MEMORY_LOCATION}/publishers/google/models/{SIMPLE_MODEL}"
    config = {
        "generation_config": {"model": model},
        "unstructured_memory_configs": [
            {
                "memory_topics": [
                    {"managed_memory_topic": {"managed_topic_enum": "USER_PREFERENCES"}},
                    {"managed_memory_topic": {"managed_topic_enum": "EXPLICIT_INSTRUCTIONS"}},
                    {"custom_memory_topic": {"label": MEMORY_TOPIC, "description": MEMORY_TOPIC_DESCRIPTION}},
                ],
                "generate_memories_examples": EXTRACTION_EXAMPLES,
            }
        ],
    }
    return memory_banks.create(
        managed_semantic_memory_config=config,
        config={"display_name": MEMORY_BANK_NAME},
    ).name


def destroy_memory_bank() -> None:
    memory_banks = agent_platform(MEMORY_LOCATION).memory_banks
    name = _find(memory_banks)
    if name:
        memory_banks.delete(name=name, force=True)


def _find(memory_banks) -> str | None:
    return next((bank.name for bank in memory_banks.list() if bank.display_name == MEMORY_BANK_NAME), None)
