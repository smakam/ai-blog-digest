"""Jev classification via the official TypeSafe SDK, pointed at OpenRouter.

One Jev call per item answers two questions about the same state:
  - is_ai (noul): probability the post is about AI (embodied AI / robotics count as no);
  - worthiness (score): an ordered rubric, normalized here to 0-1.

Switching to the native TypeSafe API is a base-URL and key change only (JEV_BASE_URL + key).
"""

from __future__ import annotations

import logging
import time

from typesafe_sdk import Noul, Score, TypeSafeClient

from digest.config import JevConfig
from digest.models import Item

log = logging.getLogger(__name__)

# Rough chars-per-token for English prose; keeps us well under Jev's 32K context.
CHARS_PER_TOKEN = 4

IS_AI = Noul(
    instructions=(
        "Is this blog post primarily about artificial intelligence: machine learning, LLMs, "
        "generative AI, AI agents, AI infrastructure, AI research, or AI products? "
        "Embodied AI, robotics, self-driving hardware, and humanoids count as NO."
    ),
    criteria={
        "true": "The post's main subject is AI/ML software, models, research, tooling, or products.",
        "false": "The post is not mainly about AI, or it is about robotics / embodied AI.",
    },
)

# Ordered lowest to highest. Normalized score = level / (len - 1).
WORTHINESS_LEVELS = [
    "Not worth reading: funding or acquisition news, listicles, prompt-tip posts, "
    "generic opinion or hype pieces, pricing changes, marketing fluff.",
    "Low value: minor feature updates, shallow news recaps, or thin commentary with no technical substance.",
    "Moderate: useful but incremental — a tutorial on known techniques, a modest product update, "
    "or informed commentary with some concrete specifics.",
    "High value: substantive technical content — architecture detail, agent infrastructure, LLMOps practice, "
    "or a significant product launch (a new model, a new agent or developer framework, a major platform capability).",
    "Exceptional: genuinely new technical insight — original research results, deep architecture write-ups, "
    "or first-hand engineering lessons that a practitioner would learn something new from.",
]

WORTHINESS = Score(
    instructions=(
        "How worthwhile is this post for an experienced AI engineer who wants technical insight and "
        "significant launches, not news noise? Judge the content only."
    ),
    criteria=WORTHINESS_LEVELS,
)

QUESTIONS = {"is_ai": IS_AI, "worthiness": WORTHINESS}


def build_state(item: Item, max_input_tokens: int) -> dict:
    """The state Jev sees. Content only: author and source are deliberately excluded (spec §2)."""
    max_chars = max_input_tokens * CHARS_PER_TOKEN
    text = item.text
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return {"title": item.title, "text": text, "truncated": truncated}


class JevClassifier:
    def __init__(self, config: JevConfig, api_key: str, client: TypeSafeClient | None = None) -> None:
        self.config = config
        self.client = client or TypeSafeClient(
            api_key=api_key,
            base_url=config.base_url,
            model=config.model,
            timeout=config.timeout_seconds,
        )

    def close(self) -> None:
        self.client.close()

    def classify(self, item: Item) -> None:
        """Populate item.is_ai / item.worthiness. Raises on API errors."""
        started = time.perf_counter()
        result = self.client.system_one(
            state=build_state(item, self.config.max_input_tokens),
            questions=QUESTIONS,
        )
        item.jev_ms = (time.perf_counter() - started) * 1000

        item.is_ai = result.nouls["is_ai"].noul
        worth = result.scores["worthiness"]
        item.worthiness = worth.score / (len(WORTHINESS_LEVELS) - 1)
        item.worthiness_confidence = worth.confidence
        item.jev_input_tokens = result.usage.input_tokens
        item.jev_cost = _usage_cost(result)


def _usage_cost(result) -> float | None:
    # OpenRouter adds usage.cost; the SDK's Usage model drops unknown fields, so read the raw body.
    try:
        cost = result.raw_http_response.json().get("usage", {}).get("cost")
        return float(cost) if cost is not None else None
    except Exception:  # noqa: BLE001
        return None
