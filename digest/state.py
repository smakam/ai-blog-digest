"""Seen-item state: guarantees an item is never processed or delivered twice across runs."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


class SeenState:
    def __init__(self, path: str | Path, retention_days: int = 30) -> None:
        self.path = Path(path)
        self.retention = timedelta(days=retention_days)
        self.seen: dict[str, str] = {}
        if self.path.exists():
            data = json.loads(self.path.read_text() or "{}")
            self.seen = data.get("seen", {})

    def __contains__(self, item_id: str) -> bool:
        return item_id in self.seen

    def add(self, item_id: str, when: datetime | None = None) -> None:
        self.seen.setdefault(item_id, (when or datetime.now(timezone.utc)).isoformat())

    def prune(self, now: datetime | None = None) -> None:
        cutoff = (now or datetime.now(timezone.utc)) - self.retention
        self.seen = {k: v for k, v in self.seen.items() if datetime.fromisoformat(v) >= cutoff}

    def save(self) -> None:
        self.prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seen": self.seen}, indent=1, sort_keys=True))
        os.replace(tmp, self.path)
