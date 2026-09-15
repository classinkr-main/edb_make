"""Web trial settings read once from environment variables.

Tests build TrialConfig directly; nothing else in the trial reads os.environ.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from trial_input import A3_AREA_PT, InputLimits

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


@dataclass(frozen=True)
class TrialConfig:
    production: bool = False
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
            max_pages=3,
            max_source_pages=100,
            max_page_area_pt=2 * A3_AREA_PT,
        )
    )
    daily_limit: int = 3
    global_daily_limit: int = 500
    parse_concurrency: int = 2
    parse_wait_seconds: float = 20.0

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
                max_pages=_positive_int(env, "TRIAL_MAX_PAGES", 3),
                max_source_pages=_positive_int(env, "TRIAL_MAX_SOURCE_PAGES", 100),
                max_page_area_pt=2 * A3_AREA_PT,
                max_words_per_page=_positive_int(env, "TRIAL_MAX_WORDS_PER_PAGE", 8000),
                max_drawings_per_page=_positive_int(env, "TRIAL_MAX_DRAWINGS_PER_PAGE", 10000),
            ),
            daily_limit=_positive_int(env, "TRIAL_DAILY_LIMIT", 3),
            global_daily_limit=_positive_int(env, "TRIAL_GLOBAL_DAILY_LIMIT", 500),
            parse_concurrency=_positive_int(env, "TRIAL_PARSE_CONCURRENCY", 2),
            parse_wait_seconds=_positive_float(env, "TRIAL_PARSE_WAIT_SECONDS", 20.0),
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
