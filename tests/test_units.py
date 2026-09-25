from datetime import datetime, timedelta, timezone

import httpx
import pytest

from digest.classify import CHARS_PER_TOKEN, JevClassifier, build_state
from digest.config import FetchConfig, JevConfig, Thresholds, load_config
from digest.fetch import Fetcher, html_to_text, parse_feed
from digest.models import Bucket, ContentSource, Feed, Item
from digest.opml import parse_opml
from digest.scoring import apply_policy, bucket_for
from digest.state import SeenState
from digest.telegram import MAX_MESSAGE_CHARS, format_digest

NOW = datetime.now(timezone.utc)


def make_item(**kw) -> Item:
    defaults = dict(id="x", title="T", url="https://ex.com/a", source="Blog", published=NOW)
    return Item(**{**defaults, **kw})


def rss(entries: str) -> bytes:
    return f"""<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title>{entries}</channel></rss>""".encode()


def entry(title, when, guid, body="", desc="short"):
    content = f"<content:encoded xmlns:content='http://purl.org/rss/1.0/modules/content/'><![CDATA[{body}]]></content:encoded>" if body else ""
    return (f"<item><title>{title}</title><link>https://ex.com/{guid}</link><guid>{guid}</guid>"
            f"<pubDate>{when:%a, %d %b %Y %H:%M:%S +0000}</pubDate><description>{desc}</description>{content}</item>")


# ---- OPML ----

def test_parse_opml_nested_and_dedup(tmp_path):
    p = tmp_path / "f.opml"
    p.write_text("""<opml version="1.0"><body>
      <outline text="AI"><outline type="rss" text="A" title="A" xmlUrl="https://a/feed"/>
        <outline type="rss" text="B" xmlUrl="https://b/feed"/></outline>
      <outline text="Dup"><outline type="rss" text="A2" xmlUrl="https://a/feed"/></outline>
    </body></opml>""")
    feeds = parse_opml(p)
    assert [f.xml_url for f in feeds] == ["https://a/feed", "https://b/feed"]
    assert feeds[1].title == "B"


# ---- Feed parsing / window ----

def test_parse_feed_window_and_undated():
    body = rss(entry("new", NOW - timedelta(hours=2), "n") + entry("old", NOW - timedelta(hours=30), "o")
               + "<item><title>nodate</title><link>https://ex.com/z</link></item>")
    items, undated = parse_feed(Feed("Blog", "https://ex.com/feed"), body, NOW - timedelta(hours=24))
    assert [i.title for i in items] == ["new"]
    assert undated == 1


def test_html_to_text():
    assert html_to_text("<p>Hello&nbsp;<b>world</b></p><script>x()</script><p>two</p>") == "Hello\xa0world\n\ntwo"


# ---- Content resolution ----

def _fetcher(handler, min_chars=100):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Fetcher(FetchConfig(min_full_text_chars=min_chars), client=client)


def test_content_prefers_feed_full_text():
    f = _fetcher(lambda r: pytest.fail("should not fetch"))
    item = make_item(feed_content="x" * 200)
    f.resolve_content(item)
    assert item.content_source == ContentSource.FEED_FULL_TEXT


def test_content_fetches_article():
    article = "<html><body><article><h1>Title</h1>" + "<p>" + "Real article sentence. " * 40 + "</p></article></body></html>"
    f = _fetcher(lambda r: httpx.Response(200, text=article))
    item = make_item(feed_content="teaser")
    f.resolve_content(item)
    assert item.content_source == ContentSource.ARTICLE_FETCH
    assert "Real article sentence" in item.text
    assert item.full_text_available


def test_content_falls_back_to_title_summary():
    f = _fetcher(lambda r: httpx.Response(500))
    item = make_item(title="Hello", feed_summary="a summary")
    f.resolve_content(item)
    assert item.content_source == ContentSource.TITLE_SUMMARY
    assert item.text == "Hello\n\na summary"
    assert not item.full_text_available
    assert item.errors


# ---- Jev state ----

def test_build_state_truncates_and_excludes_author():
    item = make_item(text="a" * (CHARS_PER_TOKEN * 100 + 50), author="Famous Person")
    state = build_state(item, max_input_tokens=100)
    assert len(state["text"]) == CHARS_PER_TOKEN * 100
    assert state["truncated"] is True
    assert "Famous Person" not in str(state) and "author" not in state


def test_jev_classifier_against_mock_openrouter():
    seen = {}

    import httpx2

    def handler(request):
        import json
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "model": "jev-1.13",
            "answers": {
                "is_ai": {"type": "noul", "noul": 0.97},
                "worthiness": {"type": "score", "score": 3.0, "confidence": 0.8,
                               "legend": {str(i): "x" for i in range(5)},
                               "probabilities": {"0": 0, "1": 0, "2": 0, "3": 1.0, "4": 0}},
            },
            "usage": {"input_tokens": 500, "output_tokens": 2, "cost": 0.00002},
        })

    from typesafe_sdk import TypeSafeClient
    client = TypeSafeClient(api_key="k", base_url="https://openrouter.ai/api", model="jev-1.13",
                            transport=httpx2.MockTransport(handler))
    item = make_item(text="Some AI post")
    JevClassifier(JevConfig(), "k", client=client).classify(item)
    assert seen["url"] == "https://openrouter.ai/api/v1/systemone"
    assert seen["body"]["model"] == "jev-1.13"
    assert set(seen["body"]["questions"]) == {"is_ai", "worthiness"}
    assert seen["body"]["questions"]["worthiness"]["type"] == "score"
    assert item.is_ai == 0.97
    assert item.worthiness == 0.75
    assert item.jev_cost == 0.00002
    assert item.jev_input_tokens == 500


# ---- Scoring ----

@pytest.mark.parametrize("score,bucket", [(0.7, Bucket.HIGH), (0.69, Bucket.MEDIUM), (0.4, Bucket.MEDIUM),
                                          (0.39, Bucket.LOW), (1.0, Bucket.HIGH), (0.0, Bucket.LOW)])
def test_bucket_thresholds(score, bucket):
    assert bucket_for(score, Thresholds()) == bucket


def test_policy_drops_non_ai_and_applies_boost_hook():
    assert apply_policy(make_item(is_ai=0.2, worthiness=0.95), Thresholds()) == Bucket.NOT_AI
    boosted = make_item(is_ai=0.9, worthiness=0.6)
    assert apply_policy(boosted, Thresholds(), adjust=lambda i, s: s + 0.2) == Bucket.HIGH
    assert boosted.final_score == pytest.approx(0.8)


# ---- State ----

def test_state_roundtrip_and_prune(tmp_path):
    path = tmp_path / "seen.json"
    s = SeenState(path, retention_days=30)
    s.add("old", NOW - timedelta(days=40))
    s.add("new")
    s.save()
    reloaded = SeenState(path)
    assert "new" in reloaded and "old" not in reloaded


# ---- Telegram formatting ----

def test_format_digest_escapes_and_splits():
    high = [make_item(title="A <b> & c", summary="Line one\nLine two", url="https://ex.com/?a=1&b=2")]
    medium = [make_item(id=str(i), title=f"Medium post number {i} " + "x" * 80) for i in range(80)]
    messages = format_digest(high, medium, NOW.date())
    assert all(len(m) <= MAX_MESSAGE_CHARS for m in messages)
    assert len(messages) > 1
    assert "A &lt;b&gt; &amp; c" in messages[0]
    assert "a=1&amp;b=2" in messages[0]


def test_format_digest_empty():
    assert "No new worthwhile" in format_digest([], [], NOW.date())[0]


# ---- Config ----

def test_config_env_overrides(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("option: a\nthresholds: {high: 0.8, medium: 0.5}\n")
    monkeypatch.setenv("SUMMARY_MODEL", "openai/gpt-5")
    monkeypatch.setenv("DIGEST_OPTION", "gha")
    c = load_config(cfg)
    assert c.summarizer.model == "openai/gpt-5" and c.option == "gha" and c.thresholds.high == 0.8


def test_config_rejects_bad_thresholds(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("thresholds: {high: 0.3, medium: 0.5}\n")
    with pytest.raises(ValueError):
        load_config(cfg)


def test_trim_to_sentence():
    from digest.summarize import trim_to_sentence
    assert trim_to_sentence("One thing. Two things. Takeaway: em") == "One thing. Two things."
    assert trim_to_sentence("First point. It delivered up to 2.") == "First point."
    assert trim_to_sentence("no sentence break at all") == "no sentence break at all"


def test_feed_fetch_retries_once_on_timeout():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, content=rss(entry("ok", NOW, "g")))

    items = _fetcher(handler).fetch_feed(Feed("Blog", "https://ex.com/feed"), NOW - timedelta(hours=1))
    assert len(calls) == 2 and [i.title for i in items] == ["ok"]
