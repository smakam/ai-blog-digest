"""CLI entry point: `python -m digest` (daily run) or `python -m digest --dry-run`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback

from digest.classify import JevClassifier
from digest.config import Secrets, load_config
from digest.fetch import Fetcher
from digest.pipeline import run
from digest.runlog import RunLog
from digest.state import SeenState
from digest.summarize import Summarizer
from digest.telegram import TelegramClient


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Blog Digest")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the digest instead of sending it; don't update state.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Keep the SDK/httpx from logging request bodies or URLs with tokens at INFO.
    for noisy in ("httpx", "httpx2", "typesafe_sdk", "trafilatura"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    telegram = None
    try:
        config = load_config(args.config)
        if args.dry_run:
            api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError("OPENROUTER_API_KEY is required even for --dry-run")
        else:
            secrets = Secrets.from_env()
            api_key = secrets.openrouter_api_key
            telegram = TelegramClient(secrets.telegram_bot_token, secrets.telegram_chat_id)

        fetcher = Fetcher(config.fetch)
        classifier = JevClassifier(config.jev, api_key)
        summarizer = Summarizer(config.summarizer, api_key)
        try:
            result = run(
                config=config,
                fetcher=fetcher,
                classifier=classifier,
                summarizer=summarizer,
                telegram=telegram,
                state=SeenState(config.state_path, config.state_retention_days),
                runlog=RunLog(config.log_dir, config.option),
                dry_run=args.dry_run,
            )
        finally:
            fetcher.close()
            classifier.close()
            summarizer.close()
        logging.info("Done: %d new items processed", len(result.items))
        return 0
    except Exception as exc:  # noqa: BLE001 — report every failure to Telegram, never fail silently
        logging.error("Run failed:\n%s", traceback.format_exc())
        if telegram is not None and "Digest delivery failed" not in str(exc):
            try:
                telegram.send_error(f"Run failed: {type(exc).__name__}: {str(exc)[:500]}")
            except Exception as notify_exc:  # noqa: BLE001
                logging.error("Could not send error notice: %s", notify_exc)
        return 1
    finally:
        if telegram is not None:
            telegram.close()


if __name__ == "__main__":
    sys.exit(cli())
