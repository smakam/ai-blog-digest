"""High-item summaries via an OpenRouter chat model. Only the model ID varies between options."""

from __future__ import annotations

import time

import httpx

from digest.config import SummarizerConfig
from digest.models import Item

# Shared by every option so summary quality is comparable.
SYSTEM_PROMPT = """You write entries for a daily AI engineering digest read by an experienced AI engineer.
Summarize the blog post in 4-5 short lines of plain text (no markdown, no bullet symbols, no preamble):
- what the post is (the concrete thing announced, built, or argued),
- why it matters to someone building AI systems,
- the single most useful takeaway.
Be specific: name models, techniques, numbers, and components from the post. Do not invent details."""


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
                "max_tokens": 400,
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
        content = body["choices"][0]["message"]["content"] or ""
        if not content.strip():
            raise RuntimeError("empty summary")
        item.summary = content.strip()
        return item.summary
