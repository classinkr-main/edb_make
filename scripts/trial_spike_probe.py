"""Call the deployed trial spike endpoint and print cold/warm timings.

Usage:
  TRIAL_SPIKE_TOKEN=... python scripts/trial_spike_probe.py https://<deployment> exam.pdf [--repeat 5]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def call(base_url: str, payload: bytes, token: str) -> tuple[int, float, dict]:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/spike",
        data=payload,
        method="POST",
        headers={"content-type": "application/pdf", "x-spike-token": token},
    )
    started_at = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    wall_ms = (time.perf_counter() - started_at) * 1000
    try:
        body = json.loads(raw)
    except ValueError:
        body = {"raw": raw[:200].decode("utf-8", "replace")}
    return status, wall_ms, body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    token = os.environ.get("TRIAL_SPIKE_TOKEN", "")
    if not token:
        print("TRIAL_SPIKE_TOKEN is not set", file=sys.stderr)
        return 2
    payload = args.pdf.read_bytes()
    print(f"file={args.pdf.name} bytes={len(payload)}")
    for attempt in range(1, args.repeat + 1):
        status, wall_ms, body = call(args.base_url, payload, token)
        print(
            json.dumps(
                {
                    "attempt": attempt,
                    "status": status,
                    "wall_ms": round(wall_ms),
                    "request_index": body.get("request_index"),
                    "import_ms": body.get("import_ms"),
                    "parse_ms": body.get("parse_ms"),
                    "max_rss_mb": body.get("max_rss_mb"),
                    "pages": body.get("page_count"),
                    "problems": len(body.get("problem_numbers") or []),
                    "python": body.get("python"),
                    "error": body.get("error") or body.get("raw"),
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
