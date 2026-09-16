"""Side-by-side crops for every trial/oracle disagreement, plus a labels skeleton.

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/adjudicate.py [case ...]

Fable reviews adjudication/<case>/<key>.png and fills labels/<case>.json:
truth = "trial" | "oracle" | "both" | "neither", then status = "approved".
Existing label files are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.trial_bench.common import BENCH_ROOT, bench_dir, crop_stem, load_json, markdown_table, save_json  # noqa: E402
from scripts.trial_bench.score import LOW_IOU, _warn, regions_iou, unscorable_observation_reason  # noqa: E402

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


def adjudicate_case(
    case: str,
    root: Path = BENCH_ROOT,
    *,
    warnings: list[str] | None = None,
    excluded: list[dict[str, str]] | None = None,
) -> int | None:
    """Render every trial/oracle disagreement for ``case`` and (re)write its labels skeleton.

    Returns ``None`` -- instead of raising -- when the trial or oracle
    observation on disk cannot be read or scored. Both sides get both
    guards, because disagreements() indexes trial["problems"] and
    oracle["problems"] alike and observe.py can die mid-run exactly like
    oracle.py can. A failure-shaped or missing-fields record (score.py's own
    guard, shared via unscorable_observation_reason) makes disagreements()
    raise KeyError with nothing to stop it; a genuinely half-written file
    (an observation run killed mid common.save_json) makes the load itself
    raise JSONDecodeError, before unscorable_observation_reason ever sees a
    parsed object. Any of them used to abort main()'s whole ``for
    oracle_path in ...`` loop and cost every other case its row too. The
    case is still reported, not silently dropped: warned about on stderr,
    and when ``excluded`` is given, appended to it as ``{"case": ...,
    "reason": ...}`` -- the reason naming which side was bad -- for main()'s
    own footnote.
    """
    try:
        trial = load_json(bench_dir("trial", root) / f"{case}.json")
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        reason = f"unreadable trial observation ({exc})"
        _warn(f"adjudicate.py: case {case!r}: {reason}; skipping this case", warnings)
        if excluded is not None:
            excluded.append({"case": case, "reason": reason})
        return None
    trial_reason = unscorable_observation_reason(trial)
    if trial_reason is not None:
        reason = f"unscorable trial observation ({trial_reason})"
        _warn(f"adjudicate.py: case {case!r}: trial observation is not scorable ({trial_reason}); skipping this case", warnings)
        if excluded is not None:
            excluded.append({"case": case, "reason": reason})
        return None
    try:
        oracle = load_json(bench_dir("oracle", root) / f"{case}.json")
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        reason = f"unreadable oracle observation ({exc})"
        _warn(f"adjudicate.py: case {case!r}: {reason}; skipping this case", warnings)
        if excluded is not None:
            excluded.append({"case": case, "reason": reason})
        return None
    reason = unscorable_observation_reason(oracle)
    if reason is not None:
        _warn(f"adjudicate.py: case {case!r}: oracle observation is not scorable ({reason}); skipping this case", warnings)
        if excluded is not None:
            excluded.append({"case": case, "reason": reason})
        return None
    items = disagreements(trial, oracle)
    stems: set[str] = set()
    for item in items:
        compose(item, bench_dir("adjudication", root) / case / f"{crop_stem(item['key'], stems)}.png")
    labels_path = bench_dir("labels", root) / f"{case}.json"
    if not labels_path.is_file():
        save_json(labels_path, labels_skeleton(case, items))
    else:
        # Never overwrite an existing labels file (see module docstring) --
        # but a pending skeleton that predates a re-observed case can go
        # stale silently otherwise, so at least say so.
        existing = load_json(labels_path)
        if existing.get("status") != "approved":
            new_keys = {item["key"] for item in items}
            existing_keys = {entry["key"] for entry in existing.get("items", [])}
            missing = sorted(new_keys - existing_keys)
            if missing:
                print(
                    f"adjudicate.py: case {case!r}: labels file is {existing.get('status')!r} and misses {missing}; delete it to regenerate",
                    file=sys.stderr,
                )
    return len(items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cases", nargs="*")
    args = parser.parse_args(argv)
    rows = []
    excluded: list[dict[str, str]] = []
    for oracle_path in sorted(bench_dir("oracle").glob("*.json")):
        case = oracle_path.stem
        if args.cases and case not in args.cases:
            continue
        if not (bench_dir("trial") / f"{case}.json").is_file():
            continue
        count = adjudicate_case(case, excluded=excluded)
        if count is None:
            continue
        rows.append([case, count])
    table = markdown_table(["case", "disagreements"], rows)
    # Same reasoning as score.py's render_report footnote: an excluded case
    # must appear inside the generated output, named with why, instead of
    # only in a stderr warning nothing else preserves.
    if excluded:
        table += (
            f"\n\n> **{len(excluded)} case(s) excluded from this report because an observation "
            "could not be scored: "
            + ", ".join(f"`{item['case']}` ({item['reason']})" for item in excluded)
            + ".** Rerun scripts/trial_bench/observe.py or scripts/trial_bench/oracle.py for these "
            "cases -- whichever side the reason names -- then rerun adjudicate.py to include them."
        )
    print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
