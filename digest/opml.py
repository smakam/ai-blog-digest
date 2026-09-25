"""Parse a Feedly OPML export into a flat, de-duplicated feed list."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from digest.models import Feed


def parse_opml(path: str | Path) -> list[Feed]:
    root = ET.parse(path).getroot()
    feeds: list[Feed] = []
    seen: set[str] = set()
    # Feedly nests feeds inside category outlines; any outline with xmlUrl is a feed.
    for outline in root.iter("outline"):
        xml_url = (outline.get("xmlUrl") or "").strip()
        if not xml_url or xml_url in seen:
            continue
        seen.add(xml_url)
        feeds.append(
            Feed(
                title=outline.get("title") or outline.get("text") or xml_url,
                xml_url=xml_url,
                html_url=outline.get("htmlUrl"),
            )
        )
    return feeds
