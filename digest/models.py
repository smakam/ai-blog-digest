"""Data carried through the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Bucket(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NOT_AI = "not_ai"


class ContentSource(str, Enum):
    FEED_FULL_TEXT = "feed_full_text"
    ARTICLE_FETCH = "article_fetch"
    TITLE_SUMMARY = "title_summary"


@dataclass
class Feed:
    title: str
    xml_url: str
    html_url: str | None = None


@dataclass
class Item:
    id: str
    title: str
    url: str
    source: str
    published: datetime
    author: str | None = None
    feed_content: str = ""
    feed_summary: str = ""

    # Filled in by the pipeline.
    text: str = ""
    content_source: ContentSource | None = None
    is_ai: float | None = None
    worthiness: float | None = None
    worthiness_confidence: float | None = None
    final_score: float | None = None
    bucket: Bucket | None = None
    summary: str | None = None
    delivered: bool = False
    jev_ms: float | None = None
    jev_input_tokens: int | None = None
    jev_cost: float | None = None
    llm_ms: float | None = None
    llm_cost: float | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def full_text_available(self) -> bool:
        return self.content_source in (ContentSource.FEED_FULL_TEXT, ContentSource.ARTICLE_FETCH)
