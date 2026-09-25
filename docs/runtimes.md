# Running the digest on each option

Both options run the identical pipeline: same code, `config.yaml`, `feeds.opml`, Jev for
classification, and Claude Haiku 4.5 (via OpenRouter) for summaries. Only the runtime differs, and
`DIGEST_OPTION` labels each one's logs and state.

| Option | Runtime | `DIGEST_OPTION` | Schedule (IST) |
|---|---|---|---|
| A | GitHub Actions cron | `gha` | 07:00 |
| B | Claude Code remote routine | `routine` | 07:15 |

State lives in `state/<option>.json` and logs in `logs/<date>_<option>.jsonl`, so all options can
share one repo and commit to it without colliding.

Required secrets on every runtime: `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

Required outbound network: `openrouter.ai`, `api.telegram.org`, every feed host in the OPML, and
arbitrary article hosts (for full-text extraction). Restrictive allowlists will show up as feed
failures and `title_summary` content sources in the logs.

---

## A. GitHub Actions

Already wired up in [`.github/workflows/digest.yml`](../.github/workflows/digest.yml).

1. Push this repo to GitHub (a private repo is fine).
2. **Settings → Secrets and variables → Actions**:
   - Secrets: `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
3. **Actions → AI Blog Digest → Run workflow** for a manual run (tick *dry run* first to check the
   output in the job log without sending).

Schedule: `30 1 * * *` UTC = 07:00 IST. GitHub may start scheduled jobs late at busy times, and
disables schedules on repos with no activity for 60 days (the daily state commit prevents that).

State and logs are committed back to the default branch after every run.

---

## B. Claude Code remote routine

1. Push the repo to GitHub and connect it to Claude Code on the web.
2. Create a cloud **environment** for the routine:
   - **Environment variables**: `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
     (`DIGEST_OPTION=routine` is set in the prompt).
   - **Network access**: full (unrestricted) access — blog domains are arbitrary. If you must use an
     allowlist, include `openrouter.ai`, `api.telegram.org`, and your feed hosts, and expect more
     title-only fallbacks.
   - **Setup script**: `pip install -e .`
3. Create the routine (`/schedule` in Claude Code, or claude.ai/code → Routines) with:
   - Repository: this repo, with **Allow unrestricted branch pushes** enabled (otherwise routines can
     only push to `claude/`-prefixed branches — in that case set the state branch accordingly).
   - Schedule: daily, custom cron `30 1 * * *` (UTC).
   - Prompt: the contents of [`routine-prompt.md`](routine-prompt.md).
4. Manual run: **Run now** on the routine, or its API `/fire` trigger.

The routine is an agent session, so the prompt tells it to run the script verbatim and not to
improvise; the Python code, not the agent, makes every decision. Check the session transcript when
debugging.

---

## C. Claude Cowork scheduled task

1. In Claude Desktop → Cowork, create a scheduled task: daily at 07:00 (IST local time).
2. Give it access to a folder containing a checkout of this repo.
3. Provide the secrets as environment variables available to that task (not in the prompt), plus
   `DIGEST_OPTION=cowork` 
4. Prompt: the contents of [`routine-prompt.md`](routine-prompt.md).

> **Check before relying on it:** at the time of writing, Cowork scheduled tasks may run on your
> own machine and only fire while the desktop app is open and the computer is awake. That would fail
> the "runs without my laptop on" requirement — record it under *Reliability* in the comparison
> rather than silently switching it off.

---

## Comparing after a week

```bash
git pull                                   # collect logs from every option
python tools/report.py review --out review.csv
# mark manual_is_ai / manual_bucket for a sample of rows in review.csv
python tools/report.py compare --review review.csv
```

`compare` prints, per option: runs, successful deliveries, distinct days, items, High/Medium counts,
mean run time, median Jev / LLM latency, cost per run and per 1,000 items (from OpenRouter's
`usage.cost`); then Jev score agreement across options on shared items (mismatches list the content
source and text length, which usually explains the difference); then agreement with your manual marks.

Summary quality is judged by reading: the `summary` field is in each option's item logs for the same
High items.
