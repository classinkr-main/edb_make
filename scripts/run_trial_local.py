"""Run the web trial locally with public/ served next to the API.

On Vercel the CDN serves public/ and the function never mounts it, so the
mount lives only in this script. Quotas stay in memory and nothing reaches
Supabase. With --turnstile-test the page uses Cloudflare's always-pass
dummy keys.

Usage:
  .venv/bin/python scripts/run_trial_local.py [--port 8790] [--turnstile-test]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from trial_config import TrialConfig  # noqa: E402
from trial_server import create_app  # noqa: E402
from trial_turnstile import TEST_SECRET_ALWAYS_PASSES, TEST_SITE_KEY_ALWAYS_PASSES  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--turnstile-test", action="store_true", help="use Cloudflare's always-pass test keys")
    parser.add_argument("--daily-limit", type=int, default=3)
    args = parser.parse_args()

    config = TrialConfig(
        turnstile_site_key=TEST_SITE_KEY_ALWAYS_PASSES if args.turnstile_test else None,
        turnstile_secret=TEST_SECRET_ALWAYS_PASSES if args.turnstile_test else None,
        daily_limit=args.daily_limit,
    )
    app = create_app(config)
    app.mount("/", StaticFiles(directory=ROOT / "public", html=True), name="public")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
