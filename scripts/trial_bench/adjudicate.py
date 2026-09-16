"""Side-by-side crops for every trial/oracle disagreement, plus a labels skeleton.

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/adjudicate.py [case ...]

Fable reviews adjudication/<case>/<key>.png and fills labels/<case>.json:
truth = "trial" | "oracle" | "both" | "neither", then status = "approved".
Existing label files are never overwritten.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.trial_bench.common import BENCH_ROOT, bench_dir, load_json, markdown_table, save_json  # noqa: E402
from scripts.trial_bench.score import LOW_IOU, regions_iou  # noqa: E402

PANEL_MAX = (900, 1200)


def disagreements(trial: dict[str, Any], oracle: dict[str, Any], low_iou: float = LOW_IOU) -> list[dict[str, Any]]:
    trial_by_key = {problem["key"]: problem for problem in trial["problems"]}
    oracle_by_key = {problem["key"]: problem for problem in oracle["problems"]}
    items = []
    for key in sorted(set(trial_by_key) | set(oracle_by_key)):
        t, o = trial_by_key.get(key), oracle_by_key.get(key)
        if t and o:
            iou = regions_iou(t["regions"], o["regions"])
            if iou >= low_iou:
                continue
            reason = f"iou {iou:.2f}"
        else:
            reason = "trial only" if t else "oracle only"
        items.append({"key": key, "reason": reason, "trial": t, "oracle": o})
    return items


def labels_skeleton(case: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "case": case,
        "status": "pending",
        "items": [{"key": item["key"], "reason": item["reason"], "truth": None, "note": ""} for item in items],
    }


def _panel(entry: dict[str, Any] | None) -> Image.Image:
    crop = entry.get("crop") if entry else None
    if crop and Path(crop).is_file():
        with Image.open(crop) as image:
            panel = image.convert("RGB")
            panel.thumbnail(PANEL_MAX)
            return panel
    return Image.new("RGB", (400, 200), "lightgray")


def compose(item: dict[str, Any], out_path: Path) -> None:
    panels = [_panel(item["trial"]), _panel(item["oracle"])]
    canvas = Image.new("RGB", (sum(panel.width for panel in panels) + 30, max(panel.height for panel in panels) + 40), "white")
    draw = ImageDraw.Draw(canvas)
    x = 10
    for side, panel in zip(("trial", "oracle"), panels):
        draw.text((x, 8), f"{side}: {item['key']} ({item['reason']})", fill="black")
        canvas.paste(panel, (x, 30))
        x += panel.width + 10
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)


def adjudicate_case(case: str, root: Path = BENCH_ROOT) -> int:
    trial = load_json(bench_dir("trial", root) / f"{case}.json")
    oracle = load_json(bench_dir("oracle", root) / f"{case}.json")
    items = disagreements(trial, oracle)
    for item in items:
        compose(item, bench_dir("adjudication", root) / case / f"{item['key'].replace(':', '_')}.png")
    labels_path = bench_dir("labels", root) / f"{case}.json"
    if not labels_path.is_file():
        save_json(labels_path, labels_skeleton(case, items))
    return len(items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cases", nargs="*")
    args = parser.parse_args(argv)
    rows = []
    for oracle_path in sorted(bench_dir("oracle").glob("*.json")):
        case = oracle_path.stem
        if args.cases and case not in args.cases:
            continue
        if not (bench_dir("trial") / f"{case}.json").is_file():
            continue
        rows.append([case, adjudicate_case(case)])
    print(markdown_table(["case", "disagreements"], rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
