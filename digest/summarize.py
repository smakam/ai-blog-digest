"""High-item summaries via an OpenRouter chat model. Only the model ID varies between options."""

from __future__ import annotations

import time

import httpx

from digest.config import SummarizerConfig
from digest.models import Item

# Shared by every option so summary quality is comparable.
SYSTEM_PROMPT = """You write entries for a daily AI engineering digest read by an experienced AI engineer.
Summarize the blog post in 4-5 short sentences, 90 words at most, plain text
(no markdown, no bullet symbols, no preamble):
- what the post is (the concrete thing announced, built, or argued),
- why it matters to someone building AI systems,
- the single most useful takeaway.
Be specific: name models, techniques, and key numbers from the post, but pick only the most
important ones. Do not invent details."""

MAX_SUMMARY_TOKENS = 250


def trim_to_sentence(text: str) -> str:
    """Drop a trailing partial sentence left by hitting the token limit."""
    cut = max(text.rfind(". "), text.rfind(".\n"), text.rfind("! "), text.rfind("? "))
    if text.rstrip().endswith((".", "!", "?")) or cut < 0:
        return text
    return text[: cut + 1]


def build_user_prompt(item: Item, max_chars: int) -> str:
    return f"Title: {item.title}\nURL: {item.url}\n\n{item.text[:max_chars]}"


class Summarizer:
    def __init__(self, config: SummarizerConfig, api_key: str, client: httpx.Client | None = None) -> None:
        self.config = config
        self.client = client or httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Title": "AI Blog Digest",
            },
        )

    def close(self) -> None:
        self.client.close()

    def summarize(self, item: Item) -> str:
        started = time.perf_counter()
        response = self.client.post(
            "/chat/completions",
            json={
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_prompt(item, self.config.max_input_chars)},
                ],
                "max_tokens": MAX_SUMMARY_TOKENS,
                "temperature": 0.2,
                "usage": {"include": True},
            },
        )
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise RuntimeError(f"OpenRouter error: {body['error']}")
        item.llm_ms = (time.perf_counter() - started) * 1000
        cost = (body.get("usage") or {}).get("cost")
        item.llm_cost = float(cost) if cost is not None else None
        choice = body["choices"][0]
        content = (choice["message"]["content"] or "").strip()
        if not content:
            raise RuntimeError("empty summary")
        if choice.get("finish_reason") == "length":
            content = trim_to_sentence(content)
        item.summary = content
        return item.summary
