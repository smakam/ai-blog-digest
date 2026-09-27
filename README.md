# AI Blog Digest

A daily job that reads your blog subscriptions (a Feedly OPML export), uses **Jev**
(TypeSafe's decision model) to keep only worthwhile AI posts, has an **LLM** summarize the best ones,
and sends one digest to **Telegram**. Jev and the summarizer are both reached through **OpenRouter**.

```
feeds.opml ─► fetch last 36h ─► skip seen ─► resolve text ─► Jev (is_ai + worthiness)
          ─► policy (drop non-AI, bucket) ─► LLM summaries (High only) ─► Telegram
          ─► state/<option>.json + logs/<date>_<option>.jsonl
```

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

```bash
export OPENROUTER_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
```

```bash
.venv/bin/python -m digest --dry-run
```

`--dry-run` prints the digest instead of sending it and leaves state untouched (it still calls Jev and
the summarizer, so it costs a little). Drop the flag for a real run. Tests: `.venv/bin/pytest`.

Replace `feeds.opml` with your own export (Feedly → Organize → Export OPML).

## How it works

| Spec | Where |
|---|---|
| §1 OPML input, 36h lookback (`lookback_hours`; wider than 24h so a late or skipped run loses nothing), content resolution (feed full text → article extraction → title+summary) | [digest/opml.py](digest/opml.py), [digest/fetch.py](digest/fetch.py) |
| §2 One Jev call per item: `is_ai` (noul) + `worthiness` (5-level score, normalized to 0–1); ~20K-token cap; content only, no author | [digest/classify.py](digest/classify.py) |
| §3 Drop non-AI; High ≥ 0.7, Medium ≥ 0.4; author-boost hook (`ScoreAdjuster`, unused in v1) | [digest/scoring.py](digest/scoring.py) |
| §4 4–5 line summaries of the top 5 High items (`max_summaries`; the rest are listed as links), one shared prompt, model ID per option | [digest/summarize.py](digest/summarize.py) |
| §5 One Telegram message: High = summary + link, Medium = title + link | [digest/telegram.py](digest/telegram.py) |
| §7 Never deliver twice: seen-ID state, pruned after 30 days | [digest/state.py](digest/state.py) |
| §9 Per-item JSONL log + error notices to Telegram | [digest/runlog.py](digest/runlog.py), [digest/pipeline.py](digest/pipeline.py) |

### Jev via the TypeSafe SDK

The classifier uses the official `typesafe-sdk` with `base_url=https://openrouter.ai/api` and model
`jev-1.13`. The SDK posts to `/v1/systemone`, which OpenRouter serves as the SDK-compatible route to
its Decisions API (`POST /api/alpha/decisions`); the wire format is the same. To go native later,
set `JEV_BASE_URL=https://api.typesafe.ai` and swap in a TypeSafe key. The Decisions API is alpha,
so the request format may change; the SDK is the single place to absorb that.

The worthiness rubric (`WORTHINESS_LEVELS` in `classify.py`) spells out High vs Low content from
the spec. Jev returns a probability-weighted level (0–4), which is divided by 4 to get the 0–1 score
the thresholds apply to. `is_ai` is a probability; ≥ 0.5 counts as AI.

### Failure handling

- A broken feed, a Jev error on one item, or an LLM error on one summary does not stop the run.
  They are collected and sent as one short ⚠️ notice after the digest.
- Items Jev failed on are not marked seen, so the next run retries them if they're still in the window.
- A High item whose summary failed is still delivered with its link, marked "(summary unavailable)".
- If delivery fails, state is not saved (nothing is lost) and the process exits non-zero.
- Any other crash sends "Run failed: …" to Telegram and exits non-zero.

## Configuration

Everything non-secret is in [config.yaml](config.yaml); each key lists its environment override.
The ones you'll change most:

| Env var | Purpose |
|---|---|
| `DIGEST_OPTION` | Label for this deployment (`gha`, `routine`, `cowork`); keys state and logs |
| `SUMMARY_MODEL` | OpenRouter model ID for summaries (default `anthropic/claude-haiku-4.5`) |
| `DIGEST_THRESHOLD_HIGH` / `DIGEST_THRESHOLD_MEDIUM` | Bucket thresholds |
| `DIGEST_OPML_PATH` | Feed list |
| `JEV_MODEL`, `JEV_BASE_URL` | Jev model / endpoint |

### Secrets

`OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — environment only, never in the
repo. For Telegram: create a bot with @BotFather, send it a message, then read your chat ID from
`https://api.telegram.org/bot<token>/getUpdates`.

The run time (07:15 IST for the routine) is set in the runtime's scheduler, not here.

## Runtimes and comparison

The digest runs as a Claude Code remote routine; the GitHub Actions workflow is disabled.
See [docs/runtimes.md](docs/runtimes.md) for routine, GitHub Actions, and Cowork setup,
and for the one-week comparison with `tools/report.py`.

## Logs

Each run appends to `logs/<date>_<option>.jsonl`: one `item` record per new item (title, URL,
source, is_ai, worthiness, bucket, full_text, content_source, delivered, summary, latency, cost,
errors) and one `run` record (counts, duration, total cost, models, delivery status).

```bash
python tools/report.py review     # CSV for the week-one manual review of Jev
python tools/report.py compare    # options side by side
```
