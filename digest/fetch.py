"""Fetch feeds, select recent items, and resolve article text.

Content resolution order (spec §1):
  1. full post text from the feed, if present;
  2. otherwise fetch the article URL and extract the main text;
  3. otherwise fall back to title plus feed summary.
"""

from __future__ import annotations

import calendar
import hashlib
import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import feedparser
import httpx
import trafilatura

from digest.config import FetchConfig
from digest.models import ContentSource, Feed, Item

log = logging.getLogger(__name__)


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "tr"}
    _SKIP = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(fragment: str) -> str:
    if not fragment:
        return ""
    parser = _TextExtractor()
    parser.feed(fragment)
    parser.close()
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return None


def _entry_id(feed: Feed, entry) -> str:
    raw = entry.get("id") or entry.get("link") or f"{entry.get('title', '')}|{feed.xml_url}"
    return hashlib.sha256(raw.strip().encode()).hexdigest()[:24]


def _entry_content(entry) -> str:
    # Pick the longest content block; some feeds ship both a teaser and the full post.
    blocks = [c.get("value", "") for c in entry.get("content", []) or []]
    return max(blocks, key=len, default="")


def parse_feed(feed: Feed, body: bytes, since: datetime) -> tuple[list[Item], int]:
    """Return (recent items, number of undated entries skipped)."""
    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"unparseable feed: {parsed.get('bozo_exception')}")
    source = parsed.feed.get("title") or feed.title
    items: list[Item] = []
    undated = 0
    for entry in parsed.entries:
        published = _entry_time(entry)
        if published is None:
            undated += 1
            continue
        if published < since:
            continue
        link = entry.get("link") or ""
        items.append(
            Item(
                id=_entry_id(feed, entry),
                title=html_to_text(entry.get("title", "")) or "(untitled)",
                url=link,
                source=source,
                published=published,
                author=entry.get("author"),
                feed_content=html_to_text(_entry_content(entry)),
                feed_summary=html_to_text(entry.get("summary", "")),
            )
        )
    return items, undated


class Fetcher:
    def __init__(self, config: FetchConfig, client: httpx.Client | None = None) -> None:
        self.config = config
        self.client = client or httpx.Client(
            timeout=config.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": config.user_agent},
        )

    def close(self) -> None:
        self.client.close()

    def fetch_feed(self, feed: Feed, since: datetime) -> list[Item]:
        try:
            response = self.client.get(feed.xml_url)
        except httpx.TransportError:
            # Timeouts and connection resets are often transient; one retry cuts false alarms.
            log.info("Retrying %s after a transport error", feed.title)
            response = self.client.get(feed.xml_url)
        response.raise_for_status()
        items, undated = parse_feed(feed, response.content, since)
        if undated:
            log.info("%s: skipped %d undated entries", feed.title, undated)
        return items

    def fetch_all(self, feeds: list[Feed], lookback_hours: float) -> tuple[list[Item], list[tuple[Feed, str]]]:
        """Fetch every feed concurrently. Returns (items, [(feed, error), ...])."""
        since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        items: list[Item] = []
        failures: list[tuple[Feed, str]] = []

        def one(feed: Feed):
            try:
                return feed, self.fetch_feed(feed, since), None
            except Exception as exc:  # noqa: BLE001 — one broken feed must not stop the run
                return feed, [], f"{type(exc).__name__}: {exc}"

        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            for feed, feed_items, error in pool.map(one, feeds):
                if error:
                    log.warning("Feed failed: %s (%s): %s", feed.title, feed.xml_url, error)
                    failures.append((feed, error))
                items.extend(feed_items)

        # The same post can appear in multiple feeds (e.g. an aggregator). Keep the first.
        unique: dict[str, Item] = {}
        by_url: set[str] = set()
        for item in items:
            if item.id in unique or (item.url and item.url in by_url):
                continue
            unique[item.id] = item
            if item.url:
                by_url.add(item.url)
        return list(unique.values()), failures

    def extract_article(self, url: str) -> str:
        response = self.client.get(url)
        response.raise_for_status()
        return trafilatura.extract(response.text, include_comments=False, include_tables=True) or ""

    def resolve_content(self, item: Item) -> None:
        minimum = self.config.min_full_text_chars
        # Many RSS feeds put the whole post in <description> rather than <content:encoded>.
        feed_text = max(item.feed_content, item.feed_summary, key=len)
        if len(feed_text) >= minimum:
            item.text, item.content_source = feed_text, ContentSource.FEED_FULL_TEXT
            return
        article = ""
        if item.url:
            try:
                article = self.extract_article(item.url)
            except Exception as exc:  # noqa: BLE001
                item.errors.append(f"article fetch: {type(exc).__name__}: {exc}")
            if len(article) >= minimum:
                item.text, item.content_source = article, ContentSource.ARTICLE_FETCH
                return
        # Neither source had full text: use whatever is longest.
        fallback = max(article, item.feed_content, item.feed_summary, key=len)
        item.text = f"{item.title}\n\n{fallback}".strip()
        item.content_source = ContentSource.TITLE_SUMMARY

    def resolve_all(self, items: list[Item]) -> None:
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            list(pool.map(self.resolve_content, items))
