import contextlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image

import page_repair
from page_repair import build_ai_fallback_config, repair_page_model
from preprocess import PreparedPage
from problem_parser import ParsedPage, ParsedProblem, ParsedRegion, ParseResult
from scripts.trial_bench import common, memory
from scripts.trial_bench import adjudicate
from scripts.trial_bench.adjudicate import adjudicate_case, compose, disagreements, labels_skeleton
from scripts.trial_bench.complexity import write_synthetic
from scripts.trial_bench.load import summarize_wave
from scripts.trial_bench.make_inputs import make_input
from scripts.trial_bench import oracle
from scripts.trial_bench.oracle import force_config, oracle_case, summarize_page_repair
from scripts.trial_bench.probe import summarize_file
from scripts.trial_bench import score
from scripts.trial_bench.score import bbox_iou, expected_from, regions_iou, render_report, score_all, score_case
from structured_schema import BlockType, Box, ContentBlock, PageModel, Subject
from trial_input import DEFAULT_MAX_PAGES


def _result() -> ParseResult:
    page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
    problems = [
        ParsedProblem(
            problem_id="q1", number=1, title="1번",
            regions=[ParsedRegion(page_id="p1", bbox=Box(left=10.0, top=10.0, width=200.0, height=80.0))],
            risk_flags=[], image=Image.new("RGB", (200, 80), "white"),
        ),
        ParsedProblem(
            problem_id="passage", number=None, title="지문 1~3",
            regions=[ParsedRegion(page_id="p1", bbox=Box(left=10.0, top=100.0, width=200.0, height=300.0))],
            risk_flags=["passage_cross_page_merge_check"], image=Image.new("RGB", (200, 300), "white"),
        ),
    ]
    return ParseResult(pages=[page], problems=problems, source_page_count=16, parser_version="dev", timing_ms={"total": 7})


def _write_pdf(path: Path, page_count: int) -> Path:
    doc = fitz.open()
    for index in range(page_count):
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), f"{index + 1}. problem stem with enough words to count as text on the page", fontsize=12)
        page.insert_text((40, 300), "① a   ② b   ③ c   ④ d   ⑤ e", fontsize=12)
    doc.save(path)
    doc.close()
    return path


class TestCommon(unittest.TestCase):
    def test_passage_range_from_title(self):
        self.assertEqual([4, 9], common.passage_range_from_title("지문 4~9"))
        self.assertEqual([10, 13], common.passage_range_from_title("지문 10 ~ 13"))
        self.assertIsNone(common.passage_range_from_title("3번"))
        self.assertIsNone(common.passage_range_from_title(None))

    def test_passage_range_requires_the_passage_marker(self):
        # A bare "<digits><sep><digits>" run in free exam text is not a
        # passage range: the parser hands us this title verbatim whenever it
        # cannot number the unit, and it routinely contains ranges like this.
        self.assertIsNone(common.passage_range_from_title("표는 1-3족 원소의 성질을 나타낸 것이다"))
        self.assertIsNone(common.passage_range_from_title("2020~2023년 사이의 인구 변화를 나타낸 그래프이다"))

    def test_problem_key(self):
        self.assertEqual("q12", common.problem_key(12, "12번"))
        self.assertEqual("p1-3", common.problem_key(None, "지문 1~3"))
        self.assertEqual("t:그림", common.problem_key(None, "그림"))

    def test_case_id_sanitizes_names(self):
        self.assertEqual("2026학년도_수능_국어", common.case_id(Path("/x/2026학년도 수능 국어.pdf")))
        self.assertEqual("01_물리학Ⅰ_문제지", common.case_id(Path("01 물리학Ⅰ_문제지.pdf")))

    def test_observation_has_numbers_boxes_and_no_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            observation = common.observation_from_result("case", _result(), crops_dir=Path(temp_dir) / "crops")
            self.assertTrue((Path(temp_dir) / "crops" / "q1.png").is_file())
        self.assertEqual(["q1", "p1-3"], [problem["key"] for problem in observation["problems"]])
        self.assertEqual([[1, 3]], observation["passage_ranges"])
        self.assertEqual(0, observation["problems"][0]["regions"][0]["page_index"])
        self.assertEqual({"left": 10.0, "top": 10.0, "width": 200.0, "height": 80.0}, observation["problems"][0]["regions"][0]["bbox"])
        self.assertEqual([[600, 800]], observation["page_sizes"])

    def test_title_drops_free_text_but_keeps_labels(self):
        # segment.py sets display_title = text[:120] whenever a unit cannot be
        # numbered or grouped into a passage; that raw exam text must never
        # reach the observation (spec 5-1: "텍스트는 넣지 않는다"), unlike a
        # genuine number or passage marker, which is just a short label.
        sentence = "표는 1-3족 원소의 성질을 비교하여 나타낸 것이다 다음 물음에 답하시오"
        page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
        problems = [
            ParsedProblem(
                problem_id="q9", number=9, title=sentence,
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=0.0, width=10.0, height=10.0))],
                risk_flags=[], image=Image.new("RGB", (10, 10), "white"),
            ),
            ParsedProblem(
                problem_id="c1", number=None, title="이어지는 자료",
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=20.0, width=10.0, height=10.0))],
                risk_flags=[], image=Image.new("RGB", (10, 10), "white"),
            ),
        ]
        result = ParseResult(pages=[page], problems=problems, source_page_count=1, parser_version="dev", timing_ms={"total": 1})
        observation = common.observation_from_result("case", result)
        self.assertIsNone(observation["problems"][0]["title"])
        self.assertIsNone(observation["problems"][1]["title"])
        self.assertNotIn(sentence, json.dumps(observation, ensure_ascii=False))
        # The numbered path stays clean: a real marker is not exam text.
        self.assertEqual("1번", common.observation_from_result(
            "case",
            ParseResult(
                pages=[page],
                problems=[ParsedProblem(problem_id="q1", number=1, title="1번", regions=[], risk_flags=[], image=Image.new("RGB", (1, 1)))],
                source_page_count=1, parser_version="dev", timing_ms={"total": 1},
            ),
        )["problems"][0]["title"])

    def test_duplicate_fallback_titles_get_unique_keys_and_crops(self):
        # build_problem_board_edb.py gives every marker-continuation unit the
        # same constant title ("이어지는 자료"), so the fallback key collides
        # for any exam with more than one such unit.
        page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
        problems = [
            ParsedProblem(
                problem_id="c1", number=None, title="이어지는 자료",
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=0.0, width=10.0, height=10.0))],
                risk_flags=[], image=Image.new("RGB", (10, 10), "white"),
            ),
            ParsedProblem(
                problem_id="c2", number=None, title="이어지는 자료",
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=20.0, width=10.0, height=10.0))],
                risk_flags=[], image=Image.new("RGB", (10, 10), "white"),
            ),
        ]
        result = ParseResult(pages=[page], problems=problems, source_page_count=1, parser_version="dev", timing_ms={"total": 1})
        with tempfile.TemporaryDirectory() as temp_dir:
            crops_dir = Path(temp_dir) / "crops"
            observation = common.observation_from_result("case", result, crops_dir=crops_dir)
            crop_files = sorted(p.name for p in crops_dir.iterdir())
        keys = [problem["key"] for problem in observation["problems"]]
        self.assertEqual(2, len(set(keys)))
        self.assertEqual(2, len(crop_files))

    def test_duplicate_numbers_get_unique_keys_and_crops(self):
        # A parser mistake that numbers two units the same must not overwrite
        # the first crop with the second, nor collapse both JSON entries onto
        # one key (Task 10's expected/trial maps are keyed by "key").
        page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
        problems = [
            ParsedProblem(
                problem_id="c1", number=3, title="3번",
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=0.0, width=10.0, height=10.0))],
                risk_flags=[], image=Image.new("RGB", (10, 10), "white"),
            ),
            ParsedProblem(
                problem_id="c2", number=3, title="3번",
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=20.0, width=20.0, height=20.0))],
                risk_flags=[], image=Image.new("RGB", (20, 20), "white"),
            ),
        ]
        result = ParseResult(pages=[page], problems=problems, source_page_count=1, parser_version="dev", timing_ms={"total": 1})
        with tempfile.TemporaryDirectory() as temp_dir:
            crops_dir = Path(temp_dir) / "crops"
            observation = common.observation_from_result("case", result, crops_dir=crops_dir)
            crop_files = sorted(crops_dir.iterdir())
            self.assertEqual(2, len(crop_files))
            with Image.open(crop_files[0]) as first_crop:
                first_size = first_crop.size
            with Image.open(crop_files[1]) as second_crop:
                second_size = second_crop.size
        self.assertEqual(["q3", "q3#2"], [problem["key"] for problem in observation["problems"]])
        self.assertEqual((10, 10), first_size)
        self.assertEqual((20, 20), second_size)

    def test_crop_filename_sanitizes_unsafe_characters(self):
        # problem.image.save() treats "/" as a directory separator; a raw
        # fallback title routinely contains one (m/s, g/cm3, A/B, ...).
        page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
        problem = ParsedProblem(
            problem_id="c1", number=None, title="Which of A/B is correct?",
            regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=0.0, width=10.0, height=10.0))],
            risk_flags=[], image=Image.new("RGB", (10, 10), "white"),
        )
        result = ParseResult(pages=[page], problems=[problem], source_page_count=1, parser_version="dev", timing_ms={"total": 1})
        with tempfile.TemporaryDirectory() as temp_dir:
            crops_dir = Path(temp_dir) / "crops"
            common.observation_from_result("case", result, crops_dir=crops_dir)
            crop_files = list(crops_dir.iterdir())
        self.assertEqual(1, len(crop_files))
        self.assertNotIn("/", crop_files[0].name)

    def test_crop_filename_truncates_long_titles(self):
        # A raw Korean title over ~85 characters is over 255 bytes and would
        # otherwise raise OSError ENAMETOOLONG when saving the crop.
        long_title = "가" * 200
        page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
        problem = ParsedProblem(
            problem_id="c1", number=None, title=long_title,
            regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=0.0, width=10.0, height=10.0))],
            risk_flags=[], image=Image.new("RGB", (10, 10), "white"),
        )
        result = ParseResult(pages=[page], problems=[problem], source_page_count=1, parser_version="dev", timing_ms={"total": 1})
        with tempfile.TemporaryDirectory() as temp_dir:
            crops_dir = Path(temp_dir) / "crops"
            common.observation_from_result("case", result, crops_dir=crops_dir)
            crop_files = list(crops_dir.iterdir())
        self.assertEqual(1, len(crop_files))
        self.assertLessEqual(len(crop_files[0].stem), 80)

    def test_long_duplicate_titles_get_distinct_crops(self):
        # The "#n" uniquifier is appended to the key, but the crop stem was
        # truncated *after* that, so a title at segment.py's normal fallback
        # length (display_title = text[:120]) sliced the suffix back off and
        # every duplicate silently collapsed onto one file.
        long_title = "가" * 90
        page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
        problems = [
            ParsedProblem(
                problem_id=f"c{index}", number=None, title=long_title,
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=float(index * 40), width=float(size), height=float(size)))],
                risk_flags=[], image=Image.new("RGB", (size, size), "white"),
            )
            for index, size in enumerate((10, 22, 33))
        ]
        result = ParseResult(pages=[page], problems=problems, source_page_count=1, parser_version="dev", timing_ms={"total": 1})
        with tempfile.TemporaryDirectory() as temp_dir:
            crops_dir = Path(temp_dir) / "crops"
            observation = common.observation_from_result("case", result, crops_dir=crops_dir)
            crop_files = sorted(crops_dir.iterdir())
            self.assertEqual(3, len(crop_files))
            sizes = set()
            for path in crop_files:
                with Image.open(path) as crop:
                    sizes.add(crop.size)
        self.assertEqual({(10, 10), (22, 22), (33, 33)}, sizes)
        self.assertEqual(3, len({problem["crop"] for problem in observation["problems"]}))
        self.assertTrue(all(len(Path(problem["crop"]).stem) <= 80 for problem in observation["problems"]))

    def test_long_titles_sharing_a_prefix_get_distinct_crops(self):
        # Two *different* fallback titles that agree on their first 80
        # characters have different keys, so their crops must not share a file.
        prefix = "가" * 90
        page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
        problems = [
            ParsedProblem(
                problem_id=f"c{index}", number=None, title=f"{prefix} {suffix}",
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=float(index * 40), width=float(size), height=float(size)))],
                risk_flags=[], image=Image.new("RGB", (size, size), "white"),
            )
            for index, (suffix, size) in enumerate((("갑", 10), ("을", 22)))
        ]
        result = ParseResult(pages=[page], problems=problems, source_page_count=1, parser_version="dev", timing_ms={"total": 1})
        with tempfile.TemporaryDirectory() as temp_dir:
            crops_dir = Path(temp_dir) / "crops"
            observation = common.observation_from_result("case", result, crops_dir=crops_dir)
            crop_files = list(crops_dir.iterdir())
        self.assertEqual(2, len({problem["key"] for problem in observation["problems"]}))
        self.assertEqual(2, len(crop_files))

    def test_percentile_is_nearest_rank(self):
        self.assertEqual(10, common.percentile(range(1, 11), 95))
        self.assertEqual(5, common.percentile(range(1, 11), 50))
        self.assertTrue(math.isnan(common.percentile([], 50)))

    def test_markdown_table(self):
        table = common.markdown_table(["a", "b"], [[1, None]])
        self.assertEqual("| a | b |\n|---|---|\n| 1 |  |", table)

    def test_markdown_table_escapes_pipes_and_collapses_newlines(self):
        # A raw "|" or newline inside a cell would end the column (or the row)
        # early and shift every later cell, which corrupts the committed
        # result tables these helpers render.
        table = common.markdown_table(["a", "b"], [["x|y", "one\r\ntwo"]])
        self.assertEqual("| a | b |\n|---|---|\n| x\\|y | one two |", table)
        body = table.splitlines()[2]
        self.assertEqual(table.splitlines()[0].count("|"), body.replace("\\|", "").count("|"))


class TestParseInScratch(unittest.TestCase):
    def test_copies_into_a_fresh_temp_dir_and_forwards_kwargs(self):
        captured: dict = {}

        def _spy(src, *, work_dir, max_pages, **kw):
            # Read the copy's bytes now: parse_in_scratch's temp dir is gone
            # by the time this call returns to the test.
            captured.update(source=src, work_dir=work_dir, max_pages=max_pages, source_bytes=src.read_bytes(), extra=kw)
            return src

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "exam.pdf"
            source.write_bytes(b"%PDF-1.4 fake exam bytes for the spy to copy")
            original_bytes = source.read_bytes()
            common.parse_in_scratch(source, _spy, subject="korean")

            self.assertNotEqual(source, captured["source"])
            self.assertEqual(original_bytes, captured["source_bytes"])
            self.assertEqual(captured["source"].parent, captured["work_dir"].parent)
            self.assertEqual(common.MAX_PAGES, captured["max_pages"])
            self.assertEqual({"subject": "korean"}, captured["extra"])

        # No .pipeline_cache warm start on a second run: the copy's directory
        # is cleaned up once parse_in_scratch returns.
        self.assertFalse(captured["source"].parent.exists())


class TestMakeInputs(unittest.TestCase):
    def test_trims_to_four_pages_and_records_the_case(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            source = _write_pdf(Path(temp_dir) / "긴 시험지.pdf", page_count=5)
            target = make_input(source, "korean", root=root)
            with fitz.open(target) as trimmed:
                self.assertEqual(4, trimmed.page_count)
            cases = json.loads((root / "cases.json").read_text(encoding="utf-8"))
        self.assertEqual({"subject": "korean", "source_page_count": 5, "source_name": "긴 시험지.pdf"}, cases["긴_시험지"])
        self.assertEqual(root / "inputs" / "긴_시험지.pdf", target)

    def test_short_documents_are_copied_whole(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            short_source = _write_pdf(Path(temp_dir) / "short.pdf", page_count=2)
            long_source = _write_pdf(Path(temp_dir) / "긴 시험지.pdf", page_count=5)
            # Two calls into the same root exercise the cases.json merge
            # branch (load_json(...) if cases_path.is_file() else {}).
            short_target = make_input(short_source, "science", root=root)
            long_target = make_input(long_source, "korean", root=root)
            with fitz.open(short_target) as copied:
                self.assertEqual(2, copied.page_count)
            cases = json.loads((root / "cases.json").read_text(encoding="utf-8"))
        self.assertEqual({"subject": "science", "source_page_count": 2, "source_name": "short.pdf"}, cases["short"])
        self.assertEqual({"subject": "korean", "source_page_count": 5, "source_name": "긴 시험지.pdf"}, cases["긴_시험지"])
        self.assertEqual(root / "inputs" / "short.pdf", short_target)
        self.assertEqual(root / "inputs" / "긴_시험지.pdf", long_target)


class TestOracleConfig(unittest.TestCase):
    def test_force_config_fails_loudly_and_forces_repair(self):
        config = force_config("")
        self.assertEqual("force", config["mode"])
        self.assertEqual("gemini", config["provider"])
        self.assertTrue(config["fail_on_error"])
        self.assertEqual("gemini-x", force_config("gemini-x")["model"])

    def test_force_config_differs_from_the_desktop_forced_path_only_where_documented(self):
        # Transcribed from app_server.py's ai_fallback="force" call sites,
        # which send threshold=0.72, max_regions=30, timeout_ms=30000 and
        # leave run_problem_export's fail_on_ai_error at its default False;
        # max_tokens/temperature go in unset and build_ai_fallback_config
        # resolves max_tokens to 4096. Spelled out rather than derived from
        # force_config so that a new or silently changed key here fails the
        # test instead of being absorbed into the baseline.
        desktop_forced = {
            "mode": "force",
            "provider": "gemini",
            "model": "",
            "threshold": 0.72,
            "max_regions": 30,
            "max_tokens": 4096,
            "timeout_ms": 30000,
            "save_debug": False,
            "fail_on_error": False,
        }
        config = force_config("")
        # max_output_token_cap has no desktop equivalent at all -- see
        # force_config's docstring and build_problem_board_edb.py's
        # _to_page_ai_config, which only reads the key when the caller's
        # dict carries it. Desktop's ai_fallback_config dicts never do, so
        # AIFallbackConfig.max_output_token_cap stays None for every desktop
        # caller and page_repair.py's per-block output-token estimate is
        # untouched -- checked directly (not just implied by this diff) in
        # test_page_repair.py's max_output_token_cap tests.
        self.assertEqual(set(config) - set(desktop_forced), {"max_output_token_cap"})
        self.assertEqual(8192, config["max_output_token_cap"])
        deltas = {
            key: (desktop_forced[key], config[key])
            for key in desktop_forced
            if desktop_forced[key] != config[key]
        }
        self.assertEqual(
            {
                # The two deliberate deltas force_config's docstring explains.
                "timeout_ms": (30000, 60000),
                "fail_on_error": (False, True),
                # Not a third delta in behaviour: force mode never consults
                # max_regions (pinned by test_page_repair.py's
                # test_force_mode_ignores_max_regions), so the oracle is left
                # on the pipeline default instead of the desktop's override.
                "max_regions": (30, 48),
                # Raised alongside max_output_token_cap=8192 above so
                # configured_max_tokens never clamps the bypass back down --
                # see force_config's docstring for the truncation this fixes.
                "max_tokens": (4096, 8192),
            },
            deltas,
        )

    def test_force_config_max_output_token_cap_reaches_ai_fallback_config(self):
        # Ties force_config's dict directly to the real pipeline conversion
        # (build_problem_board_edb._to_page_ai_config) rather than trusting
        # that the key name alone is enough -- this is the exact function
        # AIFallbackConfig.max_output_token_cap must come out through for the
        # oracle's Gemini calls to actually use it.
        import build_problem_board_edb as board

        config = board._to_page_ai_config(force_config(""))
        self.assertEqual(8192, config.max_output_token_cap)


class TestFailedPageRepairSummaryDiagnostics(unittest.TestCase):
    """oracle._failed_page_repair_summary must surface a Gemini failure's
    diagnostics (page_repair.GeminiRepairResponseError.diagnostics, copied
    onto any wrapping RuntimeError by page_repair._copy_gemini_diagnostics)
    when present, and must not fabricate them for an unrelated failure
    (missing API key, network error) that never carries any.
    """

    def test_surfaces_diagnostics_and_truncated_flag_when_present(self):
        diagnostics = {
            "model": "gemini-3.6-flash",
            "finish_reason": "MAX_TOKENS",
            "effective_max_output_tokens": 776,
            "configured_max_output_tokens": 4096,
            "block_count": 11,
            "include_problem_units": False,
            "prompt_char_count": 2353,
            "prompt_byte_count": 2377,
            "response_char_count": 1345,
            "response_byte_count": 1345,
            "response_head": "{\n  \"problem_start_block_ids\": [",
            "response_tail": "block-004\",\n      \"title\": \"4.\"",
        }
        exc = RuntimeError("AI repair failed after model fallback: Gemini response truncated ...")
        exc.diagnostics = diagnostics
        exc.truncated = True

        summary = oracle._failed_page_repair_summary(exc)

        self.assertTrue(summary["gemini_truncated"])
        self.assertEqual(diagnostics, summary["errors"][0]["gemini_diagnostics"])
        self.assertEqual("oracle_failed", summary["status"])

    def test_omits_diagnostics_for_a_failure_that_never_touched_gemini_json(self):
        summary = oracle._failed_page_repair_summary(RuntimeError("GEMINI_API_KEY not set"))

        self.assertFalse(summary["gemini_truncated"])
        self.assertNotIn("gemini_diagnostics", summary["errors"][0])


class _EmptyCache:
    """PipelineCache stand-in that never serves a cached repair -- copied
    from test_page_repair.py's helper of the same name so this test
    exercises page_repair.repair_page_model's fresh-call branch without a
    cross-module import."""

    def load_ai_repair(self, **_kwargs):
        return None

    def save_ai_repair(self, **_kwargs):
        return None


class TestGeminiTruncationDiagnosticsReachTheOracleThroughRepairPageModel(unittest.TestCase):
    """The exact hop the task asked to be instrumented: a
    page_repair.GeminiRepairTruncatedError raised inside
    page_repair.repair_page_model (fail_on_error=True) must arrive at
    oracle._failed_page_repair_summary with its .diagnostics intact.
    TestFailedPageRepairSummaryDiagnostics above only ever hands
    _failed_page_repair_summary a hand-built RuntimeError with .diagnostics
    already attached by the test itself -- it never exercises
    repair_page_model's `if resolved_config.fail_on_error: raise` (a bare
    raise, which is load-bearing: replacing it with
    `raise RuntimeError(str(exc)) from exc` would drop .diagnostics/.truncated
    silently and this test would catch that where the hand-built-exception
    tests cannot).
    """

    @staticmethod
    def _prepared_page_and_page() -> tuple[PreparedPage, PageModel]:
        prepared_page = PreparedPage(
            page_id="page-1",
            source_path="sample.png",
            page_number=1,
            image=Image.new("RGB", (100, 120), "white"),
            original_size=(100, 120),
        )
        page = PageModel(
            page_id="page-1",
            width_px=100,
            height_px=120,
            subject=Subject.ENGLISH,
            blocks=[
                ContentBlock(
                    block_id="block-1",
                    block_type=BlockType.STEM,
                    bbox=Box(left=0, top=0, width=80, height=40),
                    reading_order=0,
                    text="1. problem",
                )
            ],
        )
        return prepared_page, page

    def test_max_tokens_truncation_reaches_the_oracle_summary_with_diagnostics(self):
        def fake_post_json(url, payload, *, headers, timeout_ms):
            return {
                "candidates": [
                    {
                        "finishReason": "MAX_TOKENS",
                        "content": {"parts": [{"text": '{"problem_start_block_ids": ["blo'}]},
                    }
                ]
            }

        prepared_page, page = self._prepared_page_and_page()
        # max_output_token_cap mirrors the oracle's own force_config, so the
        # same-model retry is skipped and this stays a single fake call per
        # candidate model -- see page_repair.GeminiRepairTruncatedError's
        # docstring for why that skip is oracle-only.
        config = build_ai_fallback_config(mode="force", fail_on_error=True, max_output_token_cap=8192)

        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
            with patch.object(page_repair, "_image_to_base64", return_value="encoded-image"):
                with patch.object(page_repair, "_post_json", side_effect=fake_post_json):
                    with patch.object(page_repair.time, "sleep", side_effect=lambda seconds: None):
                        with self.assertRaises(RuntimeError) as ctx:
                            repair_page_model(
                                prepared_page,
                                page,
                                ocr_mode="gemini",
                                config=config,
                                cache=_EmptyCache(),
                            )

        summary = oracle._failed_page_repair_summary(ctx.exception)

        self.assertTrue(summary["gemini_truncated"])
        error_entry = summary["errors"][0]
        self.assertEqual("MAX_TOKENS", error_entry["gemini_diagnostics"]["finish_reason"])
        self.assertEqual("oracle_failed", summary["status"])

    def test_length_truncation_also_reaches_the_oracle_summary_with_diagnostics(self):
        def fake_post_json(url, payload, *, headers, timeout_ms):
            return {
                "candidates": [
                    {
                        "finishReason": "LENGTH",
                        "content": {"parts": [{"text": '{"problem_start_block_ids": ["blo'}]},
                    }
                ]
            }

        prepared_page, page = self._prepared_page_and_page()
        config = build_ai_fallback_config(mode="force", fail_on_error=True, max_output_token_cap=8192)

        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
            with patch.object(page_repair, "_image_to_base64", return_value="encoded-image"):
                with patch.object(page_repair, "_post_json", side_effect=fake_post_json):
                    with patch.object(page_repair.time, "sleep", side_effect=lambda seconds: None):
                        with self.assertRaises(RuntimeError) as ctx:
                            repair_page_model(
                                prepared_page,
                                page,
                                ocr_mode="gemini",
                                config=config,
                                cache=_EmptyCache(),
                            )

        summary = oracle._failed_page_repair_summary(ctx.exception)

        self.assertTrue(summary["gemini_truncated"])
        self.assertEqual("LENGTH", summary["errors"][0]["gemini_diagnostics"]["finish_reason"])


class TestSummarizePageRepair(unittest.TestCase):
    def test_counts_attempted_applied_changed_models_and_errors_from_page_metadata(self):
        # Fake per-page ai_fallback metadata -- the shape page_repair.py's
        # _attach_ai_fallback_summary attaches to PageModel.metadata. No
        # network, no PageModel, no Gemini call.
        page_repair = [
            {"attempted": True, "applied": True, "changed": True, "model_used": "gemini-3.1-pro-preview", "status": "applied"},
            # A cache hit reused a previous real answer without a fresh
            # network call -- counted separately from "attempted", matching
            # build_run_summary/_summarize_ai_fallback_usage's own fields.
            {"attempted": False, "cache_hit": True, "applied": True, "changed": True, "model_used": "gemini-3.1-pro-preview", "status": "cache_hit"},
            # A validated response was written (applied) but it reproduced
            # the local baseline exactly -- counts toward pages_applied, not
            # pages_changed. This is the exact ambiguity page_repair.py's
            # summary["changed"] exists to resolve.
            {"attempted": True, "applied": True, "changed": False, "model_used": "gemini-3.1-pro-preview", "status": "applied"},
            {"attempted": True, "applied": False, "status": "error", "error": "HTTP 500: boom"},
            {"attempted": False, "applied": False, "status": "disabled"},
        ]
        summary = summarize_page_repair(page_repair)
        self.assertEqual(5, summary["pages_total"])
        self.assertEqual(3, summary["pages_attempted"])
        self.assertEqual(1, summary["pages_cache_hit"])
        self.assertEqual(3, summary["pages_applied"])
        self.assertEqual(2, summary["pages_changed"])
        self.assertEqual(["gemini-3.1-pro-preview"], summary["models_used"])
        self.assertEqual([{"page_index": 3, "status": "error", "error": "HTTP 500: boom"}], summary["errors"])
        self.assertEqual({"applied": 2, "cache_hit": 1, "disabled": 1, "error": 1}, summary["statuses"])
        self.assertFalse(summary["zero_changed"])
        self.assertFalse(summary["no_page_records"])
        self.assertIsNone(summary["warning"])

    def test_zero_changed_case_is_flagged_even_when_pages_were_applied(self):
        # "applied" (a validated response was written) is not "changed" (the
        # write actually differed from the local baseline) -- agreement with
        # the trial here is a silent no-op, not evidence the AI recognized
        # anything, even though one page was fully applied.
        page_repair = [
            {"attempted": True, "applied": True, "changed": False, "status": "applied"},
            {"attempted": False, "applied": False, "changed": False, "status": "disabled"},
        ]
        summary = summarize_page_repair(page_repair)
        self.assertEqual(1, summary["pages_applied"])
        self.assertEqual(0, summary["pages_changed"])
        self.assertTrue(summary["zero_changed"])
        self.assertFalse(summary["no_page_records"])
        self.assertIsNotNone(summary["warning"])
        self.assertIn("NOT evidence of AI-grade recognition", summary["warning"])
        self.assertIn("1/2 pages were applied without changing anything", summary["warning"])

    def test_the_zero_changed_warning_names_the_statuses_that_explain_why(self):
        # page_repair.py reports most silent no-ops through "status" without
        # ever setting "error" -- an unset GEMINI_API_KEY is the dangerous
        # one, since it makes a whole run look like "AI repair changed
        # nothing" with an empty error list. The warning has to carry the
        # reason, not just the count.
        summary = summarize_page_repair(
            [
                {"attempted": False, "applied": False, "status": "missing_api_key"},
                {"attempted": False, "applied": False, "status": "missing_api_key"},
            ]
        )
        self.assertEqual([], summary["errors"])
        self.assertEqual({"missing_api_key": 2}, summary["statuses"])
        self.assertIn("missing_api_key=2", summary["warning"])

    def test_a_page_entry_without_a_status_is_counted_as_unknown(self):
        summary = summarize_page_repair([{"applied": False}])
        self.assertEqual({"unknown": 1}, summary["statuses"])

    def test_a_page_that_actually_changed_something_is_not_flagged(self):
        summary = summarize_page_repair([{"attempted": True, "applied": True, "changed": True, "model_used": "gemini-3.6-flash"}])
        self.assertFalse(summary["zero_changed"])
        self.assertFalse(summary["no_page_records"])
        self.assertIsNone(summary["warning"])

    def test_empty_page_repair_is_flagged_as_no_page_records_not_zero_changed(self):
        # No pages at all is a different, louder failure mode (the
        # instrumentation recorded nothing -- a renamed/missing
        # ParseResult.page_repair field, or a build_pages branch that never
        # attaches ai_fallback) from "measured and found nothing changed".
        # Collapsing them into the same silent, unflagged state would let the
        # instrumentation's own failure mode read as a clean 0/0 row.
        summary = summarize_page_repair([])
        self.assertEqual(0, summary["pages_total"])
        self.assertFalse(summary["zero_changed"])
        self.assertTrue(summary["no_page_records"])
        self.assertIsNotNone(summary["warning"])
        self.assertIn("did not run", summary["warning"])

    def test_summarize_page_repair_writes_zero_changed_not_the_old_zero_applied_name(self):
        # zero_changed is computed from pages_changed, not pages_applied --
        # the old "zero_applied" name inverted that. A fresh summary must
        # never carry the old key at all, so nothing can read it by accident.
        summary = summarize_page_repair([{"attempted": True, "applied": True, "changed": False, "status": "applied"}])
        self.assertNotIn("zero_applied", summary)


class TestZeroChangedFlag(unittest.TestCase):
    def test_reads_the_current_key(self):
        self.assertTrue(oracle.zero_changed_flag({"zero_changed": True, "zero_applied": False}))
        self.assertFalse(oracle.zero_changed_flag({"zero_changed": False}))

    def test_falls_back_to_the_legacy_key_for_an_older_observation_file(self):
        # An oracle observation saved before the zero_applied -> zero_changed
        # rename has only the old key, computed with the exact same
        # (pages_total > 0 and pages_changed == 0) condition -- just named
        # for the wrong counter. Re-scoring or re-adjudicating that file must
        # not require re-running the (paid, network) oracle.
        self.assertTrue(oracle.zero_changed_flag({"zero_applied": True}))
        self.assertFalse(oracle.zero_changed_flag({"zero_applied": False}))

    def test_missing_both_keys_defaults_to_false(self):
        self.assertFalse(oracle.zero_changed_flag({}))


class TestOracleCaseRepairInstrumentation(unittest.TestCase):
    def _fake_result(self, page_repair: list[dict]) -> ParseResult:
        page = ParsedPage(page_id="p1", index=0, width=100, height=100, image=Image.new("RGB", (100, 100), "white"))
        return ParseResult(
            pages=[page],
            problems=[],
            source_page_count=1,
            parser_version="dev",
            timing_ms={"total": 5},
            page_repair=tuple(page_repair),
        )

    def test_observation_json_carries_the_repair_counts(self):
        fake_result = self._fake_result(
            [{"attempted": True, "applied": True, "changed": True, "model_used": "gemini-3.1-pro-preview", "status": "applied"}]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            pdf_path = Path(temp_dir) / "case.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 placeholder, never read by the fake parser")
            with patch.object(oracle, "parse_problems", return_value=fake_result):
                observation = oracle_case(pdf_path, "physics", root=root)
            saved = json.loads((root / "oracle" / "case.json").read_text(encoding="utf-8"))

        expected = {
            "pages_total": 1,
            "pages_attempted": 1,
            "pages_cache_hit": 0,
            "pages_applied": 1,
            "pages_changed": 1,
            "models_used": ["gemini-3.1-pro-preview"],
            "statuses": {"applied": 1},
            "errors": [],
            "zero_changed": False,
            "no_page_records": False,
            "warning": None,
        }
        self.assertEqual(expected, observation["oracle"]["page_repair"])
        # What's on disk (what score.py and a human later read) must match
        # what the function returned in-process.
        self.assertEqual(expected, saved["oracle"]["page_repair"])

    def test_a_zero_changed_case_is_flagged_in_the_saved_observation(self):
        # Applied (a validated response was written) but not changed (it
        # matched the local baseline) -- still zero evidence.
        fake_result = self._fake_result([{"attempted": True, "applied": True, "changed": False, "status": "applied"}])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            pdf_path = Path(temp_dir) / "case.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 placeholder, never read by the fake parser")
            with patch.object(oracle, "parse_problems", return_value=fake_result):
                observation = oracle_case(pdf_path, "physics", root=root)

        self.assertTrue(observation["oracle"]["page_repair"]["zero_changed"])
        self.assertIn("NOT evidence of AI-grade recognition", observation["oracle"]["page_repair"]["warning"])

    def test_oracle_case_forwards_ocr_mode_and_forced_ai_config_to_the_parser(self):
        # patch.object(oracle, "parse_problems", return_value=fake_result) --
        # the pattern every other test in this class uses -- proves nothing
        # about what oracle_case actually asked the parser to do: an
        # oracle_case that dropped ai_fallback_config entirely, or hardcoded
        # ocr_mode, would produce this exact fake_result and pass every
        # assertion above. Capture the kwargs at the boundary instead, same
        # as test_problem_parser.py's test_recognition_arguments_reach_build_pages.
        fake_result = self._fake_result([])
        captured = {}

        def recorder(*args, **kwargs):
            captured.update(kwargs)
            return fake_result

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            pdf_path = Path(temp_dir) / "case.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 placeholder, never read by the fake parser")
            with patch.object(oracle, "parse_problems", side_effect=recorder):
                oracle_case(pdf_path, "physics", ocr_mode="auto", model="gemini-3.6-flash", root=root)

        self.assertEqual("physics", captured.get("subject"))
        self.assertEqual("auto", captured.get("ocr_mode"))
        self.assertEqual(force_config("gemini-3.6-flash"), captured.get("ai_fallback_config"))

    def test_oracle_case_does_not_silently_swallow_a_missing_page_repair_attribute(self):
        # oracle.py used to read getattr(result, "page_repair", ()) or () --
        # a renamed/missing field degraded to an empty tuple and printed a
        # clean-looking 0/0 row instead of failing loudly. Reading
        # result.page_repair directly turns that into an AttributeError.
        class ResultWithoutPageRepair:
            pages: list = []
            problems: list = []
            source_page_count = 0
            timing_ms: dict = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            pdf_path = Path(temp_dir) / "case.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 placeholder, never read by the fake parser")
            with patch.object(oracle, "parse_problems", return_value=ResultWithoutPageRepair()):
                with self.assertRaises(AttributeError):
                    oracle_case(pdf_path, "physics", root=root)

    def test_oracle_case_saves_a_failure_record_and_reraises_when_parsing_fails(self):
        # force_config's fail_on_error=True lets a Gemini exception propagate
        # out of repair_page_model, through build_pages, out of
        # parse_in_scratch. Without this, the case leaves no record of the
        # failure at all -- the only trace would be a terminal traceback.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            pdf_path = Path(temp_dir) / "case.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 placeholder, never read by the fake parser")
            with patch.object(oracle, "parse_problems", side_effect=RuntimeError("Gemini exploded")):
                with self.assertRaisesRegex(RuntimeError, "Gemini exploded"):
                    oracle_case(pdf_path, "physics", root=root)
            saved = json.loads((root / oracle.FAILURE_DIR / "case.json").read_text(encoding="utf-8"))

        page_repair = saved["oracle"]["page_repair"]
        self.assertEqual("oracle_failed", page_repair["status"])
        self.assertTrue(page_repair["no_page_records"])
        self.assertIn("Gemini exploded", page_repair["warning"])
        self.assertIn("Gemini exploded", saved["error"])

    def test_a_failed_case_does_not_clobber_its_previous_successful_observation(self):
        # oracle/<case>.json is this case's pending ground truth -- score.py
        # scores the trial against it, and it is only regenerable with a live
        # Gemini key and the out-of-repo corpus. A transient Gemini failure
        # must not overwrite it with a failure stub: that both destroys the
        # data and leaves score.py a record with no "problems"/"timing_ms",
        # which would abort the scoring loop for every other case too.
        good = self._fake_result([{"attempted": True, "applied": True, "changed": True, "status": "applied"}])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            pdf_path = Path(temp_dir) / "case.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 placeholder, never read by the fake parser")
            with patch.object(oracle, "parse_problems", return_value=good):
                oracle_case(pdf_path, "physics", root=root)
            before = (root / "oracle" / "case.json").read_text(encoding="utf-8")

            with patch.object(oracle, "parse_problems", side_effect=RuntimeError("Gemini exploded")):
                with self.assertRaisesRegex(RuntimeError, "Gemini exploded"):
                    oracle_case(pdf_path, "physics", root=root)

            after = (root / "oracle" / "case.json").read_text(encoding="utf-8")
            self.assertTrue((root / oracle.FAILURE_DIR / "case.json").is_file())

        self.assertEqual(before, after)
        observation = json.loads(after)
        self.assertIn("problems", observation)
        self.assertIn("timing_ms", observation)
        self.assertNotIn("error", observation)


class TestOracleMainReporting(unittest.TestCase):
    def test_main_prints_repair_counts_and_flags_only_the_zero_changed_case(self):
        observations = {
            "case-a": {
                "problems": [{"key": "q1"}],
                "passage_ranges": [],
                "timing_ms": {"total": 100},
                "oracle": {"page_repair": summarize_page_repair([{"attempted": True, "applied": True, "changed": True, "model_used": "gemini-3.1-pro-preview"}])},
            },
            "case-b": {
                "problems": [{"key": "q1"}, {"key": "q2"}],
                "passage_ranges": [],
                "timing_ms": {"total": 200},
                "oracle": {"page_repair": summarize_page_repair([{"attempted": True, "applied": False, "status": "not_needed"}])},
            },
        }

        def fake_oracle_case(pdf, subject, *, ocr_mode, model):
            return observations[pdf.stem]

        with tempfile.TemporaryDirectory() as temp_dir:
            empty_root = Path(temp_dir)  # no cases.json here -- subject always resolves to "unknown"
            with (
                patch.dict(os.environ, {"GEMINI_API_KEY": "fake-oracle-test-key"}),
                patch.object(oracle, "BENCH_ROOT", empty_root),
                patch.object(oracle, "select_inputs", return_value=[Path("case-a.pdf"), Path("case-b.pdf")]),
                patch.object(oracle, "oracle_case", side_effect=fake_oracle_case),
            ):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    exit_code = oracle.main(["--runtime-dir", str(empty_root)])

        self.assertEqual(0, exit_code)
        # stdout is the durable record: the table gets pasted into
        # docs/web-trial-quality.md and read later by someone who never saw
        # this terminal, so the verdict has to ride along in the row itself
        # (repair_changed, not repair_applied) and in a note under the table
        # -- a stderr-only warning vanishes the moment anyone redirects
        # stdout into a file.
        report = out.getvalue()
        self.assertIn("| case-a | unknown | 1 | 0 | 100 | 1/1 | 0/1 | 1/1 | 1/1 | gemini-3.1-pro-preview | 0 |", report)
        self.assertIn("| case-b | unknown | 2 | 0 | 200 | 1/1 | 0/1 | 0/1 (NO AI EVIDENCE) | 0/1 | - | 0 |", report)
        self.assertIn("1 of 2 case(s) are NOT evidence of AI-grade recognition", report)
        self.assertIn("`case-b`", report)
        self.assertIn("NOT evidence of AI-grade recognition", report)
        self.assertNotIn("`case-a`", report)
        # stderr keeps the operator-facing copy, matching score.py's _warn.
        warning_text = err.getvalue()
        self.assertIn("case-b:", warning_text)
        self.assertIn("NOT evidence of AI-grade recognition", warning_text)
        self.assertNotIn("case-a:", warning_text)

    def test_main_prints_no_zero_changed_note_when_every_case_changed_a_page(self):
        observation = {
            "problems": [{"key": "q1"}],
            "passage_ranges": [],
            "timing_ms": {"total": 100},
            "oracle": {"page_repair": summarize_page_repair([{"attempted": True, "applied": True, "changed": True, "model_used": "gemini-3.1-pro-preview"}])},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            empty_root = Path(temp_dir)
            with (
                patch.dict(os.environ, {"GEMINI_API_KEY": "fake-oracle-test-key"}),
                patch.object(oracle, "BENCH_ROOT", empty_root),
                patch.object(oracle, "select_inputs", return_value=[Path("case-a.pdf")]),
                patch.object(oracle, "oracle_case", return_value=observation),
            ):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    exit_code = oracle.main(["--runtime-dir", str(empty_root)])

        self.assertEqual(0, exit_code)
        self.assertNotIn("NO AI EVIDENCE", out.getvalue())
        self.assertNotIn("NOT evidence of AI-grade recognition", out.getvalue())
        self.assertEqual("", err.getvalue())

    def test_main_flags_a_case_whose_oracle_run_produced_no_page_records(self):
        # pages_total == 0 is the instrumentation's own failure mode (see
        # TestSummarizePageRepair's no_page_records tests) -- it must not
        # read as an unremarkable, unflagged 0/0 row.
        observation = {
            "problems": [],
            "passage_ranges": [],
            "timing_ms": {"total": 100},
            "oracle": {"page_repair": summarize_page_repair([])},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            empty_root = Path(temp_dir)
            with (
                patch.dict(os.environ, {"GEMINI_API_KEY": "fake-oracle-test-key"}),
                patch.object(oracle, "BENCH_ROOT", empty_root),
                patch.object(oracle, "select_inputs", return_value=[Path("case-a.pdf")]),
                patch.object(oracle, "oracle_case", return_value=observation),
            ):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    exit_code = oracle.main(["--runtime-dir", str(empty_root)])

        self.assertEqual(0, exit_code)
        report = out.getvalue()
        self.assertIn("0/0 (NO AI EVIDENCE)", report)
        self.assertIn("did not run", report)
        self.assertIn("did not run", err.getvalue())

    def test_main_still_prints_the_table_and_exits_nonzero_when_one_case_fails(self):
        # A case whose oracle_case call raises (force_config's
        # fail_on_error=True) must not cost the run its whole table: the
        # cases that already succeeded are still worth printing and pasting
        # into docs.
        def fake_oracle_case(pdf, subject, *, ocr_mode, model):
            if pdf.stem == "case-a":
                return {
                    "problems": [{"key": "q1"}],
                    "passage_ranges": [],
                    "timing_ms": {"total": 100},
                    "oracle": {"page_repair": summarize_page_repair([{"attempted": True, "applied": True, "changed": True}])},
                }
            raise RuntimeError("Gemini exploded")

        with tempfile.TemporaryDirectory() as temp_dir:
            empty_root = Path(temp_dir)
            with (
                patch.dict(os.environ, {"GEMINI_API_KEY": "fake-oracle-test-key"}),
                patch.object(oracle, "BENCH_ROOT", empty_root),
                patch.object(oracle, "select_inputs", return_value=[Path("case-a.pdf"), Path("case-b.pdf")]),
                patch.object(oracle, "oracle_case", side_effect=fake_oracle_case),
            ):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    exit_code = oracle.main(["--runtime-dir", str(empty_root)])

        self.assertEqual(1, exit_code)
        report = out.getvalue()
        self.assertIn("| case-a |", report)
        self.assertIn("| case-b |", report)
        self.assertIn("ERROR", report)
        self.assertIn("Gemini exploded", err.getvalue())


def _obs(case: str, problems: list[tuple], total_ms: int = 100) -> dict:
    """Build a fake observation. Each problem tuple is (key, number, title, regions[, risk_flags])."""
    entries = []
    ranges = []
    for key, number, title, regions, *rest in problems:
        risk_flags = rest[0] if rest else []
        span = common.passage_range_from_title(title) if number is None else None
        if span:
            ranges.append(span)
        entries.append(
            {
                "key": key, "number": number, "title": title, "passage_range": span,
                "regions": [{"page_index": p, "bbox": {"left": l, "top": t, "width": w, "height": h}} for p, l, t, w, h in regions],
                "risk_flags": risk_flags, "crop": None,
            }
        )
    return {"case": case, "pages": 3, "source_page_count": 16, "page_sizes": [[600, 800]] * 3, "problems": entries, "passage_ranges": ranges, "timing_ms": {"total": total_ms}}


class TestScore(unittest.TestCase):
    def test_bbox_iou(self):
        box = {"left": 0.0, "top": 0.0, "width": 10.0, "height": 10.0}
        self.assertEqual(1.0, bbox_iou(box, box))
        self.assertEqual(0.0, bbox_iou(box, {"left": 20.0, "top": 0.0, "width": 10.0, "height": 10.0}))
        self.assertAlmostEqual(0.25, bbox_iou(box, {"left": 0.0, "top": 0.0, "width": 5.0, "height": 5.0}))

    def test_regions_iou_counts_pages_present_on_one_side_as_union(self):
        a = [{"page_index": 0, "bbox": {"left": 0.0, "top": 0.0, "width": 10.0, "height": 10.0}}]
        b = a + [{"page_index": 1, "bbox": {"left": 0.0, "top": 0.0, "width": 10.0, "height": 10.0}}]
        self.assertAlmostEqual(0.5, regions_iou(a, b))
        self.assertEqual(1.0, regions_iou(b, b))

    def test_regions_iou_unions_several_regions_on_one_page(self):
        """Spec 5-1 compares the per-page union box, so region order and count must not matter."""
        top_half = {"page_index": 0, "bbox": {"left": 0.0, "top": 0.0, "width": 10.0, "height": 10.0}}
        bottom_half = {"page_index": 0, "bbox": {"left": 0.0, "top": 10.0, "width": 10.0, "height": 10.0}}
        self.assertEqual(1.0, regions_iou([top_half, bottom_half], [bottom_half, top_half]))
        # Same envelope height in the right-hand column: disjoint, so nowhere near 1.0.
        right_column = [
            {"page_index": 0, "bbox": {"left": 20.0, "top": 0.0, "width": 10.0, "height": 10.0}},
            {"page_index": 0, "bbox": {"left": 20.0, "top": 10.0, "width": 10.0, "height": 10.0}},
        ]
        self.assertEqual(0.0, regions_iou([top_half, bottom_half], right_column))
        # Half-overlapping envelopes (columns 0-10 vs 5-15) over the same rows.
        straddling = [
            {"page_index": 0, "bbox": {"left": 5.0, "top": 0.0, "width": 10.0, "height": 10.0}},
            {"page_index": 0, "bbox": {"left": 5.0, "top": 10.0, "width": 10.0, "height": 10.0}},
        ]
        self.assertAlmostEqual(1 / 3, regions_iou([top_half, bottom_half], straddling))

    def test_score_case_recall_precision_and_low_iou(self):
        trial = _obs(
            "c",
            [
                ("q1", 1, "1번", [(0, 0, 0, 10, 10)], ["fallback_grouping"]),
                ("q2", 2, "2번", [(0, 0, 20, 10, 10)], ["low_confidence"]),
                ("p1-3", None, "지문 1~3", [(0, 0, 40, 10, 10)]),
            ],
        )
        expected = {p["key"]: p for p in _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)]), ("q2", 2, "2번", [(0, 0, 25, 10, 10)]), ("q3", 3, "3번", [(1, 0, 0, 10, 10)]), ("p1-3", None, "지문 1~3", [(0, 0, 40, 10, 10)])])["problems"]}
        score = score_case(trial, expected)
        self.assertAlmostEqual(2 / 3, score["question_recall"])
        self.assertEqual(1.0, score["question_precision"])
        self.assertEqual(1.0, score["passage_recall"])
        self.assertEqual(1.0, score["passage_precision"])
        self.assertEqual(["q3"], score["missing"])
        self.assertEqual([], score["extra"])
        self.assertEqual(1, score["low_iou"])  # q2 overlaps by half
        self.assertAlmostEqual(7 / 9, score["mean_iou"])  # (1.0 + 1/3 + 1.0) / 3
        # Only q1's "fallback_grouping" is a REVIEW_WORTHY flag; q2's
        # "low_confidence" must not trip the frozenset filter.
        self.assertAlmostEqual(1 / 3, score["review_rate"])
        self.assertEqual(100, score["trial_ms"])

    def test_score_case_counts_unnumbered_non_passage_keys_as_extra(self):
        """common.problem_key emits "t:<title>" for a unit with no number and no passage marker."""
        trial = _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)]), ("t:그림", None, "그림", [(0, 0, 40, 10, 10)])])
        expected = {p["key"]: p for p in _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)])])["problems"]}
        score = score_case(trial, expected)
        self.assertEqual(1.0, score["question_recall"])
        self.assertEqual(1.0, score["question_precision"])
        self.assertEqual(["t:그림"], score["extra"])

    def test_expected_from_applies_approved_labels(self):
        trial = _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)]), ("q9", 9, "9번", [(0, 0, 0, 10, 10)])])
        oracle = _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)]), ("q2", 2, "2번", [(0, 0, 0, 10, 10)])])
        pending, status = expected_from(oracle, trial, None)
        self.assertEqual("pending", status)
        self.assertEqual({"q1", "q2"}, set(pending))

        # An un-adjudicated labels file (status still "pending") must not be
        # applied even though it already carries items -- only the guard on
        # `status == "approved"`, not merely a truthy labels dict, may apply them.
        unapproved = {"case": "c", "status": "pending", "items": [{"key": "q2", "truth": "neither"}]}
        still_pending, status = expected_from(oracle, trial, unapproved)
        self.assertEqual("pending", status)
        self.assertEqual({"q1", "q2"}, set(still_pending))

        labels = {"case": "c", "status": "approved", "items": [{"key": "q2", "truth": "neither"}, {"key": "q9", "truth": "trial"}]}
        approved, status = expected_from(oracle, trial, labels)
        self.assertEqual("approved", status)
        self.assertEqual({"q1", "q9"}, set(approved))

        # truth "oracle" is an explicit no-op: the oracle's own entry stands.
        oracle_noop = {"case": "c", "status": "approved", "items": [{"key": "q1", "truth": "oracle"}]}
        kept, status = expected_from(oracle, trial, oracle_noop)
        self.assertEqual({"q1", "q2"}, set(kept))

        # truth "both" credits the trial's own detection (a disagreement
        # judged acceptable either way) instead of leaving it in "extra".
        both = {"case": "c", "status": "approved", "items": [{"key": "q9", "truth": "both"}]}
        both_expected, status = expected_from(oracle, trial, both)
        self.assertEqual({"q1", "q2", "q9"}, set(both_expected))

    def test_expected_from_rejects_unknown_truth_but_ignores_a_stale_key(self):
        trial = _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)])])
        oracle = _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)])])
        typo_truth = {"case": "c", "status": "approved", "items": [{"key": "q1", "truth": "orcale"}]}
        with self.assertRaises(ValueError):
            expected_from(oracle, trial, typo_truth)

        # A key neither side reports any more is a stale label, not a broken
        # one: an approved "trial only" verdict stops matching as soon as a
        # parser fix removes that detection, which is exactly the before/after
        # workflow. Raising there would abort scoring for every case, so it is
        # skipped with a collected warning instead.
        stale = {"case": "c", "status": "approved", "items": [{"key": "t:보기 중 옳은 것은?", "truth": "neither"}]}
        warnings: list[str] = []
        with contextlib.redirect_stderr(io.StringIO()):
            expected, status = expected_from(oracle, trial, stale, warnings=warnings)
        self.assertEqual("approved", status)
        self.assertEqual({"q1"}, set(expected))
        self.assertEqual(1, len(warnings))
        self.assertIn("no longer present", warnings[0])
        self.assertIn("t:보기 중 옳은 것은?", warnings[0])

    def test_render_report_has_one_row_per_case_and_an_aggregate(self):
        rows = [
            {"case": "a", "status": "approved", "question_recall": 1.0, "question_precision": 1.0, "passage_recall": 1.0, "passage_precision": 1.0, "mean_iou": 0.95, "low_iou": 0, "review_rate": 0.0, "missing": [], "extra": [], "trial_ms": 1500, "oracle_ms": 9000},
            {"case": "b", "status": "pending", "question_recall": 0.5, "question_precision": 1.0, "passage_recall": 1.0, "passage_precision": 1.0, "mean_iou": 0.55, "low_iou": 2, "review_rate": 0.5, "missing": ["q7"], "extra": [], "trial_ms": 2000, "oracle_ms": 8000},
        ]
        report = render_report(rows)
        self.assertIn("| a | approved | 1.00 |", report)
        self.assertIn("| b | pending | 0.50 |", report)
        # A two-row fixture with different values, so a bare sum (instead of a
        # mean) or dropped low_iou/missing/extra totals would fail this: only
        # 1 of 2 rows is approved, the recall/iou columns are means (0.75,
        # 0.75), and low_iou/missing/extra are summed (2, 1, 0).
        self.assertIn("| 합계 | 1/2 approved | 0.75 | 1.00 | 1.00 | 1.00 | 0.75 | 2 | 0.25 | 1 | 0 |", report)

    def test_render_report_leaves_undefined_ratios_as_empty_cells(self):
        rows = [{"case": "a", "status": "pending", "question_recall": None, "question_precision": 1.0, "passage_recall": None, "passage_precision": None, "mean_iou": None, "low_iou": 0, "review_rate": 0.0, "missing": [], "extra": [], "trial_ms": 100, "oracle_ms": 200}]
        report = render_report(rows)
        self.assertIn("| a | pending |  | 1.00 |  |  |  | 0 |", report)

    def test_render_report_keeps_unnumbered_keys_out_of_the_table(self):
        # An unnumbered unit's key carries up to 120 characters of raw exam
        # text, pipes and newlines included; rendered verbatim it both leaks
        # exam content into the committed doc and shifts trial_ms/oracle_ms
        # out of their columns.
        rows = [{"case": "c", "status": "pending", "question_recall": 1.0, "question_precision": 1.0, "passage_recall": None, "passage_precision": None, "mean_iou": 1.0, "low_iou": 0, "review_rate": 0.0, "missing": [], "extra": ["q5", "t:다음 표는 | 원소 A~C의\n성질이다.", "t:그림"], "trial_ms": 100, "oracle_ms": 200}]
        report = render_report(rows)
        header, _, body = report.splitlines()[:3]
        self.assertNotIn("원소", body)
        self.assertIn("q5 t:#1 t:#2", body)
        # Trailing "?" is the ai_evidence column's fallback for a row with no
        # such field, same as every other hand-built fixture in this class.
        self.assertTrue(body.endswith("| 100 | 200 | ? |"), body)
        self.assertEqual(header.count("|"), body.count("|"))

    def test_render_report_adds_an_ai_evidence_column_and_a_zero_evidence_footnote(self):
        rows = [
            {"case": "a", "status": "approved", "question_recall": 1.0, "question_precision": 1.0, "passage_recall": None, "passage_precision": None, "mean_iou": 1.0, "low_iou": 0, "review_rate": 0.0, "missing": [], "extra": [], "trial_ms": 100, "oracle_ms": 200, "ai_evidence": "2/2", "ai_evidence_ok": True},
            {"case": "b", "status": "pending", "question_recall": 1.0, "question_precision": 1.0, "passage_recall": None, "passage_precision": None, "mean_iou": 1.0, "low_iou": 0, "review_rate": 0.0, "missing": [], "extra": [], "trial_ms": 100, "oracle_ms": 200, "ai_evidence": "0/2 (NO EVIDENCE)", "ai_evidence_ok": False},
        ]
        report = render_report(rows)
        self.assertIn("ai_evidence", report.splitlines()[0])
        self.assertIn("| a | approved | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 100 | 200 | 2/2 |", report)
        self.assertIn("| b | pending | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 100 | 200 | 0/2 (NO EVIDENCE) |", report)
        # The caveat is generated fresh every call, inside the same string
        # that update_doc wraps in DOC_START/DOC_END -- unlike the old
        # hand-written banner in docs/web-trial-quality.md, it cannot go
        # stale relative to the numbers next to it.
        self.assertIn("AI page repair produced no evidence of a real change for 1 of 2 case(s): `b`", report)

    def test_render_report_marks_a_row_with_no_ai_evidence_field_as_unknown_not_zero(self):
        # A row with no ai_evidence field at all (every other render_report
        # test in this file, matching score.py's output before this
        # instrumentation existed) renders "?" and must not be swept into
        # the zero-evidence footnote -- "unknown" is not "measured and found
        # nothing".
        rows = [{"case": "a", "status": "approved", "question_recall": 1.0, "question_precision": 1.0, "passage_recall": None, "passage_precision": None, "mean_iou": 1.0, "low_iou": 0, "review_rate": 0.0, "missing": [], "extra": [], "trial_ms": 100, "oracle_ms": 200}]
        report = render_report(rows)
        self.assertIn("| a | approved | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 100 | 200 | ? |", report)
        self.assertNotIn("no evidence of a real change", report)

    def test_render_report_adds_a_footnote_naming_excluded_cases_and_why(self):
        # This is the fix for both English bench cases vanishing with no
        # trace: an excluded case must appear inside the same generated
        # block score.py writes into docs/web-trial-quality.md, not only in
        # a warning that the next --doc run can't see.
        rows = [{"case": "a", "status": "approved", "question_recall": 1.0, "question_precision": 1.0, "passage_recall": None, "passage_precision": None, "mean_iou": 1.0, "low_iou": 0, "review_rate": 0.0, "missing": [], "extra": [], "trial_ms": 100, "oracle_ms": 200}]
        excluded = [
            {"case": "english_2020suneung_go3_20191107", "reason": "no oracle observation found"},
            {"case": "english_go2_hakpyeong_20260324", "reason": "Gemini response JSON decode failed: Unterminated string"},
        ]
        report = render_report(rows, excluded)
        self.assertIn("2 case(s) excluded from this report", report)
        self.assertIn("`english_2020suneung_go3_20191107` (no oracle observation found)", report)
        self.assertIn("`english_go2_hakpyeong_20260324` (Gemini response JSON decode failed: Unterminated string)", report)

    def test_render_report_has_no_excluded_footnote_when_nothing_was_excluded(self):
        rows = [{"case": "a", "status": "approved", "question_recall": 1.0, "question_precision": 1.0, "passage_recall": None, "passage_precision": None, "mean_iou": 1.0, "low_iou": 0, "review_rate": 0.0, "missing": [], "extra": [], "trial_ms": 100, "oracle_ms": 200}]
        self.assertNotIn("excluded from this report", render_report(rows))
        self.assertNotIn("excluded from this report", render_report(rows, []))


class TestScoreAll(unittest.TestCase):
    def test_score_all_filters_cases_skips_missing_oracle_and_carries_oracle_ms(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(common.bench_dir("trial", root) / "a.json", _obs("a", [("q1", 1, "1번", [(0, 0, 0, 10, 10)])], total_ms=111))
            common.save_json(common.bench_dir("oracle", root) / "a.json", _obs("a", [("q1", 1, "1번", [(0, 0, 0, 10, 10)])], total_ms=222))
            common.save_json(common.bench_dir("labels", root) / "a.json", {"case": "a", "status": "approved", "items": []})
            common.save_json(common.bench_dir("trial", root) / "b.json", _obs("b", [("q1", 1, "1번", [(0, 0, 0, 10, 10)])], total_ms=333))
            common.save_json(common.bench_dir("oracle", root) / "b.json", _obs("b", [("q1", 1, "1번", [(0, 0, 0, 10, 10)])], total_ms=444))
            # "c" has a trial but no oracle -- it must be skipped, not crash,
            # but it must not vanish silently either: a bare `continue` here
            # is exactly how both English bench cases disappeared from the
            # generated corpus table with no row, no warning and no footnote
            # to explain why.
            common.save_json(common.bench_dir("trial", root) / "c.json", _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)])], total_ms=555))

            excluded: list[dict[str, str]] = []
            with contextlib.redirect_stderr(io.StringIO()) as captured:
                rows = score_all([], root=root, excluded=excluded)
            by_case = {row["case"]: row for row in rows}
            self.assertEqual({"a", "b"}, set(by_case))
            self.assertEqual([{"case": "c", "reason": "no oracle observation found"}], excluded)
            self.assertIn("case 'c'", captured.getvalue())
            self.assertIn("no oracle observation found", captured.getvalue())
            self.assertEqual("approved", by_case["a"]["status"])
            self.assertEqual("pending", by_case["b"]["status"])
            self.assertEqual(111, by_case["a"]["trial_ms"])
            self.assertEqual(222, by_case["a"]["oracle_ms"])  # from the oracle's total, not the trial's
            # _obs() (used for both the trial and oracle fixture files here)
            # carries no "oracle" key at all -- an oracle observation from
            # before this instrumentation existed -- so score_all must fall
            # back to "unknown", not crash or claim zero evidence.
            self.assertEqual("?", by_case["a"]["ai_evidence"])
            self.assertIsNone(by_case["a"]["ai_evidence_ok"])

            filtered = score_all(["a"], root=root)
            self.assertEqual(["a"], [row["case"] for row in filtered])

    def test_score_all_reads_ai_evidence_from_the_oracle_observations_page_repair(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(common.bench_dir("trial", root) / "a.json", _obs("a", [("q1", 1, "1번", [(0, 0, 0, 10, 10)])], total_ms=100))
            oracle_obs = _obs("a", [("q1", 1, "1번", [(0, 0, 0, 10, 10)])], total_ms=200)
            oracle_obs["oracle"] = {"page_repair": {"pages_total": 2, "pages_changed": 1}}
            common.save_json(common.bench_dir("oracle", root) / "a.json", oracle_obs)

            rows = score_all([], root=root)
            self.assertEqual(1, len(rows))
            self.assertEqual("1/2", rows[0]["ai_evidence"])
            self.assertTrue(rows[0]["ai_evidence_ok"])

    def test_score_all_skips_an_unscorable_oracle_observation_instead_of_aborting_the_run(self):
        # A failure-shaped record ({case, error, oracle}) left under oracle/
        # -- by an older build, or by hand -- has no "problems" and no
        # "timing_ms". Indexing into it raises KeyError inside the loop, which
        # would cost every *other* case its row too: no report.md, no doc
        # table. One bad case must cost only its own row.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for case in ("a", "b"):
                common.save_json(common.bench_dir("trial", root) / f"{case}.json", _obs(case, [("q1", 1, "1번", [(0, 0, 0, 10, 10)])], total_ms=100))
            common.save_json(common.bench_dir("oracle", root) / "b.json", _obs("b", [("q1", 1, "1번", [(0, 0, 0, 10, 10)])], total_ms=200))
            common.save_json(
                common.bench_dir("oracle", root) / "a.json",
                {"case": "a", "error": "Gemini exploded", "oracle": {"page_repair": oracle._failed_page_repair_summary(RuntimeError("Gemini exploded"))}},
            )

            warnings: list[str] = []
            excluded: list[dict[str, str]] = []
            with contextlib.redirect_stderr(io.StringIO()):
                rows = score_all([], root=root, warnings=warnings, excluded=excluded)

        self.assertEqual(["b"], [row["case"] for row in rows])
        self.assertEqual(1, len(warnings))
        self.assertIn("not a scorable oracle observation", warnings[0])
        self.assertIn("Gemini exploded", warnings[0])
        # Same fact, structured for the report footnote instead of scraped
        # out of the warning text.
        self.assertEqual([{"case": "a", "reason": "Gemini exploded"}], excluded)

    def test_score_all_still_scores_a_case_whose_label_key_went_stale(self):
        """The before/after workflow: the fixed parser no longer emits an approved "trial only" key."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(common.bench_dir("trial", root) / "c.json", _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)])], total_ms=100))
            common.save_json(common.bench_dir("oracle", root) / "c.json", _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)])], total_ms=200))
            common.save_json(
                common.bench_dir("labels", root) / "c.json",
                {"case": "c", "status": "approved", "items": [{"key": "t:보기 중 옳은 것은?", "truth": "neither"}]},
            )
            warnings: list[str] = []
            with contextlib.redirect_stderr(io.StringIO()):
                rows = score_all([], root=root, warnings=warnings)
            self.assertEqual(["c"], [row["case"] for row in rows])
            self.assertEqual("approved", rows[0]["status"])
            self.assertEqual(1.0, rows[0]["question_recall"])
            self.assertEqual(1, len(warnings))
            self.assertIn("no longer present", warnings[0])


class TestScoreMainReporting(unittest.TestCase):
    def test_main_reports_the_real_reason_a_named_case_has_no_row(self):
        # main() used to compute its "no trial observation found" message
        # from the same `matched` set that score_all's oracle guard also
        # removes a case from -- so a case with a trial file but an
        # unscorable oracle observation got the wrong, misleading reason.
        def fake_score_all(cases, excluded=None):
            if excluded is not None:
                excluded.append({"case": "bad-oracle", "reason": "Gemini exploded"})
            return []

        with patch.object(score, "score_all", side_effect=fake_score_all):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                exit_code = score.main(["bad-oracle", "never-observed"])

        self.assertEqual(1, exit_code)
        output = err.getvalue()
        self.assertIn("case 'bad-oracle' was excluded from the report: Gemini exploded", output)
        self.assertIn("no trial observation found for case 'never-observed'", output)
        self.assertNotIn("no trial observation found for case 'bad-oracle'", output)


class TestAdjudicate(unittest.TestCase):
    def test_disagreements_cover_missing_extra_and_low_iou(self):
        trial = _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)]), ("q2", 2, "2번", [(0, 0, 20, 10, 10)]), ("q9", 9, "9번", [(0, 0, 0, 10, 10)])])
        oracle = _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)]), ("q2", 2, "2번", [(0, 0, 25, 10, 10)]), ("q3", 3, "3번", [(1, 0, 0, 10, 10)])])
        items = disagreements(trial, oracle)
        self.assertEqual(["q2", "q3", "q9"], [item["key"] for item in items])
        self.assertEqual(["iou 0.33", "oracle only", "trial only"], [item["reason"] for item in items])

    def test_labels_skeleton_is_pending_with_empty_truth(self):
        skeleton = labels_skeleton("c", [{"key": "q3", "reason": "oracle only", "trial": None, "oracle": {}}])
        self.assertEqual({"case": "c", "status": "pending", "items": [{"key": "q3", "reason": "oracle only", "truth": None, "note": ""}]}, skeleton)

    def test_compose_writes_a_png_even_without_crops(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "c" / "q3.png"
            compose({"key": "q3", "reason": "oracle only", "trial": None, "oracle": {"crop": None}}, out)
            with Image.open(out) as image:
                self.assertEqual("RGB", image.mode)
                self.assertGreater(image.width, 400)
                # Both sides fall back to a 400x200 lightgray placeholder, so
                # the canvas is exactly two panels wide (830x240) -- and each
                # panel's centre really is the placeholder, not a blank white
                # canvas that happens to satisfy the width/mode checks above.
                self.assertEqual((830, 240), image.size)
                self.assertEqual((211, 211, 211), image.getpixel((210, 130)))
                self.assertEqual((211, 211, 211), image.getpixel((620, 130)))
                # The header strip (y in [9, 19]) carries the "trial: q3
                # (oracle only)" text, so it is not uniform white either --
                # pins "two panels, side by side" rather than "some RGB file".
                header_row = [image.getpixel((x, 15)) for x in range(11, 102)]
                self.assertTrue(any(pixel != (255, 255, 255) for pixel in header_row))


class TestAdjudicateCase(unittest.TestCase):
    def test_writes_one_png_per_disagreement_and_never_overwrites_labels(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            # "t:A/B 비교" is an unnumbered fallback key (common.problem_key's
            # "t:<title>" case) carrying a "/" that must not become a path
            # separator in the adjudication PNG's filename.
            trial = _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)]), ("t:A/B 비교", None, "A/B 비교", [(0, 0, 40, 10, 10)])])
            oracle = _obs("c", [("q1", 1, "1번", [(0, 0, 25, 10, 10)])])
            common.save_json(common.bench_dir("trial", root) / "c.json", trial)
            common.save_json(common.bench_dir("oracle", root) / "c.json", oracle)

            count = adjudicate_case("c", root)
            self.assertEqual(2, count)  # q1 (iou 0.00) + the trial-only key

            case_dir = common.bench_dir("adjudication", root) / "c"
            pngs = list(case_dir.glob("*.png"))
            self.assertEqual(2, len(pngs))
            for png in pngs:
                self.assertNotIn("/", png.name)
                self.assertEqual(case_dir, png.parent)

            labels_path = common.bench_dir("labels", root) / "c.json"
            skeleton = common.load_json(labels_path)
            self.assertEqual("pending", skeleton["status"])
            self.assertEqual({"q1", "t:A/B 비교"}, {item["key"] for item in skeleton["items"]})

            # "Existing label files are never overwritten" (module docstring):
            # a human verdict already on disk must survive a rerun untouched.
            approved = {"case": "c", "status": "approved", "items": [{"key": "q1", "truth": "oracle", "note": "checked"}]}
            common.save_json(labels_path, approved)
            adjudicate_case("c", root)
            self.assertEqual(approved, common.load_json(labels_path))

    def test_warns_but_still_keeps_a_stale_pending_skeleton(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(common.bench_dir("trial", root) / "c.json", _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)])]))
            common.save_json(common.bench_dir("oracle", root) / "c.json", _obs("c", [("q1", 1, "1번", [(0, 0, 25, 10, 10)])]))
            adjudicate_case("c", root)
            labels_path = common.bench_dir("labels", root) / "c.json"
            stale = common.load_json(labels_path)
            self.assertEqual("pending", stale["status"])

            # Re-observing the case adds a disagreement the pending skeleton
            # never saw; adjudicate_case must still not touch that file.
            common.save_json(
                common.bench_dir("trial", root) / "c.json",
                _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)]), ("q2", 2, "2번", [(0, 0, 0, 10, 10)])]),
            )
            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                adjudicate_case("c", root)
            self.assertIn("q2", captured.getvalue())
            self.assertEqual(stale, common.load_json(labels_path))

    def test_skips_an_unscorable_oracle_observation_instead_of_aborting(self):
        # adjudicate_case used to read oracle["problems"] with no guard at
        # all: a truncated or failure-shaped oracle record (the exact shape
        # oracle_case saves under FAILURE_DIR, or leaves behind on an
        # interrupted run) raised KeyError here and would abort the whole
        # `for oracle_path in ...` loop in main(), costing every other case
        # its row too -- the same failure mode score.py's score_all already
        # guards against.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(common.bench_dir("trial", root) / "a.json", _obs("a", [("q1", 1, "1번", [(0, 0, 0, 10, 10)])]))
            common.save_json(
                common.bench_dir("oracle", root) / "a.json",
                {"case": "a", "error": "Gemini exploded", "oracle": {"page_repair": oracle._failed_page_repair_summary(RuntimeError("Gemini exploded"))}},
            )

            warnings: list[str] = []
            excluded: list[dict[str, str]] = []
            with contextlib.redirect_stderr(io.StringIO()) as captured:
                count = adjudicate_case("a", root, warnings=warnings, excluded=excluded)

        self.assertIsNone(count)
        self.assertEqual(1, len(warnings))
        self.assertIn("case 'a'", warnings[0])
        self.assertIn("not scorable", warnings[0])
        self.assertIn("Gemini exploded", warnings[0])
        self.assertEqual([{"case": "a", "reason": "Gemini exploded"}], excluded)
        # No PNGs, no labels skeleton -- nothing that would misrepresent this
        # case as having zero disagreements.
        self.assertFalse((common.bench_dir("adjudication", root) / "a").exists())
        self.assertFalse((common.bench_dir("labels", root) / "a.json").exists())


class TestAdjudicateMain(unittest.TestCase):
    def test_continues_past_an_excluded_case_and_footnotes_the_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for case in ("a", "b"):
                common.save_json(common.bench_dir("trial", root) / f"{case}.json", {})
                common.save_json(common.bench_dir("oracle", root) / f"{case}.json", {})

            def fake_adjudicate_case(case, root=None, **kwargs):
                excluded = kwargs.get("excluded")
                if case == "a":
                    if excluded is not None:
                        excluded.append({"case": "a", "reason": "Gemini exploded"})
                    return None
                return 3

            with (
                patch.object(adjudicate, "bench_dir", lambda name: common.bench_dir(name, root)),
                patch.object(adjudicate, "adjudicate_case", side_effect=fake_adjudicate_case),
            ):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    exit_code = adjudicate.main([])

        self.assertEqual(0, exit_code)
        report = out.getvalue()
        table_only, _, footnote = report.partition("\n\n")
        self.assertIn("| b | 3 |", table_only)
        self.assertNotIn("| a |", table_only)
        self.assertIn("1 case(s) excluded", footnote)
        self.assertIn("`a` (Gemini exploded)", footnote)


class TestProbeSummaries(unittest.TestCase):
    def test_summarize_file_treats_second_call_onward_as_warm(self):
        rows = [
            {"status": 200, "wall_ms": 9000, "bytes": 10, "timing_ms": {"total": 8000, "render": 1000, "segment": 3000, "assets": 2000}, "instance_id": "a", "instance_age_s": 5.0},
            {"status": 200, "wall_ms": 7000, "bytes": 12, "timing_ms": {"total": 6500, "render": 900, "segment": 2900, "assets": 1900}, "instance_id": "a", "instance_age_s": 20.0},
            {"status": 200, "wall_ms": 7200, "bytes": 12, "timing_ms": {"total": 6600, "render": 950, "segment": 2950, "assets": 1950}, "instance_id": "a", "instance_age_s": 30.0},
        ]
        summary = summarize_file(rows)
        self.assertEqual(9000, summary["first_wall_ms"])
        self.assertEqual(7000, summary["warm_p50_ms"])  # nearest-rank p50 of [7000, 7200]
        self.assertEqual(7200, summary["warm_max_ms"])
        self.assertEqual(6500, summary["parse_p50_ms"])
        self.assertEqual(900, summary["render_p50_ms"])
        self.assertEqual(["a"], summary["instances"])
        self.assertEqual(12, summary["bytes_max"])

    def test_summarize_wave_counts_distribution(self):
        rows = [
            {"status": 200, "wall_ms": 7000, "instance_id": "a", "instance_age_s": 100.0},
            {"status": 200, "wall_ms": 12000, "instance_id": "a", "instance_age_s": 100.0},
            {"status": 200, "wall_ms": 13000, "instance_id": "b", "instance_age_s": 4.0},
            {"status": 503, "wall_ms": 20000, "instance_id": None, "instance_age_s": None},
            {"status": 504, "wall_ms": 60000, "instance_id": None, "instance_age_s": None},
        ]
        summary = summarize_wave(rows)
        self.assertEqual({"requests": 5, "ok": 3, "busy": 1, "failed_other": 1, "p50_ms": 12000, "p95_ms": 13000, "max_ms": 13000, "instances": 2, "max_per_instance": 2, "cold": 1}, summary)


class TestComplexitySynthetic(unittest.TestCase):
    def test_numbers_only_every_fifth_line_with_one_running_counter(self):
        number_line = re.compile(r"^(\d+)\. ")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_synthetic(Path(temp_dir) / "synthetic.pdf", words_per_page=500, drawings_per_page=0)
            all_numbers: list[int] = []
            with fitz.open(path) as doc:
                self.assertEqual(4, doc.page_count)
                for page in doc:
                    lines = page.get_text("text").splitlines()
                    matches = [number_line.match(line) for line in lines]
                    numbered = [match.group(1) for match in matches if match]
                    self.assertGreaterEqual(len(numbered), 5)
                    self.assertLessEqual(len(numbered), 12)
                    all_numbers.extend(int(value) for value in numbered)

        for previous, current in zip(all_numbers, all_numbers[1:]):
            self.assertLess(previous, current, "numbers must strictly increase across the whole document")


class TestMemoryBench(unittest.TestCase):
    def test_write_2xa3_uses_the_real_trial_page_count(self):
        # The trial's own page cap (trial_input.DEFAULT_MAX_PAGES) went from 3
        # to 4; the synthetic case must track it, not a number frozen in the
        # bench, or it stops matching what the trial server actually parses.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = memory.write_2xa3(Path(temp_dir) / "2xa3.pdf")
            with fitz.open(path) as doc:
                self.assertEqual(DEFAULT_MAX_PAGES, doc.page_count)

    def test_run_two_overlapping_parses_reports_pages_and_peak_after_both_finish(self):
        # An ordered event log (not a bare call count) proves *when* the RSS
        # read happens: a mutant that reads max_rss_mb() before either parse
        # runs would still satisfy a call-count-only assertion but would
        # report the child's import-time baseline instead of the peak during
        # the two parses.
        events = []

        def _fake_parse_in_scratch(pdf, parse, **kwargs):
            events.append("parse")
            return _result()  # 1 page, timing_ms={"total": 7}

        def _fake_max_rss_mb():
            events.append("rss")
            return 42.5

        with patch.object(memory, "parse_in_scratch", side_effect=_fake_parse_in_scratch), \
                patch.object(memory, "max_rss_mb", side_effect=_fake_max_rss_mb):
            payload = memory.run_two_overlapping_parses(Path("dummy.pdf"))

        self.assertEqual({"pages": 1, "rss_peak_mb": 42.5, "total_ms_a": 7, "total_ms_b": 7}, payload)
        self.assertEqual(["parse", "parse", "rss"], events, "must read peak RSS once, after both parses finish")

    def test_measure_case_runs_a_fresh_subprocess_and_never_parses_in_the_parent(self):
        # This is the bug fix: the parent must not call parse_in_scratch (or
        # read its own rss) to build a row. If it did, this file's peak could
        # inherit whatever a previous case already pushed ru_maxrss to, which
        # is exactly the process-lifetime-high-water-mark bug being fixed.
        fake_payload = {"pages": 4, "rss_peak_mb": 199.0, "total_ms_a": 1000, "total_ms_b": 1100}
        fake_completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(fake_payload) + "\n", stderr="")
        with patch.object(memory, "parse_in_scratch", side_effect=AssertionError("must not parse in the parent process")), \
                patch.object(memory.subprocess, "run", return_value=fake_completed) as run_mock:
            result = memory.measure_case(Path("/tmp/x.pdf"))

        self.assertEqual(fake_payload, result)
        command = run_mock.call_args.args[0]
        self.assertEqual(sys.executable, command[0])
        self.assertEqual(str(memory.SCRIPT_PATH), command[1], "child must be told to run this same script")
        self.assertTrue(memory.SCRIPT_PATH.is_file())
        self.assertIn("--worker", command)
        self.assertEqual("/tmp/x.pdf", command[-1])
        self.assertTrue(run_mock.call_args.kwargs.get("check"))

    def test_measure_case_returns_an_error_dict_instead_of_raising_when_the_child_fails(self):
        # A failed child used to re-raise CalledProcessError straight out of
        # measure_case, into main()'s bare `for pdf in pdfs` loop -- one bad
        # case would abort the whole run and discard every row already
        # measured before it, printing no table at all.
        failure = subprocess.CalledProcessError(returncode=1, cmd=["x"], output="", stderr="boom: worker crashed")
        with patch.object(memory.subprocess, "run", side_effect=failure):
            result = memory.measure_case(Path("/tmp/bad.pdf"))
        self.assertIn("error", result)
        self.assertIn("boom: worker crashed", result["error"])

    def test_measure_case_returns_an_error_dict_when_the_child_exits_0_with_no_output(self):
        # A second, distinct failure mode from the CalledProcessError above:
        # the child exits 0 but prints nothing, so
        # proc.stdout.strip().splitlines()[-1] used to raise IndexError
        # instead of CalledProcessError -- an unhandled exception either way.
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(memory.subprocess, "run", return_value=completed):
            result = memory.measure_case(Path("/tmp/empty.pdf"))
        self.assertIn("error", result)

    def test_measure_case_returns_an_error_dict_when_the_last_line_is_not_json(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json\n", stderr="")
        with patch.object(memory.subprocess, "run", return_value=completed):
            result = memory.measure_case(Path("/tmp/garbled.pdf"))
        self.assertIn("error", result)

    def test_main_reports_a_failed_case_as_a_row_and_keeps_measuring_the_rest(self):
        good_payload = {"pages": 4, "rss_peak_mb": 900.0, "total_ms_a": 1, "total_ms_b": 2}

        def fake_measure_case(pdf: Path) -> dict:
            if pdf.name == "bad.pdf":
                return {"error": "boom: worker crashed"}
            return good_payload

        with patch.object(memory, "measure_case", side_effect=fake_measure_case):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                exit_code = memory.main(["bad.pdf", "good.pdf"])

        self.assertEqual(1, exit_code)
        text = out.getvalue()
        # good.pdf's row must survive being measured after the failed case,
        # not be discarded along with it.
        self.assertIn("| good.pdf | 4 | 900.0 | 1 | 2 |", text)
        self.assertIn("bad.pdf", text)
        self.assertIn("ERROR", text)
        self.assertIn("boom: worker crashed", err.getvalue())

    def test_main_reports_each_rows_own_isolated_peak_not_a_running_maximum(self):
        # A smaller later file reporting a *smaller* peak than an earlier,
        # bigger one is only possible once each row comes from its own fresh
        # process; the old cumulative-rss bug could only ever grow.
        payloads = [
            {"pages": 4, "rss_peak_mb": 900.0, "total_ms_a": 1, "total_ms_b": 2},
            {"pages": 1, "rss_peak_mb": 61.0, "total_ms_a": 3, "total_ms_b": 4},
        ]
        completed = [subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload)) for payload in payloads]
        with patch.object(memory.subprocess, "run", side_effect=completed) as run_mock:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                exit_code = memory.main(["big.pdf", "small.pdf"])
        text = out.getvalue()

        self.assertEqual(0, exit_code)
        # Each row's own file, in order, must reach its own child -- a mutant
        # that measures the same (e.g. first) file for every row would still
        # pass if the test only checked stdout against the mocked payloads.
        self.assertEqual(
            ["big.pdf", "small.pdf"],
            [call.args[0][-1] for call in run_mock.call_args_list],
        )
        self.assertIn("| big.pdf | 4 | 900.0 | 1 | 2 |", text)
        self.assertIn("| small.pdf | 1 | 61.0 | 3 | 4 |", text)
        self.assertNotIn("rss_before", text)
        self.assertNotIn("cumulative", text)

    def test_worker_flag_prints_the_measured_payload_as_the_last_json_line(self):
        with patch.object(memory, "parse_in_scratch", return_value=_result()), \
                patch.object(memory, "max_rss_mb", return_value=12.3):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                exit_code = memory.main(["--worker", "input.pdf"])

        self.assertEqual(0, exit_code)
        payload = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual({"pages": 1, "rss_peak_mb": 12.3, "total_ms_a": 7, "total_ms_b": 7}, payload)


if __name__ == "__main__":
    unittest.main()
