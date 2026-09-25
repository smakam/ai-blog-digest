"""The daily run: feeds -> new items -> content -> Jev -> policy -> summaries -> Telegram -> state + logs."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from digest.classify import JevClassifier
from digest.config import Config
from digest.fetch import Fetcher
from digest.models import Bucket, Item
from digest.opml import parse_opml
from digest.runlog import RunLog
from digest.scoring import ScoreAdjuster, apply_policy, no_boost
from digest.state import SeenState
from digest.summarize import Summarizer
from digest.telegram import TelegramClient, format_digest

log = logging.getLogger(__name__)

JEV_WORKERS = 4


@dataclass
class RunResult:
    items: list[Item] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    feed_failures: list[tuple[str, str]] = field(default_factory=list)
    jev_failures: list[tuple[Item, str]] = field(default_factory=list)
    llm_failures: list[tuple[Item, str]] = field(default_factory=list)

    def error_report(self) -> str | None:
        lines: list[str] = []
        if self.feed_failures:
            lines.append(f"{len(self.feed_failures)} feed(s) failed:")
            lines += [f"  • {name}: {err[:150]}" for name, err in self.feed_failures[:10]]
        if self.jev_failures:
            lines.append(f"Jev failed on {len(self.jev_failures)} item(s) (will retry next run):")
            lines += [f"  • {item.title[:80]}: {err[:150]}" for item, err in self.jev_failures[:5]]
        if self.llm_failures:
            lines.append(f"Summary LLM failed on {len(self.llm_failures)} item(s) (sent without summary):")
            lines += [f"  • {item.title[:80]}: {err[:150]}" for item, err in self.llm_failures[:5]]
        return "\n".join(lines) or None


def run(
    config: Config,
    fetcher: Fetcher,
    classifier: JevClassifier,
    summarizer: Summarizer,
    telegram: TelegramClient | None,
    state: SeenState,
    runlog: RunLog,
    adjust: ScoreAdjuster = no_boost,
    dry_run: bool = False,
) -> RunResult:
    started = time.perf_counter()
    result = RunResult()

    feeds = parse_opml(config.opml_path)
    log.info("Loaded %d feeds from %s", len(feeds), config.opml_path)

    fetched, failures = fetcher.fetch_all(feeds, config.lookback_hours)
    result.feed_failures = [(feed.title, err) for feed, err in failures]
    items = [item for item in fetched if item.id not in state]
    log.info("%d recent items, %d new", len(fetched), len(items))
    result.items = items

    fetcher.resolve_all(items)

    def classify(item: Item) -> None:
        try:
            classifier.classify(item)
            apply_policy(item, config.thresholds, adjust)
        except Exception as exc:  # noqa: BLE001 — one bad item must not stop the run
            msg = f"{type(exc).__name__}: {exc}"
            item.errors.append(f"jev: {msg}")
            result.jev_failures.append((item, msg))
            log.warning("Jev failed for %s: %s", item.url, msg)

    with ThreadPoolExecutor(max_workers=JEV_WORKERS) as pool:
        list(pool.map(classify, items))

    ranked = sorted((i for i in items if i.bucket), key=lambda i: i.final_score or 0, reverse=True)
    high = [i for i in ranked if i.bucket == Bucket.HIGH]
    medium = [i for i in ranked if i.bucket == Bucket.MEDIUM]
    # Only the best High items get a summary; the overflow is listed (still delivered) above Medium.
    top, overflow = high[: config.max_summaries], high[config.max_summaries :]

    for item in top:
        try:
            summarizer.summarize(item)
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            item.errors.append(f"llm: {msg}")
            result.llm_failures.append((item, msg))
            log.warning("Summary failed for %s: %s", item.url, msg)

    result.messages = format_digest(top, overflow + medium, datetime.now(timezone.utc).date(), option=config.option)

    delivery_error = None
    if dry_run:
        print("\n\n---- message break ----\n\n".join(result.messages))
    else:
        try:
            for message in result.messages:
                telegram.send(message)
            for item in high + medium:
                item.delivered = True
        except Exception as exc:  # noqa: BLE001
            delivery_error = f"{type(exc).__name__}: {exc}"
            log.error("Delivery failed: %s", delivery_error)

    for item in items:
        runlog.item(item)

    # Mark classified items as seen only once delivery succeeded; items Jev failed on stay unseen
    # so the next run retries them while they're still inside the lookback window.
    if not dry_run and delivery_error is None:
        for item in items:
            if item.bucket is not None:
                state.add(item.id)
        state.save()

    runlog.run_summary(
        duration_s=round(time.perf_counter() - started, 2),
        feeds=len(feeds),
        feed_failures=len(result.feed_failures),
        items_recent=len(fetched),
        items_new=len(items),
        high=len(high),
        medium=len(medium),
        low=sum(1 for i in items if i.bucket == Bucket.LOW),
        not_ai=sum(1 for i in items if i.bucket == Bucket.NOT_AI),
        jev_failures=len(result.jev_failures),
        llm_failures=len(result.llm_failures),
        jev_cost=round(sum(i.jev_cost or 0 for i in items), 6),
        llm_cost=round(sum(i.llm_cost or 0 for i in items), 6),
        jev_model=config.jev.model,
        summary_model=config.summarizer.model,
        delivered=delivery_error is None and not dry_run,
        dry_run=dry_run,
        delivery_error=delivery_error,
    )

    report = result.error_report()
    if delivery_error:
        report = f"Digest delivery failed: {delivery_error}\n" + (report or "")
    if report and not dry_run:
        try:
            telegram.send_error(report)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not send error notice: %s", exc)
    elif report:
        print("\n---- error notice ----\n" + report)

    if delivery_error:
        raise RuntimeError(f"Digest delivery failed: {delivery_error}")
    return result
