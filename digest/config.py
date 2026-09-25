"""Configuration: config.yaml merged with environment overrides. Secrets come only from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class JevConfig:
    model: str = "jev-1.13"
    base_url: str = "https://openrouter.ai/api"
    max_input_tokens: int = 20000
    timeout_seconds: float = 30


@dataclass
class SummarizerConfig:
    model: str = "anthropic/claude-haiku-4.5"
    base_url: str = "https://openrouter.ai/api/v1"
    max_input_chars: int = 60000
    timeout_seconds: float = 90


@dataclass
class Thresholds:
    high: float = 0.7
    medium: float = 0.4


@dataclass
class FetchConfig:
    timeout_seconds: float = 30
    max_workers: int = 8
    user_agent: str = "Mozilla/5.0 (compatible; ai-blog-digest/0.1)"
    min_full_text_chars: int = 1500


@dataclass
class Secrets:
    openrouter_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str

    @classmethod
    def from_env(cls) -> Secrets:
        missing = [
            name
            for name in ("OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
            if not os.environ.get(name, "").strip()
        ]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        return cls(
            openrouter_api_key=os.environ["OPENROUTER_API_KEY"].strip(),
            telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"].strip(),
            telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"].strip(),
        )


@dataclass
class Config:
    option: str = "local"
    opml_path: str = "feeds.opml"
    lookback_hours: float = 36
    max_summaries: int = 5
    jev: JevConfig = field(default_factory=JevConfig)
    summarizer: SummarizerConfig = field(default_factory=SummarizerConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)
    state_path: str = "state/{option}.json"
    state_retention_days: int = 30
    log_dir: str = "logs"
    fetch: FetchConfig = field(default_factory=FetchConfig)


# env var -> (section or None, key, type)
_ENV_OVERRIDES = {
    "DIGEST_OPTION": (None, "option", str),
    "DIGEST_OPML_PATH": (None, "opml_path", str),
    "DIGEST_LOOKBACK_HOURS": (None, "lookback_hours", float),
    "DIGEST_MAX_SUMMARIES": (None, "max_summaries", int),
    "DIGEST_STATE_PATH": (None, "state_path", str),
    "DIGEST_LOG_DIR": (None, "log_dir", str),
    "JEV_MODEL": ("jev", "model", str),
    "JEV_BASE_URL": ("jev", "base_url", str),
    "JEV_MAX_INPUT_TOKENS": ("jev", "max_input_tokens", int),
    "SUMMARY_MODEL": ("summarizer", "model", str),
    "DIGEST_THRESHOLD_HIGH": ("thresholds", "high", float),
    "DIGEST_THRESHOLD_MEDIUM": ("thresholds", "medium", float),
}

_SECTIONS = {
    "jev": JevConfig,
    "summarizer": SummarizerConfig,
    "thresholds": Thresholds,
    "fetch": FetchConfig,
}


def load_config(path: str | Path | None = "config.yaml") -> Config:
    raw: dict = {}
    if path and Path(path).exists():
        raw = yaml.safe_load(Path(path).read_text()) or {}

    for env_name, (section, key, cast) in _ENV_OVERRIDES.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            target = raw.setdefault(section, {}) if section else raw
            target[key] = cast(value)

    kwargs = {}
    for key, value in raw.items():
        if key in _SECTIONS:
            kwargs[key] = _SECTIONS[key](**(value or {}))
        elif key in Config.__dataclass_fields__:
            kwargs[key] = value
        else:
            raise ValueError(f"Unknown config key: {key}")
    config = Config(**kwargs)
    config.state_path = config.state_path.format(option=config.option)

    if not 0 <= config.thresholds.medium <= config.thresholds.high <= 1:
        raise ValueError("Thresholds must satisfy 0 <= medium <= high <= 1")
    return config
