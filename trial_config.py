"""Web trial settings read once from environment variables.

Tests build TrialConfig directly; nothing else in the trial reads os.environ.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from trial_demo import DemoConfig

from trial_input import (
    A3_AREA_PT,
    DEFAULT_MAX_DRAWINGS_PER_PAGE,
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_WORDS_PER_PAGE,
    InputLimits,
)

DEFAULT_INQUIRY_URL = "https://classin.co.kr/contact"


def _text(env: Mapping[str, str], name: str) -> str | None:
    value = (env.get(name) or "").strip()
    return value or None


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _text(env, name)
    if raw is None:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _text(env, name)
    if raw is None:
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


_FLAG_TRUE = frozenset({"1", "true", "yes", "on"})
_FLAG_FALSE = frozenset({"0", "false", "no", "off"})


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _text(env, name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _FLAG_TRUE:
        return True
    if lowered in _FLAG_FALSE:
        return False
    raise ValueError(f"{name} must be one of 1/0, true/false, yes/no, on/off")


@dataclass(frozen=True)
class TrialConfig:
    production: bool = False
    demo: DemoConfig = field(default_factory=DemoConfig)
    inquiry_url: str = DEFAULT_INQUIRY_URL
    turnstile_site_key: str | None = None
    turnstile_secret: str | None = None
    supabase_url: str | None = None
    supabase_secret_key: str | None = None
    ip_salt: str | None = None
    cron_secret: str | None = None
    expected_hostnames: frozenset[str] = frozenset()
    limits: InputLimits = field(
        default_factory=lambda: InputLimits(
            max_bytes=4_000_000,
            max_pages=DEFAULT_MAX_PAGES,
            max_source_pages=100,
            max_page_area_pt=2 * A3_AREA_PT,
        )
    )
    daily_limit: int = 3
    global_daily_limit: int = 500
    # Four-page science PDFs can peak above 1.3 GiB when two overlap.
    # Start with one parse per 2 GiB instance; cloud instances can still scale out.
    parse_concurrency: int = 1
    parse_wait_seconds: float = 20.0
    # Chalk-cutout previews next to the raw crops: +4-6 s and +0.1-0.35 GB per parse on
    # Vercel (spec 2026-09-16 §2-2). Off turns the cutouts, the response field and the
    # page toggle off together.
    board_previews: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "TrialConfig":
        inquiry_url = _text(env, "TRIAL_INQUIRY_URL") or DEFAULT_INQUIRY_URL
        if not inquiry_url.startswith("https://"):
            raise ValueError("TRIAL_INQUIRY_URL must be an https URL")
        hostnames = frozenset(
            part.strip() for part in (env.get("TRIAL_HOSTNAMES") or "").split(",") if part.strip()
        )
        return cls(
            production=(env.get("VERCEL_ENV") or "").strip() == "production",
            demo=DemoConfig.from_env(env),
            inquiry_url=inquiry_url,
            turnstile_site_key=_text(env, "TRIAL_TURNSTILE_SITE_KEY"),
            turnstile_secret=_text(env, "TRIAL_TURNSTILE_SECRET"),
            supabase_url=_text(env, "SUPABASE_URL"),
            supabase_secret_key=_text(env, "SUPABASE_SECRET_KEY"),
            ip_salt=_text(env, "TRIAL_IP_SALT"),
            cron_secret=_text(env, "CRON_SECRET"),
            expected_hostnames=hostnames,
            limits=InputLimits(
                max_bytes=_positive_int(env, "TRIAL_MAX_BYTES", 4_000_000),
                max_pages=_positive_int(env, "TRIAL_MAX_PAGES", DEFAULT_MAX_PAGES),
                max_source_pages=_positive_int(env, "TRIAL_MAX_SOURCE_PAGES", 100),
                max_page_area_pt=2 * A3_AREA_PT,
                max_words_per_page=_positive_int(env, "TRIAL_MAX_WORDS_PER_PAGE", DEFAULT_MAX_WORDS_PER_PAGE),
                max_drawings_per_page=_positive_int(
                    env, "TRIAL_MAX_DRAWINGS_PER_PAGE", DEFAULT_MAX_DRAWINGS_PER_PAGE
                ),
            ),
            daily_limit=_positive_int(env, "TRIAL_DAILY_LIMIT", 3),
            global_daily_limit=_positive_int(env, "TRIAL_GLOBAL_DAILY_LIMIT", 500),
            parse_concurrency=_positive_int(env, "TRIAL_PARSE_CONCURRENCY", 1),
            parse_wait_seconds=_positive_float(env, "TRIAL_PARSE_WAIT_SECONDS", 20.0),
            board_previews=_flag(env, "TRIAL_BOARD_PREVIEWS", True),
        )

    def missing_production_settings(self) -> list[str]:
        """Secrets production cannot run without; empty outside production."""
        if not self.production:
            return []
        required = (
            ("TRIAL_TURNSTILE_SITE_KEY", self.turnstile_site_key),
            ("TRIAL_TURNSTILE_SECRET", self.turnstile_secret),
            ("SUPABASE_URL", self.supabase_url),
            ("SUPABASE_SECRET_KEY", self.supabase_secret_key),
            ("TRIAL_IP_SALT", self.ip_salt),
            ("CRON_SECRET", self.cron_secret),
        )
        return [name for name, value in required if not value]
