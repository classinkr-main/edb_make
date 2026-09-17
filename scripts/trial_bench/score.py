"""Score trial observations against ground truth, approved labels, or the oracle while pending.

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/score.py [--doc docs/web-trial-quality.md] [case ...]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.trial_bench.common import BENCH_ROOT, bench_dir, load_json, markdown_table  # noqa: E402
from trial_preview import needs_review  # noqa: E402

LOW_IOU = 0.8
DOC_START = "<!-- corpus-table -->"
DOC_END = "<!-- /corpus-table -->"
ACCEPTED_TRUTHS = {"trial", "oracle", "both", "neither"}
ACCEPTED_VERIFIED_BY = {"model", "human"}


def _area(box: dict[str, float]) -> float:
    return max(0.0, box["width"]) * max(0.0, box["height"])


def _intersection(a: dict[str, float], b: dict[str, float]) -> float:
    width = min(a["left"] + a["width"], b["left"] + b["width"]) - max(a["left"], b["left"])
    height = min(a["top"] + a["height"], b["top"] + b["height"]) - max(a["top"], b["top"])
    return max(0.0, width) * max(0.0, height)


def bbox_iou(a: dict[str, float], b: dict[str, float]) -> float:
    inter = _intersection(a, b)
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 else 0.0


def _envelope(boxes: list[dict[str, float]]) -> dict[str, float]:
    """Smallest box covering every input box."""
    left = min(box["left"] for box in boxes)
    top = min(box["top"] for box in boxes)
    right = max(box["left"] + max(0.0, box["width"]) for box in boxes)
    bottom = max(box["top"] + max(0.0, box["height"]) for box in boxes)
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


def _envelopes_by_page(regions: list[dict[str, Any]]) -> dict[int, dict[str, float]]:
    """One box per page: spec 5-1's "페이지별 합집합 박스".

    A problem can hold several regions on one page (two columns, a stem split
    around a figure), so they are reduced to their envelope before comparing.
    Keying straight off page_index would keep only the last region of each
    page and silently drop the rest.
    """
    by_page: dict[int, list[dict[str, float]]] = {}
    for region in regions:
        by_page.setdefault(region["page_index"], []).append(region["bbox"])
    return {page: _envelope(boxes) for page, boxes in by_page.items()}


def regions_iou(a_regions: list[dict[str, Any]], b_regions: list[dict[str, Any]]) -> float:
    """Sum of per-page intersections over per-page unions. A page present on one side only adds union."""
    by_a = _envelopes_by_page(a_regions)
    by_b = _envelopes_by_page(b_regions)
    inter = union = 0.0
    for page in set(by_a) | set(by_b):
        a, b = by_a.get(page), by_b.get(page)
        if a is not None and b is not None:
            overlap = _intersection(a, b)
            inter += overlap
            union += _area(a) + _area(b) - overlap
        else:
            union += _area(a if a is not None else b)
    return inter / union if union > 0 else 0.0


def _warn(message: str, sink: list[str] | None) -> None:
    """Report a recoverable problem: stderr for the operator, the sink for the caller."""
    print(message, file=sys.stderr)
    if sink is not None:
        sink.append(message)


def unscorable_observation_reason(obs: Any) -> str | None:
    """None when ``obs`` is a scorable observation; otherwise why not.

    Applied to *both* sides -- trial and oracle -- by score.py's score_all
    and adjudicate.py's adjudicate_case, so a truncated, failure-shaped, or
    missing-fields observation is recognized and reported the same way
    wherever it sits: one KeyError deep in either script's per-case loop --
    expected_from/disagreements/score_case all index ``["problems"]`` and
    score_all also reads ``["timing_ms"]``, on whichever side is broken --
    used to abort every *other* case's row along with the bad one.

    The two required fields are the observation contract
    (common.observation_from_result always writes both), so anything missing
    either one is a failure record or a foreign file rather than an
    observation. That is judged the same way regardless of which fields a
    particular consumer happens to index -- adjudicate.py never reads
    timing_ms -- so that score.py and adjudicate.py cannot disagree about
    which cases are scorable; they must not, because adjudicate.py produces
    the labels score.py then consumes.
    """
    if not isinstance(obs, dict):
        return "not a JSON object"
    if "problems" not in obs or "timing_ms" not in obs or obs.get("error"):
        return str(obs.get("error") or "no problems/timing_ms")
    return None


def _expected_from_ground_truth(
    case: str,
    ground_truth: dict[str, Any],
    oracle: dict[str, Any],
    *,
    warnings: list[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], str]:
    """(Expected key set, verified_by) built from an independently read ``ground_truth`` object, not from the oracle.

    ``ground_truth`` carries question numbers and passage ranges only -- no
    boxes -- so a key the oracle also reports keeps the oracle's own
    ``regions`` (spec: bbox IoU stays scored against the oracle boxes even on
    a truth-backed row, since a plain number list has none of its own). A key
    the oracle never detected gets an empty regions list rather than a
    fabricated box: score_case's IoU pass skips any matched key with no
    regions instead of scoring it as a false zero-overlap match.

    Raises ``ValueError`` -- naming ``case``, in the same style as
    ``expected_from``'s own ``items[]`` truth-value check -- for a shape too
    broken to score at all: ``ground_truth`` that is not an object, or a
    ``passage_ranges`` entry that is not a two-element ``[start, end]`` pair.
    A malformed shape must abort loudly and name the file, the same way a
    broken ``items[]`` entry already does, rather than raising a bare
    ``ValueError``/``AttributeError`` deep in a dict-comprehension that names
    neither.

    An empty result (no usable ``question_numbers`` or ``passage_ranges``) is
    returned as ``{}`` rather than raised: the caller falls back to the
    oracle path for that, because a half-filled stub (``{"source": ...}``
    with nothing counted yet) is a normal, recoverable, in-progress label,
    not a broken file -- and must never be scored as if it were a real
    answer with an empty expected set.

    The returned ``verified_by`` is ``ground_truth["verified_by"]`` --
    "model" (default, when the key is absent) or "human" -- the provenance
    class of whoever produced this ``ground_truth``. Every label on disk as
    of this writing omits the key, because every one of them was read by a
    Claude agent visually rendering the trimmed input (see the label's own
    ``source``/``note`` fields), not signed off by a person; defaulting to
    "model" keeps that true instead of silently implying a human reviewed a
    label that says nothing about who did. An unrecognized value is a broken
    label, raised the same way a malformed ``passage_ranges`` entry is.
    """
    if not isinstance(ground_truth, dict):
        raise ValueError(f"label case {case!r}: ground_truth is a {type(ground_truth).__name__}, not an object")
    verified_by = ground_truth.get("verified_by", "model")
    if verified_by not in ACCEPTED_VERIFIED_BY:
        raise ValueError(f"label case {case!r}: ground_truth.verified_by {verified_by!r} is not one of {sorted(ACCEPTED_VERIFIED_BY)}")
    # ``pages`` is the trimmed trial input's own page count
    # (~/edb-trial-bench/inputs/<case>.pdf, MAX_PAGES leading pages) -- the
    # same input both the trial and the oracle parsed -- never the full
    # source exam. Counting from the wrong PDF is the likely failure mode
    # this guards, so a mismatch is worth a warning even though the count
    # itself is otherwise unused.
    declared_pages, observed_pages = ground_truth.get("pages"), oracle.get("pages")
    if declared_pages is not None and observed_pages is not None and declared_pages != observed_pages:
        _warn(
            f"score.py: case {case!r}: ground_truth.pages ({declared_pages}) does not match the trial "
            f"input's own page count ({observed_pages}); question_numbers/passage_ranges must be counted "
            f"from ~/edb-trial-bench/inputs/{case}.pdf (the trimmed trial input), not the full source exam",
            warnings,
        )
    oracle_by_key = {problem["key"]: problem for problem in oracle["problems"]}
    expected: dict[str, dict[str, Any]] = {}
    for number in ground_truth.get("question_numbers") or []:
        key = f"q{number}"
        expected[key] = oracle_by_key.get(key, {"key": key, "regions": []})
    for entry in ground_truth.get("passage_ranges") or []:
        if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
            raise ValueError(f"label case {case!r}: ground_truth.passage_ranges entry {entry!r} is not a [start, end] pair")
        start, end = entry
        lo, hi = (start, end) if start <= end else (end, start)
        key = f"p{lo}-{hi}"
        expected[key] = oracle_by_key.get(key, {"key": key, "regions": []})
    return expected, verified_by


def expected_from(
    oracle: dict[str, Any],
    trial: dict[str, Any],
    labels: dict[str, Any] | None,
    *,
    warnings: list[str] | None = None,
    meta: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Ground truth per key: an independently read list, approved labels over the oracle, or the oracle itself while pending.

    A label carrying a truthy ``ground_truth`` object (question_numbers,
    passage_ranges -- see _expected_from_ground_truth) takes full precedence
    over the oracle, but only when ``status`` is "approved" or its alias
    "truth" (the value this function itself returns, and so the natural one
    for whoever fills the label to copy back): the expected key set comes
    from that independently read list alone, never from oracle["problems"],
    and the status becomes "truth" so a reader can tell a real-answer row
    from an oracle-as-provisional one at a glance. When ``meta`` is given,
    this also writes ``meta["verified_by"]`` (see _expected_from_ground_truth)
    so a caller such as score_all can surface who read this ground_truth --
    "model" (default) or "human" -- without a second pass over the label.
    Its per-item "items" truth overrides (below) do not apply on top of it --
    once a case has real ground truth, oracle-vs-trial adjudication is moot.

    A ``ground_truth`` present under any other status (typically "pending",
    what adjudicate.py writes and every existing label file on disk
    currently carries) is *not* silently ignored: this warns on stderr and
    in ``warnings``, naming the case and the status, then falls through to
    the oracle-scored path below -- so filling in ``ground_truth`` without
    also flipping ``status`` gets told, instead of unknowingly getting a
    "pending" row that is quietly compared against itself.
    Likewise a ``ground_truth`` with an approved/truth status but with no
    usable question_numbers or passage_ranges (a half-filled stub such as
    ``{"source": ..., "note": "WIP"}``) warns and falls through rather than
    claiming status "truth" for an empty expected set. Only a shape too
    broken to interpret at all -- not a dict, or a malformed passage range
    entry -- raises ValueError (see _expected_from_ground_truth), the same
    way a broken ``items[]`` entry does below.

    Without ground_truth, the oracle is corrected by approved labels (truth:
    trial | oracle | both | neither). "oracle" is an explicit no-op (the
    oracle's own entry already stands). "both" scores the key against the
    trial's own regions, crediting the trial's detection instead of leaving
    it as a false positive, for a disagreement judged acceptable either way.
    Raises ValueError only for a truth value outside the four above -- a
    broken label file.

    A key that neither side reports any more is a *stale* label, not a broken
    one: that is what an approved "trial only" verdict becomes as soon as a
    parser fix removes the detection, which is the before/after workflow this
    script exists for. Raising there would leave the whole run unscored, so
    the key is skipped with a warning instead.
    """
    case = labels.get("case") if labels else None
    ground_truth = labels.get("ground_truth") if labels else None
    status = labels.get("status") if labels else None
    if ground_truth:
        if status not in {"approved", "truth"}:
            _warn(
                f"score.py: case {case!r}: ground_truth present but status is {status!r}; "
                "ignoring it and scoring against the oracle",
                warnings,
            )
        else:
            truth_expected, verified_by = _expected_from_ground_truth(case, ground_truth, oracle, warnings=warnings)
            if truth_expected:
                if meta is not None:
                    meta["verified_by"] = verified_by
                return truth_expected, "truth"
            _warn(
                f"score.py: case {case!r}: ground_truth has no question_numbers or passage_ranges; "
                "ignoring it and scoring against the oracle",
                warnings,
            )
    expected = {problem["key"]: problem for problem in oracle["problems"]}
    if not labels or labels.get("status") != "approved":
        return expected, "pending"
    trial_by_key = {problem["key"]: problem for problem in trial["problems"]}
    oracle_by_key = {problem["key"]: problem for problem in oracle["problems"]}
    for item in labels.get("items", []):
        key, truth = item.get("key"), item.get("truth")
        if truth not in ACCEPTED_TRUTHS:
            raise ValueError(f"label case {case!r}: key {key!r} has unknown truth {truth!r}; expected one of {sorted(ACCEPTED_TRUTHS)}")
        if key not in oracle_by_key and key not in trial_by_key:
            _warn(f"score.py: case {case!r}: label key {key!r} is no longer present in the oracle or the trial; ignoring", warnings)
            continue
        if truth == "neither":
            expected.pop(key, None)
        elif truth in {"trial", "both"} and key in trial_by_key:
            expected[key] = trial_by_key[key]
        # truth == "oracle", or "both" with no matching trial detection:
        # keep the oracle's entry unchanged.
    return expected, "approved"


def _ratio(numerator: int, denominator: int) -> float | None:
    """None (rendered as an empty cell) when the metric is undefined -- never a fake perfect score."""
    return numerator / denominator if denominator else None


def ai_evidence_cell(page_repair: Any) -> tuple[str, bool | None]:
    """(cell text, has_evidence) from an oracle observation's ``oracle.page_repair``
    (scripts/trial_bench/oracle.py's ``summarize_page_repair`` output).

    ``has_evidence`` is ``None`` when there is nothing to judge -- the field
    is missing or malformed, e.g. an oracle observation from before this
    instrumentation existed -- which is distinct from ``False`` (measured,
    and found no real change). Only ``False`` triggers the report's
    zero-evidence footnote; a scored report must not silently read a `1.00`
    row as AI-confirmed when the oracle run behind it never proved AI page
    repair changed anything.
    """
    if not isinstance(page_repair, dict) or "pages_total" not in page_repair:
        return "?", None
    pages_total = page_repair.get("pages_total") or 0
    if pages_total == 0:
        return "no records", False
    pages_changed = page_repair.get("pages_changed") or 0
    has_evidence = pages_changed > 0
    cell = f"{pages_changed}/{pages_total}" if has_evidence else f"{pages_changed}/{pages_total} (NO EVIDENCE)"
    return cell, has_evidence


def score_case(trial: dict[str, Any], expected: dict[str, dict[str, Any]]) -> dict[str, Any]:
    trial_by_key = {problem["key"]: problem for problem in trial["problems"]}
    questions_t = {key for key in trial_by_key if key.startswith("q")}
    questions_e = {key for key in expected if key.startswith("q")}
    passages_t = {key for key in trial_by_key if key.startswith("p")}
    passages_e = {key for key in expected if key.startswith("p")}
    # Neither numbered nor a passage range -- common.problem_key's "t:<title>"
    # fallback for an unnumbered, non-passage unit. Never a legitimate
    # detection, so it always counts as a false positive rather than being
    # silently dropped from scoring.
    others_t = set(trial_by_key) - questions_t - passages_t
    matched = (questions_t & questions_e) | (passages_t & passages_e)
    # A truth-backed expected entry the oracle never detected (see
    # _expected_from_ground_truth) carries an empty regions list -- there is
    # no box to compare the trial's against, so that key is left out of the
    # IoU pass entirely rather than scored as a fabricated zero-overlap miss.
    # Every non-truth-backed expected entry (built from a real oracle or
    # trial problem) always has at least one region, so this changes nothing
    # for the pre-existing oracle/approved-labels path.
    ious = {key: regions_iou(trial_by_key[key]["regions"], expected[key]["regions"]) for key in matched if expected[key].get("regions")}
    return {
        "case": trial["case"],
        "question_recall": _ratio(len(questions_t & questions_e), len(questions_e)),
        "question_precision": _ratio(len(questions_t & questions_e), len(questions_t)),
        "passage_recall": _ratio(len(passages_t & passages_e), len(passages_e)),
        "passage_precision": _ratio(len(passages_t & passages_e), len(passages_t)),
        "mean_iou": sum(ious.values()) / len(ious) if ious else None,
        "low_iou": sum(1 for value in ious.values() if value < LOW_IOU),
        "review_rate": sum(1 for problem in trial["problems"] if needs_review(problem["risk_flags"])) / max(1, len(trial_by_key)),
        "missing": sorted(questions_e - questions_t) + sorted(passages_e - passages_t),
        "extra": sorted(questions_t - questions_e) + sorted(passages_t - passages_e) + sorted(others_t),
        "trial_ms": trial["timing_ms"].get("total"),
    }


def _fmt(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else ""


def _keys_cell(keys: list[str]) -> str:
    """Key list for a rendered table cell, with unnumbered keys made opaque.

    An unnumbered unit's key is common.problem_key's "t:<display_title>"
    fallback, and display_title is up to 120 characters of raw exam text
    (segment.py). That must not reach the committed doc table, so each such
    key becomes "t:#<n>" -- the count and order survive, the exam text does
    not. The full keys stay in the out-of-repo observation JSON.
    """
    rendered = []
    unnumbered = 0
    for key in keys:
        if key.startswith("t:"):
            unnumbered += 1
            rendered.append(f"t:#{unnumbered}")
        else:
            rendered.append(key)
    return " ".join(rendered)


def render_report(rows: list[dict[str, Any]], excluded: list[dict[str, str]] | None = None) -> str:
    headers = ["case", "status", "q_recall", "q_prec", "p_recall", "p_prec", "mean_iou", "low_iou", "review", "missing", "extra", "trial_ms", "oracle_ms", "ai_evidence", "verified_by"]
    table_rows = [
        [
            row["case"], row["status"], _fmt(row["question_recall"]), _fmt(row["question_precision"]),
            _fmt(row["passage_recall"]), _fmt(row["passage_precision"]), _fmt(row["mean_iou"]), row["low_iou"],
            _fmt(row["review_rate"]), _keys_cell(row["missing"]), _keys_cell(row["extra"]), row["trial_ms"], row["oracle_ms"],
            # A row built by hand (as the tests here do) rather than by
            # score_all carries no ai_evidence field at all -- "?" says the
            # report cannot vouch for this row either way, distinct from
            # score_all's own "no records"/"NO EVIDENCE" cells.
            row.get("ai_evidence", "?"),
            # verified_by is blank for a "pending"/"approved" row -- there is
            # no ground_truth in play at all, so there is no one to name.
            # Only a "truth" row carries it, and even then it says "model" by
            # default (see _expected_from_ground_truth): a case whose answer
            # key was read by a Claude agent must not render identically to
            # one a person actually signed off on.
            row.get("verified_by", ""),
        ]
        for row in rows
    ]
    if rows:
        count = len(rows)

        def mean(field: str) -> float | None:
            values = [row[field] for row in rows if row[field] is not None]
            return sum(values) / len(values) if values else None

        # "truth" (an independently read ground_truth object, see
        # expected_from) is the only status backed by an independent answer;
        # "approved" and "pending" are both still scored against the oracle
        # -- provisional, in this task's own framing, however carefully
        # adjudicated -- so the aggregate counts them on the same side of the
        # truth-backed split.
        truth_count = sum(1 for row in rows if row["status"] == "truth")
        approved_count = sum(1 for row in rows if row["status"] == "approved")
        provisional_count = count - truth_count
        table_rows.append(
            [
                "합계", f"{truth_count}/{count} truth-backed, {provisional_count}/{count} provisional ({approved_count} approved)",
                _fmt(mean("question_recall")), _fmt(mean("question_precision")), _fmt(mean("passage_recall")),
                _fmt(mean("passage_precision")), _fmt(mean("mean_iou")), sum(row["low_iou"] for row in rows),
                _fmt(mean("review_rate")), sum(len(row["missing"]) for row in rows), sum(len(row["extra"]) for row in rows), "", "", "", "",
            ]
        )
    table = markdown_table(headers, table_rows)
    # Same "next to the numbers, regenerated every run" reasoning as the two
    # footnotes below: a "truth" row's q_recall/q_prec/p_recall/p_prec come
    # from an independently read question/passage list with no boxes of its
    # own, so mean_iou/low_iou for it are still scored against the oracle's
    # boxes (score_case skips the IoU pass only for a matched key the oracle
    # never detected) -- that must be said here, not left to silently mix an
    # independently read count with an oracle-sourced box.
    truth_backed_cases = [row["case"] for row in rows if row["status"] == "truth"]
    if truth_backed_cases:
        table += (
            "\n\n> **"
            + ", ".join(f"`{case}`" for case in truth_backed_cases)
            + f": {len(truth_backed_cases)} truth-backed case(s) above.** `q_recall`/`q_prec`/`p_recall`/`p_prec` "
            "come from an independently read question/passage list (the label's `ground_truth`), but `mean_iou`/`low_iou` "
            "are still scored against the oracle's own boxes -- see `ground_truth`, `verified_by`, and the "
            "`docs/web-trial-quality.md` 라벨 형식 section."
        )
    # The caveat lives next to the numbers it qualifies and is regenerated
    # every run, so it can never go stale the way a hand-written banner in
    # docs/web-trial-quality.md did (docs/web-trial-quality.md's old "AI 근거
    # 없음" paragraph stayed put after update_doc rewrote only the table).
    zero_evidence_cases = [row["case"] for row in rows if row.get("ai_evidence_ok") is False]
    if zero_evidence_cases:
        table += (
            "\n\n> **AI page repair produced no evidence of a real change for "
            f"{len(zero_evidence_cases)} of {len(rows)} case(s): "
            + ", ".join(f"`{case}`" for case in zero_evidence_cases)
            + ".** On those cases the forced-AI oracle's block types, problem grouping, "
            "titles, crop boxes and review flags all came out identical to what the local "
            "baseline produced on its own, so those rows' scores show agreement with the "
            "trial's own local baseline, not confirmation by AI-grade recognition -- see "
            "`ai_evidence` and rerun scripts/trial_bench/oracle.py to refresh."
        )
    # Same "next to the numbers, regenerated every run" reasoning as the
    # zero-evidence caveat above, for a case with no row at all: naming it
    # here (case and reason) is what stops it vanishing the way both English
    # bench cases once did, leaving only hand-written prose that the next
    # --doc run couldn't refresh.
    if excluded:
        table += (
            f"\n\n> **{len(excluded)} case(s) excluded from this report because an observation "
            "could not be scored: "
            + ", ".join(f"`{item['case']}` ({item['reason']})" for item in excluded)
            + ".** Rerun scripts/trial_bench/observe.py or scripts/trial_bench/oracle.py for these "
            "cases -- whichever side the reason names -- then rerun score.py to include them."
        )
    return table


def score_all(
    cases: list[str],
    root: Path = BENCH_ROOT,
    warnings: list[str] | None = None,
    excluded: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Score every trial observation under ``root`` against its oracle.

    A case whose trial *or* oracle observation is missing or unscorable --
    never run, or a run that failed or was interrupted mid-write -- must
    cost only its own row, never the whole loop: expected_from and
    score_case index ``["problems"]`` and the row build indexes
    ``["timing_ms"]``, on both sides, so letting any of those raise here
    would abort scoring for every other case too -- no report.md, no doc
    table. Neither side is the privileged one: observe.py can die mid-run
    exactly like oracle.py can. Such a case is still *reported*, not
    silently dropped: warned about on stderr, and when ``excluded`` is
    given, appended to it as ``{"case": ..., "reason": ...}`` -- the reason
    naming which side was bad -- so a caller (render_report's footnote,
    main()'s per-case message) can say which case is missing and why,
    instead of leaving that to hand-written prose the next run can't
    refresh.

    "Interrupted mid-write" is not just a hypothetical mentioned above: a
    run killed while common.save_json was writing leaves a half JSON file on
    disk, and load_json's json.loads raises JSONDecodeError on it *before*
    unscorable_observation_reason ever sees a parsed object -- so that guard
    alone cannot catch it. Each load is wrapped here for the same reason
    unscorable_observation_reason exists: one unreadable file must cost only
    its own row.
    """
    rows = []
    for trial_path in sorted(bench_dir("trial", root).glob("*.json")):
        case = trial_path.stem
        if cases and case not in cases:
            continue
        oracle_path = bench_dir("oracle", root) / f"{case}.json"
        if not oracle_path.is_file():
            reason = "no oracle observation found"
            _warn(f"score.py: case {case!r}: {reason} ({oracle_path}); skipping this case", warnings)
            if excluded is not None:
                excluded.append({"case": case, "reason": reason})
            continue
        labels_path = bench_dir("labels", root) / f"{case}.json"
        try:
            trial = load_json(trial_path)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            reason = f"unreadable trial observation ({exc})"
            _warn(f"score.py: case {case!r}: {trial_path}: {reason}; skipping this case", warnings)
            if excluded is not None:
                excluded.append({"case": case, "reason": reason})
            continue
        trial_reason = unscorable_observation_reason(trial)
        if trial_reason is not None:
            reason = f"unscorable trial observation ({trial_reason})"
            _warn(f"score.py: case {case!r}: {trial_path} is not a scorable trial observation ({trial_reason}); skipping this case", warnings)
            if excluded is not None:
                excluded.append({"case": case, "reason": reason})
            continue
        try:
            oracle = load_json(oracle_path)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            reason = f"unreadable oracle observation ({exc})"
            _warn(f"score.py: case {case!r}: {oracle_path} is not a scorable oracle observation ({reason}); skipping this case", warnings)
            if excluded is not None:
                excluded.append({"case": case, "reason": reason})
            continue
        reason = unscorable_observation_reason(oracle)
        if reason is not None:
            _warn(f"score.py: case {case!r}: {oracle_path} is not a scorable oracle observation ({reason}); skipping this case", warnings)
            if excluded is not None:
                excluded.append({"case": case, "reason": reason})
            continue
        labels = load_json(labels_path) if labels_path.is_file() else None
        meta: dict[str, Any] = {}
        expected, status = expected_from(oracle, trial, labels, warnings=warnings, meta=meta)
        row = score_case(trial, expected)
        oracle_page_repair = (oracle.get("oracle") or {}).get("page_repair") if isinstance(oracle.get("oracle"), dict) else None
        evidence_cell, evidence_ok = ai_evidence_cell(oracle_page_repair)
        # meta["verified_by"] is only set when expected_from actually used
        # ground_truth (status "truth"); a "pending"/"approved" row has no
        # ground_truth in play at all, so its cell must be blank, never a
        # fabricated "model".
        row.update(
            status=status, oracle_ms=oracle["timing_ms"].get("total"), ai_evidence=evidence_cell,
            ai_evidence_ok=evidence_ok, verified_by=meta.get("verified_by", ""),
        )
        rows.append(row)
    return rows


def update_doc(doc_path: Path, table: str) -> None:
    stamp = f"측정일 {dt.date.today().isoformat()} · 라벨 없는 케이스는 오라클을 임시 정답으로 채점(pending)"
    block = f"{DOC_START}\n{stamp}\n\n{table}\n{DOC_END}"
    text = doc_path.read_text(encoding="utf-8") if doc_path.is_file() else "# 웹 체험판 정확성 코퍼스 결과\n\n"
    if DOC_START in text and DOC_END in text:
        head, rest = text.split(DOC_START, 1)
        _, tail = rest.split(DOC_END, 1)
        text = head + block + tail
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    doc_path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--doc", type=Path, default=None, help="markdown file whose corpus-table block is replaced")
    parser.add_argument("cases", nargs="*")
    args = parser.parse_args(argv)
    excluded: list[dict[str, str]] = []
    rows = score_all(args.cases, excluded=excluded)
    matched = {row["case"] for row in rows}
    excluded_reasons = {item["case"]: item["reason"] for item in excluded}
    missing = [name for name in args.cases if name not in matched]
    for name in missing:
        # A named case can be missing from `rows` for two different reasons
        # -- computing this message from `matched` alone (as before) reported
        # "no trial observation found" even for a case that has one, when its
        # oracle observation was what score_all actually excluded it for.
        if name in excluded_reasons:
            print(f"score.py: case {name!r} was excluded from the report: {excluded_reasons[name]}", file=sys.stderr)
        else:
            print(f"score.py: no trial observation found for case {name!r}", file=sys.stderr)
    if not rows:
        print("score.py: nothing matched; leaving report.md" + (f" and {args.doc}" if args.doc else "") + " unchanged", file=sys.stderr)
        return 1
    table = render_report(rows, excluded)
    print(table)
    (BENCH_ROOT / "report.md").write_text(table + "\n", encoding="utf-8")
    doc_refused = False
    if args.doc:
        if args.cases:
            print(
                f"score.py: refusing to overwrite {args.doc}'s corpus table with a case-filtered subset "
                f"({', '.join(sorted(matched))}); rerun without case names to refresh the whole table",
                file=sys.stderr,
            )
            doc_refused = True
        else:
            update_doc(args.doc, table)
    return 1 if (missing or doc_refused) else 0


if __name__ == "__main__":
    raise SystemExit(main())
