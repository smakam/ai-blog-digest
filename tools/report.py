"""Week-one review and option comparison over the JSONL logs.

  python tools/report.py review  [--log-dir logs] [--out review.csv]
      One row per item (latest classification per option), with empty `manual_is_ai` and
      `manual_bucket` columns to fill in by hand.

  python tools/report.py compare [--log-dir logs] [--review review.csv]
      Per-option reliability, latency, and cost; Jev score agreement across options on the same
      items; and, if a filled-in review CSV is given, agreement of each option with the manual marks.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

REVIEW_FIELDS = ["option", "run_id", "published", "source", "title", "url", "is_ai", "worthiness",
                 "bucket", "full_text", "content_source", "delivered", "manual_is_ai", "manual_bucket", "notes"]


def load(log_dir: Path) -> tuple[list[dict], list[dict]]:
    items, runs = [], []
    for path in sorted(log_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            (items if record["type"] == "item" else runs).append(record)
    return items, runs


def latest_per_option(items: list[dict]) -> dict[tuple[str, str], dict]:
    """(option, item_id) -> the most recent record (an item retried after a Jev error appears twice)."""
    latest: dict[tuple[str, str], dict] = {}
    for record in sorted(items, key=lambda r: r["run_id"]):
        latest[(record["option"], record["item_id"])] = record
    return latest


def cmd_review(args) -> None:
    items, _ = load(Path(args.log_dir))
    rows = sorted(latest_per_option(items).values(), key=lambda r: (r["option"], r["published"]))
    with open(args.out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=REVIEW_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "manual_is_ai": "", "manual_bucket": "", "notes": ""})
    print(f"Wrote {len(rows)} rows to {args.out}. Fill manual_is_ai (yes/no) and manual_bucket "
          f"(high/medium/low) for a sample, then run: python tools/report.py compare --review {args.out}")


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "n/a"


def cmd_compare(args) -> None:
    items, runs = load(Path(args.log_dir))
    latest = latest_per_option(items)
    options = sorted({r["option"] for r in runs} | {o for o, _ in latest})

    print("== Per option ==")
    header = f"{'option':<16}{'runs':>5}{'ok':>5}{'days':>6}{'items':>7}{'hi':>5}{'med':>5}" \
             f"{'run s':>8}{'jev ms':>8}{'llm ms':>8}{'$/run':>10}{'$/1k items':>12}  models"
    print(header)
    for option in options:
        opt_runs = [r for r in runs if r["option"] == option and not r.get("dry_run")]
        opt_items = [r for (o, _), r in latest.items() if o == option]
        ok = sum(1 for r in opt_runs if r.get("delivered"))
        days = len({r["started"][:10] for r in opt_runs})
        cost = sum((r.get("jev_cost") or 0) + (r.get("llm_cost") or 0) for r in opt_runs)
        n_items = sum(r.get("items_new", 0) for r in opt_runs)
        jev_ms = [r["jev_ms"] for r in opt_items if r.get("jev_ms")]
        llm_ms = [r["llm_ms"] for r in opt_items if r.get("llm_ms")]
        models = sorted({r.get("summary_model", "?") for r in opt_runs})
        print(f"{option:<16}{len(opt_runs):>5}{ok:>5}{days:>6}{len(opt_items):>7}"
              f"{sum(1 for r in opt_items if r['bucket'] == 'high'):>5}"
              f"{sum(1 for r in opt_items if r['bucket'] == 'medium'):>5}"
              f"{statistics.mean([r['duration_s'] for r in opt_runs]) if opt_runs else 0:>8.1f}"
              f"{statistics.median(jev_ms) if jev_ms else 0:>8.0f}"
              f"{statistics.median(llm_ms) if llm_ms else 0:>8.0f}"
              f"{cost / len(opt_runs) if opt_runs else 0:>10.4f}"
              f"{1000 * cost / n_items if n_items else 0:>12.4f}  {', '.join(models)}")

    # Jev consistency: same item scored by several options should get (nearly) the same scores.
    by_url: dict[str, dict[str, dict]] = defaultdict(dict)
    for (option, _), record in latest.items():
        if record.get("worthiness") is not None:
            by_url[record["url"]][option] = record
    shared = {url: recs for url, recs in by_url.items() if len(recs) > 1}
    print(f"\n== Jev consistency across options ({len(shared)} shared items) ==")
    if shared:
        spreads = [max(r["worthiness"] for r in recs.values()) - min(r["worthiness"] for r in recs.values())
                   for recs in shared.values()]
        bucket_agree = sum(1 for recs in shared.values() if len({r["bucket"] for r in recs.values()}) == 1)
        print(f"bucket agreement: {_pct(bucket_agree, len(shared))}; "
              f"worthiness spread median {statistics.median(spreads):.3f}, max {max(spreads):.3f}")
        mismatches = sorted(shared.items(), key=lambda kv: -max(r["worthiness"] for r in kv[1].values())
                            + min(r["worthiness"] for r in kv[1].values()))[:10]
        for url, recs in mismatches:
            detail = ", ".join(f"{o}={r['worthiness']:.2f}/{r['content_source']}/{r['text_chars']}ch"
                               for o, r in sorted(recs.items()))
            print(f"  {url}\n    {detail}")

    if args.review:
        print("\n== Jev quality vs manual marks ==")
        with open(args.review) as fh:
            marks = {row["url"]: row for row in csv.DictReader(fh)
                     if row.get("manual_is_ai") or row.get("manual_bucket")}
        for option in options:
            ai_n = ai_ok = b_n = b_ok = 0
            for (o, _), record in latest.items():
                mark = marks.get(record["url"])
                if o != option or not mark or record.get("is_ai") is None:
                    continue
                if mark.get("manual_is_ai"):
                    ai_n += 1
                    ai_ok += (record["is_ai"] >= 0.5) == (mark["manual_is_ai"].strip().lower() in ("yes", "y", "1", "true"))
                if mark.get("manual_bucket"):
                    b_n += 1
                    predicted = record["bucket"] if record["bucket"] != "not_ai" else "low"
                    b_ok += predicted == mark["manual_bucket"].strip().lower()
            print(f"{option:<16} is_ai agreement {_pct(ai_ok, ai_n)} (n={ai_n}); "
                  f"bucket agreement {_pct(b_ok, b_n)} (n={b_n})")

    failed = [r for r in runs if r.get("delivery_error") or r.get("feed_failures") or r.get("jev_failures")]
    if failed:
        print("\n== Runs with problems ==")
        for r in failed:
            print(f"  {r['run_id']}: feeds failed={r.get('feed_failures')}, jev failed={r.get('jev_failures')}, "
                  f"llm failed={r.get('llm_failures')}, delivery={r.get('delivery_error') or 'ok'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    review = sub.add_parser("review")
    review.add_argument("--log-dir", default="logs")
    review.add_argument("--out", default="review.csv")
    review.set_defaults(func=cmd_review)
    compare = sub.add_parser("compare")
    compare.add_argument("--log-dir", default="logs")
    compare.add_argument("--review")
    compare.set_defaults(func=cmd_compare)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
