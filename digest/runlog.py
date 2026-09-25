"""Structured per-item and per-run logs (JSONL) for the week-one review and option comparison."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from digest.models import Item


def item_record(item: Item, run_id: str, option: str) -> dict:
    return {
        "type": "item",
        "run_id": run_id,
        "option": option,
        "item_id": item.id,
        "title": item.title,
        "url": item.url,
        "source": item.source,
        "published": item.published.isoformat(),
        "is_ai": _round(item.is_ai),
        "worthiness": _round(item.worthiness),
        "worthiness_confidence": _round(item.worthiness_confidence),
        "final_score": _round(item.final_score),
        "bucket": item.bucket.value if item.bucket else None,
        "full_text": item.full_text_available,
        "content_source": item.content_source.value if item.content_source else None,
        "text_chars": len(item.text),
        "delivered": item.delivered,
        "summary": item.summary,
        "jev_ms": _round(item.jev_ms, 1),
        "jev_input_tokens": item.jev_input_tokens,
        "jev_cost": item.jev_cost,
        "llm_ms": _round(item.llm_ms, 1),
        "llm_cost": item.llm_cost,
        "errors": item.errors,
    }


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


class RunLog:
    def __init__(self, log_dir: str | Path, option: str, started: datetime | None = None) -> None:
        self.started = started or datetime.now(timezone.utc)
        self.option = option
        self.run_id = f"{self.started:%Y%m%dT%H%M%SZ}-{option}"
        self.path = Path(log_dir) / f"{self.started:%Y-%m-%d}_{option}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict) -> None:
        with self.path.open("a") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def item(self, item: Item) -> None:
        self.write(item_record(item, self.run_id, self.option))

    def run_summary(self, **fields) -> None:
        self.write({"type": "run", "run_id": self.run_id, "option": self.option,
                    "started": self.started.isoformat(), **fields})
