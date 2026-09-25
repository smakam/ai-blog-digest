"""Telegram delivery: one digest per day, split only if it exceeds Telegram's message limit."""

from __future__ import annotations

import html
from datetime import date

import httpx

from digest.models import Item

MAX_MESSAGE_CHARS = 4000  # Telegram's hard limit is 4096.


def _link(item: Item) -> str:
    title = html.escape(item.title)
    if not item.url:
        return f"<b>{title}</b>"
    return f'<a href="{html.escape(item.url, quote=True)}">{title}</a>'


def format_digest(high: list[Item], medium: list[Item], day: date, option: str | None = None) -> list[str]:
    """Render the digest as one or more HTML-formatted Telegram messages."""
    header = f"<b>AI Blog Digest — {day:%a %d %b %Y}</b>"
    if option:
        header += f"  <i>[{html.escape(option)}]</i>"

    blocks: list[str] = [header]
    if not high and not medium:
        blocks.append("No new worthwhile AI posts since the last digest.")

    if high:
        blocks.append(f"<b>🔥 Top picks ({len(high)})</b>")
        for item in high:
            summary = html.escape(item.summary) if item.summary else "<i>(summary unavailable)</i>"
            blocks.append(f"{_link(item)}\n<i>{html.escape(item.source)}</i>\n{summary}")

    if medium:
        lines = [f"<b>📌 Also worth a look ({len(medium)})</b>"]
        lines += [f"• {_link(item)} — <i>{html.escape(item.source)}</i>" for item in medium]
        blocks.append("\n".join(lines))

    return _pack(blocks)


def _pack(blocks: list[str]) -> list[str]:
    messages: list[str] = []
    current = ""
    for block in blocks:
        for piece in _split_block(block):
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) > MAX_MESSAGE_CHARS and current:
                messages.append(current)
                current = piece
            else:
                current = candidate
    if current:
        messages.append(current)
    return messages


def _split_block(block: str) -> list[str]:
    # A single block (e.g. a long Medium list) can exceed the limit; split it on line boundaries.
    if len(block) <= MAX_MESSAGE_CHARS:
        return [block]
    pieces, current = [], ""
    for line in block.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > MAX_MESSAGE_CHARS and current:
            pieces.append(current)
            current = line
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str, client: httpx.Client | None = None) -> None:
        self.chat_id = chat_id
        self.client = client or httpx.Client(base_url=f"https://api.telegram.org/bot{bot_token}", timeout=30)

    def close(self) -> None:
        self.client.close()

    def send(self, text: str, parse_mode: str | None = "HTML") -> None:
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        response = self.client.post("/sendMessage", json=payload)
        if response.status_code != 200 or not response.json().get("ok"):
            # Never include the URL: it contains the bot token.
            raise RuntimeError(f"Telegram sendMessage failed: HTTP {response.status_code}: {response.text[:300]}")

    def send_error(self, text: str) -> None:
        """Plain-text error notice (no HTML, so exception text can't break formatting)."""
        self.send(f"⚠️ AI Blog Digest\n{text}"[:MAX_MESSAGE_CHARS], parse_mode=None)
