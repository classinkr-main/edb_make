"""Fire N concurrent uploads at a preview deployment and report the distribution.

Usage:
  VERCEL_AUTOMATION_BYPASS_SECRET=... .venv/bin/python scripts/trial_bench/load.py https://<preview>.vercel.app exam.pdf --concurrency 10 --waves 1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.trial_bench.common import markdown_table, percentile  # noqa: E402
from scripts.trial_bench.probe import call_parse  # noqa: E402

COLD_AGE_S = 30.0


def run_wave(base_url: str, payload: bytes, concurrency: int, bypass_secret: str | None) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(call_parse, base_url, payload, bypass_secret=bypass_secret, timeout=120.0) for _ in range(concurrency)]
        return [future.result() for future in futures]


def summarize_wave(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [row for row in rows if row["status"] == 200]
    walls = [row["wall_ms"] for row in ok]
    per_instance = Counter(row["instance_id"] for row in ok)
    return {
        "requests": len(rows),
        "ok": len(ok),
        "busy": sum(1 for row in rows if row["status"] == 503),
        "failed_other": sum(1 for row in rows if row["status"] not in (200, 503)),
        "p50_ms": percentile(walls, 50),
        "p95_ms": percentile(walls, 95),
        "max_ms": max(walls) if walls else None,
        "instances": len(per_instance),
        "max_per_instance": max(per_instance.values()) if per_instance else 0,
        "cold": sum(1 for row in ok if (row["instance_age_s"] or 0) < COLD_AGE_S),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--waves", type=int, default=1)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)
    bypass = os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET") or None
    payload = args.pdf.read_bytes()
    table = []
    for wave in range(1, args.waves + 1):
        rows = run_wave(args.base_url, payload, args.concurrency, bypass)
        if args.json:
            with args.json.open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps({"wave": wave, "concurrency": args.concurrency, **row}, ensure_ascii=False) + "\n")
        summary = summarize_wave(rows)
        table.append([wave, args.concurrency, summary["ok"], summary["busy"], summary["failed_other"], summary["p50_ms"], summary["p95_ms"], summary["max_ms"], summary["instances"], summary["max_per_instance"], summary["cold"]])
    print(markdown_table(["wave", "concurrency", "ok", "busy", "other_fail", "p50_ms", "p95_ms", "max_ms", "instances", "max_per_inst", "cold"], table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
