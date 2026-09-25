"""One-off experiment: classify the same items with Jev and with an LLM, and report disagreements.

Both see identical input (the same state Jev gets) and the same two questions and rubric from
digest/classify.py; the same scoring policy then buckets both. Nothing is sent to Telegram and
seen-state is not touched.

  python tools/compare_classifiers.py [--model anthropic/claude-sonnet-5] [--hours 36] [--out file.csv]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digest.classify import IS_AI, WORTHINESS, WORTHINESS_LEVELS, JevClassifier, build_state  # noqa: E402
from digest.config import load_config  # noqa: E402
from digest.fetch import Fetcher  # noqa: E402
from digest.opml import parse_opml  # noqa: E402
from digest.scoring import apply_policy  # noqa: E402

LLM_SYSTEM = f"""You classify blog posts for an AI engineering digest. Answer two questions about the post.

Question "is_ai": {IS_AI.instructions}
  yes = {IS_AI.criteria["true"]}
  no  = {IS_AI.criteria["false"]}
Give the probability (0 to 1) that the answer is yes.

Question "worthiness": {WORTHINESS.instructions}
Pick the level that fits best (you may use a decimal between levels if it falls between two):
""" + "\n".join(f"  {i}: {text}" for i, text in enumerate(WORTHINESS_LEVELS)) + """

Judge the content only. Reply with only a JSON object, no other text:
{"is_ai": <probability 0-1>, "worthiness_level": <0-4>}"""


class LLMClassifier:
    def __init__(self, model: str, api_key: str, max_input_tokens: int) -> None:
        self.model = model
        self.max_input_tokens = max_input_tokens
        self.client = httpx.Client(base_url="https://openrouter.ai/api/v1", timeout=90,
                                   headers={"Authorization": f"Bearer {api_key}"})

    def classify(self, item) -> None:
        started = time.perf_counter()
        response = self.client.post("/chat/completions", json={
            "model": self.model,
            "messages": [
                {"role": "system", "content": LLM_SYSTEM},
                {"role": "user", "content": json.dumps(build_state(item, self.max_input_tokens), ensure_ascii=False)},
            ],
            "max_tokens": 60,
            "temperature": 0,
            "usage": {"include": True},
        })
        response.raise_for_status()
        body = response.json()
        item.jev_ms = (time.perf_counter() - started) * 1000
        text = body["choices"][0]["message"]["content"] or ""
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError(f"no JSON in reply: {text[:100]!r}")
        answer = json.loads(match.group(0))
        item.is_ai = float(answer["is_ai"])
        item.worthiness = float(answer["worthiness_level"]) / (len(WORTHINESS_LEVELS) - 1)
        usage = body.get("usage") or {}
        item.jev_input_tokens = usage.get("prompt_tokens")
        item.jev_cost = usage.get("cost")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="anthropic/claude-sonnet-5")  # classifier to compare against Jev
    parser.add_argument("--hours", type=float)
    parser.add_argument("--out", default="classifier_comparison.csv")
    args = parser.parse_args()

    config = load_config("config.yaml")
    api_key = os.environ["OPENROUTER_API_KEY"]
    fetcher = Fetcher(config.fetch)
    items, failures = fetcher.fetch_all(parse_opml(config.opml_path), args.hours or config.lookback_hours)
    fetcher.resolve_all(items)
    print(f"{len(items)} items ({len(failures)} feeds failed)", file=sys.stderr)

    jev = JevClassifier(config.jev, api_key)
    llm = LLMClassifier(args.model, api_key, config.jev.max_input_tokens)
    pairs = [(item, copy(item)) for item in items]  # (jev copy, llm copy) share the same text

    def run(classifier, item):
        try:
            classifier.classify(item)
            apply_policy(item, config.thresholds)
        except Exception as exc:  # noqa: BLE001
            item.errors.append(f"{type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(lambda p: run(jev, p[0]), pairs))
        list(pool.map(lambda p: run(llm, p[1]), pairs))

    ok = [(j, s) for j, s in pairs if j.bucket and s.bucket]
    failed = len(pairs) - len(ok)
    bucket = lambda i: i.bucket.value  # noqa: E731

    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["title", "source", "url", "content_source", "jev_is_ai", "llm_is_ai", "jev_worth", "llm_worth",
                    "jev_bucket", "llm_bucket", "agree"])
        for j, s in ok:
            w.writerow([j.title, j.source, j.url, j.content_source.value, f"{j.is_ai:.2f}", f"{s.is_ai:.2f}",
                        f"{j.worthiness:.2f}", f"{s.worthiness:.2f}", bucket(j), bucket(s), bucket(j) == bucket(s)])

    n = len(ok)
    ai_agree = sum((j.is_ai >= 0.5) == (s.is_ai >= 0.5) for j, s in ok)
    b_agree = sum(bucket(j) == bucket(s) for j, s in ok)
    delivered = lambda i: bucket(i) in ("high", "medium")  # noqa: E731
    d_agree = sum(delivered(j) == delivered(s) for j, s in ok)
    worth_ai = [(j, s) for j, s in ok if j.is_ai >= 0.5 and s.is_ai >= 0.5]
    mad = sum(abs(j.worthiness - s.worthiness) for j, s in worth_ai) / max(1, len(worth_ai))

    print(f"\nJev vs {args.model} on {n} items" + (f" ({failed} failed)" if failed else ""))
    print(f"  is_ai agreement:          {ai_agree}/{n}")
    print(f"  bucket agreement:         {b_agree}/{n}")
    print(f"  delivered-or-not agreement: {d_agree}/{n}")
    print(f"  mean |worthiness diff| on items both call AI: {mad:.2f} (n={len(worth_ai)})")

    order = ["high", "medium", "low", "not_ai"]
    matrix = Counter((bucket(j), bucket(s)) for j, s in ok)
    print("\n  rows = Jev, cols = LLM")
    print("  " + " " * 8 + "".join(f"{b:>8}" for b in order))
    for a in order:
        print(f"  {a:<8}" + "".join(f"{matrix[(a, b)]:>8}" for b in order))

    diffs = sorted(((j, s) for j, s in ok if bucket(j) != bucket(s)),
                   key=lambda p: -abs(order.index(bucket(p[0])) - order.index(bucket(p[1]))))
    if diffs:
        print("\n  Disagreements (Jev -> LLM):")
        for j, s in diffs:
            print(f"   {bucket(j):>6} -> {bucket(s):<6}  ai {j.is_ai:.2f}/{s.is_ai:.2f}  "
                  f"w {j.worthiness:.2f}/{s.worthiness:.2f}  {j.source[:16]:<16} {j.title[:55]}")

    jev_cost = sum(j.jev_cost or 0 for j, _ in pairs)
    llm_cost = sum(s.jev_cost or 0 for _, s in pairs)
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else 0  # noqa: E731
    print(f"\n  cost:    Jev ${jev_cost:.4f}   LLM ${llm_cost:.4f}   ({llm_cost / jev_cost:.0f}x)" if jev_cost else "")
    print(f"  latency: Jev {med([j.jev_ms for j, _ in pairs if j.jev_ms]):.0f} ms   "
          f"LLM {med([s.jev_ms for _, s in pairs if s.jev_ms]):.0f} ms (median per item)")
    for j, s in pairs:
        for tag, i in (("jev", j), ("llm", s)):
            if i.errors:
                print(f"  {tag} error on {i.title[:50]}: {i.errors[-1][:100]}")
    print(f"\n  per-item detail: {args.out}")


if __name__ == "__main__":
    main()
