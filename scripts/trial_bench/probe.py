"""Measure the trial parse endpoint on a preview deployment, file by file.

Usage:
  VERCEL_AUTOMATION_BYPASS_SECRET=... .venv/bin/python scripts/trial_bench/probe.py https://<preview>.vercel.app a.pdf b.pdf --repeat 5

Preview deployments skip Turnstile (no secret in the Preview env) and keep
quotas in memory; the bypass header gets through Vercel's login protection.
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
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.trial_bench.common import markdown_table, percentile  # noqa: E402

BYPASS_HEADER = "x-vercel-protection-bypass"


def call_parse(base_url: str, payload: bytes, *, bypass_secret: str | None, timeout: float = 90.0) -> dict[str, Any]:
    headers = {"content-type": "application/pdf"}
    if bypass_secret:
        headers[BYPASS_HEADER] = bypass_secret
    request = urllib.request.Request(base_url.rstrip("/") + "/api/parse", data=payload, method="POST", headers=headers)
    started_at = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {"status": 0, "wall_ms": round((time.perf_counter() - started_at) * 1000), "bytes": 0, "timing_ms": {}, "instance_id": None, "instance_age_s": None, "error": repr(error)}
    wall_ms = round((time.perf_counter() - started_at) * 1000)
    try:
        body = json.loads(raw)
    except ValueError:
        body = {}
    return {
        "status": status,
        "wall_ms": wall_ms,
        "bytes": len(raw),
        "elapsed_ms": body.get("elapsed_ms"),
        "timing_ms": body.get("timing_ms") or {},
        "instance_id": body.get("instance_id"),
        "instance_age_s": body.get("instance_age_s"),
        "problems": len(body.get("problems") or []),
        "error": (body.get("error") or {}).get("code") if isinstance(body.get("error"), dict) else None,
    }


def summarize_file(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [row for row in rows if row["status"] == 200]
    warm = ok[1:] or ok
    stage = lambda key: percentile([row["timing_ms"].get(key, 0) for row in warm], 50)  # noqa: E731
    return {
        "first_wall_ms": rows[0]["wall_ms"] if rows else None,
        "warm_p50_ms": percentile([row["wall_ms"] for row in warm], 50),
        "warm_max_ms": max((row["wall_ms"] for row in warm), default=None),
        "parse_p50_ms": stage("total"),
        "render_p50_ms": stage("render"),
        "segment_p50_ms": stage("segment"),
        "assets_p50_ms": stage("assets"),
        "instances": sorted({row["instance_id"] for row in ok if row["instance_id"]}),
        "bytes_max": max((row["bytes"] for row in ok), default=0),
        "failures": len(rows) - len(ok),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url")
    parser.add_argument("pdfs", nargs="+", type=Path)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--json", type=Path, default=None, help="append raw rows as JSON lines")
    args = parser.parse_args(argv)
    bypass = os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET") or None
    table = []
    for pdf in args.pdfs:
        payload = pdf.read_bytes()
        rows = []
        for attempt in range(args.repeat):
            row = call_parse(args.base_url, payload, bypass_secret=bypass)
            row.update(file=pdf.name, attempt=attempt + 1)
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), file=sys.stderr)
            if args.json:
                with args.json.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        summary = summarize_file(rows)
        table.append([pdf.name, len(payload), summary["first_wall_ms"], summary["warm_p50_ms"], summary["warm_max_ms"], summary["parse_p50_ms"], summary["render_p50_ms"], summary["segment_p50_ms"], summary["assets_p50_ms"], summary["bytes_max"], len(summary["instances"]), summary["failures"]])
    print(markdown_table(["file", "bytes", "first_wall", "warm_p50", "warm_max", "parse_p50", "render", "segment", "assets", "resp_bytes", "instances", "failures"], table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
