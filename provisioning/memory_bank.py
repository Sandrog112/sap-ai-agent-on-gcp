"""Memory Bank: long-term partner preferences, extracted from conversations and isolated per partner.

No Terraform resource creates a Memory Bank, and its id is assigned by the service, so terraform/memory_bank.tf calls
this module:

    python -m provisioning.memory_bank ensure   create the bank unless one with this display name exists
    python -m provisioning.memory_bank lookup   print {"name": ...} as Terraform's `external` data source expects
    python -m provisioning.memory_bank delete   delete the bank and its memories

Settings from the environment: GCP_PROJECT_ID, MEMORY_BANK_DISPLAY_NAME, and optionally MEMORY_LOCATION and
SIMPLE_MODEL (the model that extracts memories), which default as in agent/config.py.
"""

import argparse
import contextlib
import json
import os
import sys

from agent.config import MEMORY_LOCATION, MEMORY_TOPIC, MEMORY_TOPIC_DESCRIPTION, SIMPLE_MODEL

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


def _setting(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Set {name}: Terraform passes it to this module, see terraform/memory_bank.tf.")
    return value


def _memory_banks():
    import agentplatform

    return agentplatform.Client(project=_setting("GCP_PROJECT_ID"), location=MEMORY_LOCATION).memory_banks


def find(memory_banks) -> str | None:
    display_name = _setting("MEMORY_BANK_DISPLAY_NAME")
    return next((bank.name for bank in memory_banks.list() if bank.display_name == display_name), None)


def ensure() -> str:
    """Create the Memory Bank if there is none yet; returns its resource name. An existing one is left as it is,
    because changing the bank must never cost the partners' memories."""
    memory_banks = _memory_banks()
    existing = find(memory_banks)
    if existing:
        return existing
    model = f"projects/{_setting('GCP_PROJECT_ID')}/locations/{MEMORY_LOCATION}/publishers/google/models/{SIMPLE_MODEL}"
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
        config={"display_name": _setting("MEMORY_BANK_DISPLAY_NAME")},
    ).name


def delete() -> None:
    memory_banks = _memory_banks()
    name = find(memory_banks)
    if name:
        memory_banks.delete(name=name, force=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["ensure", "lookup", "delete"])
    command = parser.parse_args().command
    # Terraform reads stdout as JSON, so whatever the libraries print goes to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        if command == "ensure":
            result = {"name": ensure()}
        elif command == "lookup":
            result = {"name": find(_memory_banks()) or ""}
        else:
            delete()
            result = {}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
