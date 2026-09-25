import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from digest.config import Config, FetchConfig
from digest.fetch import Fetcher
from digest.models import Item
from digest.pipeline import run
from digest.runlog import RunLog
from digest.state import SeenState

NOW = datetime.now(timezone.utc)

POSTS = {
    # guid: (title, is_ai, worthiness)
    "arch": ("Inside our agent runtime architecture", 0.95, 0.85),
    "launch": ("Announcing a new open model", 0.9, 0.75),
    "tips": ("10 prompt tips", 0.9, 0.5),
    "funding": ("Startup raises $50M", 0.8, 0.1),
    "robot": ("Our new humanoid robot", 0.1, 0.9),
    "broken": ("Jev will fail on this one", 0.9, 0.9),
}


def feed_xml() -> str:
    items = "".join(
        f"<item><title>{t}</title><link>https://blog.ex/{g}</link><guid>{g}</guid>"
        f"<pubDate>{(NOW - timedelta(hours=3)):%a, %d %b %Y %H:%M:%S +0000}</pubDate>"
        f"<description>{'Body text. ' * 200}</description></item>"
        for g, (t, _, _) in POSTS.items()
    )
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>Eng Blog</title>{items}</channel></rss>"


def http_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "blog.ex" and request.url.path == "/feed":
        return httpx.Response(200, text=feed_xml())
    if request.url.host == "dead.ex":
        return httpx.Response(404)
    return httpx.Response(200, text="<html><body><p>article</p></body></html>")


class FakeJev:
    def classify(self, item: Item) -> None:
        guid = item.url.rsplit("/", 1)[-1]
        if guid == "broken":
            raise RuntimeError("upstream 502")
        _, item.is_ai, item.worthiness = POSTS[guid]
        item.jev_cost = 0.00001


class FakeSummarizer:
    def __init__(self, fail_on=()):
        self.fail_on = fail_on
        self.calls = []

    def summarize(self, item: Item) -> str:
        self.calls.append(item.title)
        if item.title in self.fail_on:
            raise RuntimeError("model overloaded")
        item.summary = f"Summary of {item.title}"
        item.llm_cost = 0.001
        return item.summary


class FakeTelegram:
    def __init__(self, fail=False):
        self.sent, self.errors, self.fail = [], [], fail

    def send(self, text, parse_mode="HTML"):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(text)

    def send_error(self, text):
        self.errors.append(text)


@pytest.fixture
def env(tmp_path):
    opml = tmp_path / "feeds.opml"
    opml.write_text("""<opml><body><outline text="AI">
      <outline type="rss" text="Eng" xmlUrl="https://blog.ex/feed"/>
      <outline type="rss" text="Dead" xmlUrl="https://dead.ex/feed"/>
    </outline></body></opml>""")
    config = Config(option="test", opml_path=str(opml), state_path=str(tmp_path / "seen.json"),
                    log_dir=str(tmp_path / "logs"), fetch=FetchConfig(min_full_text_chars=500))
    fetcher = Fetcher(config.fetch, client=httpx.Client(transport=httpx.MockTransport(http_handler)))
    return config, fetcher


def do_run(config, fetcher, telegram, summarizer=None, **kw):
    return run(config=config, fetcher=fetcher, classifier=FakeJev(), summarizer=summarizer or FakeSummarizer(),
               telegram=telegram, state=SeenState(config.state_path), runlog=RunLog(config.log_dir, config.option), **kw)


def read_log(config):
    records = []
    for path in sorted((__import__("pathlib").Path(config.log_dir)).glob("*.jsonl")):
        records += [json.loads(line) for line in path.read_text().splitlines()]
    return records


def test_end_to_end(env):
    config, fetcher = env
    tg = FakeTelegram()
    summarizer = FakeSummarizer()
    do_run(config, fetcher, tg, summarizer)

    assert len(tg.sent) == 1
    digest = tg.sent[0]
    assert "Summary of Inside our agent runtime architecture" in digest
    assert "Summary of Announcing a new open model" in digest
    assert "10 prompt tips" in digest and "Summary of 10 prompt tips" not in digest  # medium: link only
    assert "raises" not in digest and "humanoid" not in digest  # low / non-AI not delivered
    # Only high items are summarized, best first.
    assert summarizer.calls == ["Inside our agent runtime architecture", "Announcing a new open model"]

    # Errors surface to Telegram: broken feed + Jev failure.
    assert len(tg.errors) == 1
    assert "Dead" in tg.errors[0] and "Jev failed on 1" in tg.errors[0]

    records = read_log(config)
    items = {r["title"]: r for r in records if r["type"] == "item"}
    assert len(items) == 6
    assert items["Our new humanoid robot"]["bucket"] == "not_ai"
    assert items["Startup raises $50M"]["bucket"] == "low" and not items["Startup raises $50M"]["delivered"]
    assert items["10 prompt tips"]["delivered"] is True
    assert items["Inside our agent runtime architecture"]["full_text"] is True
    for key in ("title", "url", "source", "is_ai", "worthiness", "bucket", "full_text", "delivered"):
        assert key in items["10 prompt tips"]
    run_rec = [r for r in records if r["type"] == "run"][0]
    assert run_rec["high"] == 2 and run_rec["medium"] == 1 and run_rec["jev_failures"] == 1


def test_second_run_never_redelivers(env):
    config, fetcher = env
    do_run(config, fetcher, FakeTelegram())
    tg2 = FakeTelegram()
    result = do_run(config, fetcher, tg2)
    # Only the item Jev failed on is retried; nothing already delivered appears again.
    assert [i.title for i in result.items] == ["Jev will fail on this one"]
    assert "Inside our agent runtime" not in tg2.sent[0]
    assert "No new worthwhile AI posts" in tg2.sent[0]


def test_llm_failure_still_delivers_title(env):
    config, fetcher = env
    tg = FakeTelegram()
    do_run(config, fetcher, tg, FakeSummarizer(fail_on={"Announcing a new open model"}))
    assert "Announcing a new open model" in tg.sent[0]
    assert "summary unavailable" in tg.sent[0]
    assert "Summary LLM failed on 1" in tg.errors[0]


def test_delivery_failure_keeps_state_and_raises(env):
    config, fetcher = env
    tg = FakeTelegram(fail=True)
    with pytest.raises(RuntimeError, match="delivery failed"):
        do_run(config, fetcher, tg)
    assert "Digest delivery failed" in tg.errors[0]
    assert SeenState(config.state_path).seen == {}


def test_dry_run_sends_nothing(env, capsys):
    config, fetcher = env
    tg = FakeTelegram()
    do_run(config, fetcher, tg, dry_run=True)
    assert tg.sent == [] and tg.errors == []
    assert "AI Blog Digest" in capsys.readouterr().out
    assert SeenState(config.state_path).seen == {}


def test_only_top_n_high_items_are_summarized(env):
    config, fetcher = env
    config.max_summaries = 1
    tg, summarizer = FakeTelegram(), FakeSummarizer()
    do_run(config, fetcher, tg, summarizer)
    assert summarizer.calls == ["Inside our agent runtime architecture"]
    digest = tg.sent[0]
    assert "Top picks (1)" in digest
    # The overflow High item is still delivered, as a link ahead of the Medium items.
    also = digest.split("Also worth a look")[1]
    assert also.index("Announcing a new open model") < also.index("10 prompt tips")
    assert "Summary of Announcing" not in digest
