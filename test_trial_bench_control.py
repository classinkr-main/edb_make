"""Tests for scripts/trial_bench/control.py -- the live positive control for
oracle.py's repair-change detector.

These tests cover the deterministic, no-AI half (building an image-only PDF,
and the run_control glue around the parser boundary) with a faked
parse_problems, the same pattern test_trial_bench.py uses for oracle_case.
The live-Gemini half (does the counter actually fire on a real response) is
not something a unit test can prove or should depend on -- it is exercised
by actually running scripts/trial_bench/control.py against a real corpus
page and reported separately.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image

from problem_parser import ParsedPage, ParseResult
from scripts.trial_bench import control, oracle


def _write_pdf(path: Path, page_count: int = 2, size: tuple[int, int] = (600, 800)) -> Path:
    doc = fitz.open()
    for index in range(page_count):
        page = doc.new_page(width=size[0], height=size[1])
        page.insert_text((40, 80), f"{index + 1}. problem stem text, enough words to count as content", fontsize=12)
        page.insert_text((40, 300), "① a   ② b   ③ c   ④ d   ⑤ e", fontsize=12)
        # Distinguishable geometry per page: a filled black square whose
        # position depends on the page index. The two pages' text differs
        # only in an ordinal number a pixel sample can't read, so without
        # this marker a test cannot tell which source page a rendered
        # pixmap actually came from (see
        # test_renders_the_requested_page_not_some_other_page below).
        marker = fitz.Rect(20, 500 + index * 120, 80, 560 + index * 120)
        page.draw_rect(marker, color=(0, 0, 0), fill=(0, 0, 0))
    doc.save(path)
    doc.close()
    return path


def _marker_pixel(pixmap, dpi: int, page_index: int) -> tuple[int, int, int]:
    """Sample the center of page ``page_index``'s marker square (see
    ``_write_pdf``) in a pixmap rendered at ``dpi``, converting the marker's
    point coordinates to pixel coordinates the same way PyMuPDF does."""
    x = int(50 * dpi / 72)
    y = int((530 + page_index * 120) * dpi / 72)
    return pixmap.pixel(x, y)


class TestBuildControlPdf(unittest.TestCase):
    def test_produces_a_single_page_image_only_pdf_at_the_requested_dpi(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            source = _write_pdf(Path(temp_dir) / "source.pdf", page_count=2, size=(600, 800))
            target = control.build_control_pdf(source, 1, dpi=200, root=root)

            self.assertEqual(root / "inputs" / "source-p2-image-only.pdf", target)
            with fitz.open(target) as doc:
                self.assertEqual(1, doc.page_count)
                page = doc[0]
                # No text layer at all -- the whole point of the control: the
                # trial's text-marker segmenter has nothing to read here.
                self.assertEqual("", page.get_text().strip())
                # Exactly one embedded raster image, no vector drawings.
                self.assertEqual(1, len(page.get_images()))
                self.assertEqual(0, len(page.get_drawings()))
                # Re-rendering this control page at the same dpi reproduces
                # the pixel size the source page would have rendered to,
                # within a pixel of rounding either library does converting
                # pt <-> px.
                pixmap = page.get_pixmap(dpi=200)
                self.assertAlmostEqual(600 * 200 / 72, pixmap.width, delta=1)
                self.assertAlmostEqual(800 * 200 / 72, pixmap.height, delta=1)
                # This is page 1 (0-based index 1) of a 2-page source: its
                # own marker must be present, and page 0's marker (a
                # different position the two pages' text alone can't tell
                # apart) must not be -- a regression that renders the wrong
                # source page (e.g. always page 0) would pass every
                # assertion above (both pages share the same size) but fail
                # here.
                self.assertEqual((0, 0, 0), _marker_pixel(pixmap, 200, page_index=1))
                self.assertEqual((255, 255, 255), _marker_pixel(pixmap, 200, page_index=0))

    def test_renders_the_requested_page_not_some_other_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            source = _write_pdf(Path(temp_dir) / "source.pdf", page_count=2, size=(600, 800))
            target = control.build_control_pdf(source, 0, dpi=200, root=root)
            with fitz.open(target) as doc:
                pixmap = doc[0].get_pixmap(dpi=200)
                self.assertEqual((0, 0, 0), _marker_pixel(pixmap, 200, page_index=0))
                self.assertEqual((255, 255, 255), _marker_pixel(pixmap, 200, page_index=1))

    def test_out_of_range_page_index_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            source = _write_pdf(Path(temp_dir) / "source.pdf", page_count=1)
            with self.assertRaises(ValueError):
                control.build_control_pdf(source, 5, root=root)

    def test_name_overrides_the_derived_case_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            source = _write_pdf(Path(temp_dir) / "source.pdf", page_count=1)
            target = control.build_control_pdf(source, 0, root=root, name="my-control-case")
            self.assertEqual(root / "inputs" / "my-control-case.pdf", target)

    def test_root_is_required_with_no_default(self):
        # A stray default here (e.g. BENCH_ROOT, the corpus root) would let
        # a caller silently write a control PDF into the corpus's own
        # inputs/, where observe.select_inputs would pick it up as a real
        # case -- see the module docstring. Keyword-required with no
        # default makes that mistake impossible rather than merely
        # documented.
        source = Path("unused.pdf")
        with self.assertRaises(TypeError):
            control.build_control_pdf(source, 0)


class TestRunControl(unittest.TestCase):
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

    def test_saves_observation_and_returns_the_raw_per_page_counters(self):
        entry = {
            "attempted": True,
            "applied": True,
            "changed": True,
            "status": "applied",
            "model_used": "gemini-3.1-pro-preview",
            "blocks_changed": 3,
            "problems_regrouped": True,
            "titles_changed": 2,
            "boxes_overridden": 1,
            "problem_metadata_changed": 0,
            "baseline_block_count": 6,
            "baseline_problem_count": 4,
        }
        fake_result = self._fake_result([entry])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            pdf_path = Path(temp_dir) / "control-case.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 placeholder, never read by the fake parser")
            with patch.object(control, "parse_problems", return_value=fake_result):
                result = control.run_control(pdf_path, "social", root=root)
            saved = json.loads((root / "oracle" / "control-case.json").read_text(encoding="utf-8"))

        self.assertEqual([entry], result["pages"])
        expected_summary = oracle.summarize_page_repair([entry])
        self.assertEqual(1, expected_summary["pages_changed"])
        self.assertEqual(expected_summary, result["observation"]["oracle"]["page_repair"])
        self.assertEqual(expected_summary, saved["oracle"]["page_repair"])
        # The raw per-page counters (blocks_changed, titles_changed, the
        # pre-repair baseline_block_count/baseline_problem_count, ...) must
        # survive on disk too -- summarize_page_repair's aggregate above
        # drops all of them, so without this the only place they ever
        # existed was this process's stdout.
        self.assertEqual([entry], saved["oracle"]["page_repair_pages"])

    def test_run_control_root_is_required_with_no_default(self):
        with self.assertRaises(TypeError):
            control.run_control(Path("unused.pdf"), "social")

    def test_forwards_subject_ocr_mode_and_forced_ai_config_to_the_parser(self):
        fake_result = self._fake_result([])
        captured: dict = {}

        def recorder(*args, **kwargs):
            captured.update(kwargs)
            return fake_result

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            pdf_path = Path(temp_dir) / "control-case.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 placeholder, never read by the fake parser")
            with patch.object(control, "parse_problems", side_effect=recorder):
                control.run_control(pdf_path, "social", ocr_mode="auto", model="gemini-3.6-flash", root=root)

        self.assertEqual("social", captured.get("subject"))
        self.assertEqual("auto", captured.get("ocr_mode"))
        self.assertEqual(control.control_force_config("gemini-3.6-flash"), captured.get("ai_fallback_config"))

    def _invalid_page(self) -> dict:
        return {
            "attempted": True,
            "applied": False,
            "changed": False,
            "status": "invalid_response",
            "error": "problem start and choice block ids overlap",
            "blocks_changed": 0,
            "problems_regrouped": False,
            "titles_changed": 0,
            "boxes_overridden": 0,
            "problem_metadata_changed": 0,
            "baseline_block_count": 6,
            "baseline_problem_count": 4,
        }

    def _applied_page(self) -> dict:
        return {
            "attempted": True,
            "applied": True,
            "changed": True,
            "status": "applied",
            "model_used": "gemini-3.1-pro-preview",
            "blocks_changed": 0,
            "problems_regrouped": False,
            "titles_changed": 5,
            "boxes_overridden": 0,
            "problem_metadata_changed": 0,
            "baseline_block_count": 6,
            "baseline_problem_count": 4,
        }

    def test_retries_until_a_response_validates_and_saves_that_attempt(self):
        # The reason --attempts exists: the same command on the same input
        # produced `applied` on some runs and `invalid_response` on others,
        # so a single run is not a repeatable experiment. Without the loop,
        # a reader following the documented command sees a failure and has
        # no way to reach the positive.
        invalid, applied = self._invalid_page(), self._applied_page()
        results = [self._fake_result([invalid]), self._fake_result([invalid]), self._fake_result([applied])]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            pdf_path = Path(temp_dir) / "control-case.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 placeholder, never read by the fake parser")
            buffer = io.StringIO()
            with patch.object(control, "parse_problems", side_effect=results) as parse_mock, \
                    contextlib.redirect_stdout(buffer):
                result = control.run_control(pdf_path, "math", root=root, attempts=5)
            saved = json.loads((root / "oracle" / "control-case.json").read_text(encoding="utf-8"))

        # Stops at the first validated response rather than spending the
        # whole budget, and the observation saved is that attempt's -- not
        # the last invalid one.
        self.assertEqual(3, parse_mock.call_count)
        self.assertEqual([applied], result["pages"])
        self.assertEqual([applied], saved["oracle"]["page_repair_pages"])
        self.assertEqual(1, saved["oracle"]["page_repair"]["pages_applied"])
        # How many tries that positive took is part of the evidence.
        self.assertEqual([1, 2, 3], [entry["attempt"] for entry in saved["oracle"]["attempts"]])
        self.assertEqual(
            [{"invalid_response": 1}, {"invalid_response": 1}, {"applied": 1}],
            [entry["statuses"] for entry in saved["oracle"]["attempts"]],
        )
        self.assertIn("problem start and choice block ids overlap", saved["oracle"]["attempts"][0]["errors"])
        self.assertIn("attempt 1/5", buffer.getvalue())
        self.assertIn("attempt 3/5", buffer.getvalue())

    def test_spends_the_whole_budget_then_saves_the_last_attempt(self):
        invalid = self._invalid_page()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            pdf_path = Path(temp_dir) / "control-case.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 placeholder, never read by the fake parser")
            with patch.object(control, "parse_problems", return_value=self._fake_result([invalid])) as parse_mock, \
                    contextlib.redirect_stdout(io.StringIO()):
                control.run_control(pdf_path, "math", root=root, attempts=3)
            saved = json.loads((root / "oracle" / "control-case.json").read_text(encoding="utf-8"))

        self.assertEqual(3, parse_mock.call_count)
        # A run that never validated still records a full observation --
        # "the model answered and was rejected three times" is itself a
        # result, and must not be silently dropped.
        self.assertEqual([invalid], saved["oracle"]["page_repair_pages"])
        self.assertEqual(3, len(saved["oracle"]["attempts"]))
        self.assertEqual({}, saved["oracle"]["repair_payloads"])

    def test_default_is_a_single_attempt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            pdf_path = Path(temp_dir) / "control-case.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 placeholder, never read by the fake parser")
            with patch.object(control, "parse_problems", return_value=self._fake_result([self._invalid_page()])) as parse_mock, \
                    contextlib.redirect_stdout(io.StringIO()):
                control.run_control(pdf_path, "math", root=root)
        self.assertEqual(1, parse_mock.call_count)

    def test_saves_the_raw_validated_payload_before_the_scratch_dir_is_deleted(self):
        # page_repair.py writes the raw answer into the parse scratch
        # directory; common.parse_in_scratch would delete that directory on
        # the way out, taking the only on-disk copy with it. This asserts
        # the harvest happens inside that directory's lifetime.
        payload = {
            "problem_start_block_ids": ["control-page-001-block-001"],
            "choice_block_ids": ["control-page-001-block-002"],
            "figure_block_ids": [],
            "display_titles": [{"block_id": "control-page-001-block-001", "title": "1."}],
            "notes": [],
        }
        applied = self._applied_page()

        def parser(source, *, work_dir, **kwargs):
            ai_debug = Path(work_dir) / ".pipeline_cache" / "ai_debug"
            ai_debug.mkdir(parents=True, exist_ok=True)
            (ai_debug / "control-page-001_repair.json").write_text(
                json.dumps({"summary": {"status": "applied"}, "repair_payload": payload}, ensure_ascii=False),
                encoding="utf-8",
            )
            return self._fake_result([applied])

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            pdf_path = Path(temp_dir) / "control-case.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 placeholder, never read by the fake parser")
            with patch.object(control, "parse_problems", side_effect=parser), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = control.run_control(pdf_path, "math", root=root)
            saved = json.loads((root / "oracle" / "control-case.json").read_text(encoding="utf-8"))

        self.assertEqual({"control-page-001": payload}, result["payloads"])
        self.assertEqual({"control-page-001": payload}, saved["oracle"]["repair_payloads"])
        # The specific question the counters alone cannot answer: which
        # titles the model sent, so they can be compared with what the
        # baseline was using.
        self.assertEqual(
            [{"block_id": "control-page-001-block-001", "title": "1."}],
            saved["oracle"]["repair_payloads"]["control-page-001"]["display_titles"],
        )

    def test_saves_a_failure_record_and_reraises_when_parsing_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            pdf_path = Path(temp_dir) / "control-case.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 placeholder, never read by the fake parser")
            with patch.object(control, "parse_problems", side_effect=RuntimeError("Gemini exploded")):
                with self.assertRaisesRegex(RuntimeError, "Gemini exploded"):
                    control.run_control(pdf_path, "social", root=root)
            saved = json.loads((root / oracle.FAILURE_DIR / "control-case.json").read_text(encoding="utf-8"))

        page_repair = saved["oracle"]["page_repair"]
        self.assertEqual("oracle_failed", page_repair["status"])
        self.assertTrue(page_repair["no_page_records"])
        self.assertIn("Gemini exploded", saved["error"])
        # A failure must not clobber a previous run's oracle/<case>.json --
        # same guarantee oracle.oracle_case gives (see
        # test_a_failed_case_does_not_clobber_its_previous_successful_observation).
        self.assertFalse((root / "oracle" / "control-case.json").is_file())


class TestControlForceConfig(unittest.TestCase):
    def test_adds_save_debug_on_top_of_the_corpus_oracle_config(self):
        # save_debug is the only thing that ever writes the raw model answer
        # (problem_start_block_ids / display_titles / ...) to disk; without
        # it the control's counters can only be re-checked by paying for
        # another non-deterministic Gemini call.
        self.assertEqual({**oracle.force_config("m"), "save_debug": True}, control.control_force_config("m"))
        self.assertTrue(control.control_force_config()["save_debug"])

    def test_leaves_the_corpus_oracle_config_untouched(self):
        # The corpus oracle runs over real exam pages and its debug artifact
        # would contain their raw text; this knob is control-only.
        self.assertFalse(oracle.force_config().get("save_debug", False))


class TestCollectRepairPayloads(unittest.TestCase):
    def _write_debug(self, ai_debug: Path, page_id: str, payload) -> None:
        ai_debug.mkdir(parents=True, exist_ok=True)
        (ai_debug / f"{page_id}_repair.json").write_text(
            json.dumps({"summary": {"status": "applied"}, "repair_payload": payload}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_returns_the_raw_payload_keyed_by_page_id(self):
        payload = {
            "problem_start_block_ids": ["page-001-block-001"],
            "choice_block_ids": [],
            "figure_block_ids": [],
            "display_titles": [{"block_id": "page-001-block-001", "title": "1."}],
            "notes": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            scratch = Path(temp_dir)
            self._write_debug(scratch / "work" / ".pipeline_cache" / "ai_debug", "case-page-001", payload)
            self.assertEqual({"case-page-001": payload}, control.collect_repair_payloads(scratch))

    def test_ignores_files_outside_ai_debug_and_unreadable_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            scratch = Path(temp_dir)
            # A same-named file somewhere else in the scratch tree must not
            # be mistaken for a repair artifact.
            decoy = scratch / "work" / "elsewhere"
            decoy.mkdir(parents=True)
            (decoy / "case-page-002_repair.json").write_text('{"repair_payload": {"x": 1}}', encoding="utf-8")
            ai_debug = scratch / "work" / ".pipeline_cache" / "ai_debug"
            ai_debug.mkdir(parents=True)
            (ai_debug / "broken-page-001_repair.json").write_text("not json at all", encoding="utf-8")
            self._write_debug(ai_debug, "good-page-001", {"display_titles": []})

            self.assertEqual({"good-page-001": {"display_titles": []}}, control.collect_repair_payloads(scratch))

    def test_no_artifacts_is_an_empty_mapping_not_an_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual({}, control.collect_repair_payloads(Path(temp_dir)))


class TestMain(unittest.TestCase):
    """Coverage for control.main(): the actual deliverable a user runs.

    Every case here patches ``control.BENCH_ROOT`` to a temp directory
    (main() reads that module global at call time, computing
    ``control_root = bench_dir(CONTROL_DIR, BENCH_ROOT)``) so a bug that
    reintroduces a corpus-root default, drops the API-key guard, or renders
    an empty table is caught without touching the real bench.
    """

    def test_run_prints_the_per_page_counter_table(self):
        fake_pages = [
            {
                "status": "applied",
                "attempted": True,
                "applied": True,
                "changed": True,
                "model_used": "gemini-3.1-pro-preview",
                "blocks_changed": 0,
                "problems_regrouped": False,
                "titles_changed": 6,
                "boxes_overridden": 0,
                "problem_metadata_changed": 0,
                "baseline_block_count": 6,
                "baseline_problem_count": 4,
                "error": "",
            }
        ]
        fake_result = {
            "observation": {"oracle": {"page_repair": oracle.summarize_page_repair(fake_pages)}},
            "pages": fake_pages,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            source = _write_pdf(Path(temp_dir) / "source.pdf", page_count=1)
            runtime_dir = Path(temp_dir) / "runtime"
            runtime_dir.mkdir()
            buffer = io.StringIO()
            with patch.object(control, "BENCH_ROOT", root), \
                    patch.object(control, "run_control", return_value=fake_result) as run_mock, \
                    patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                    contextlib.redirect_stdout(buffer):
                exit_code = control.main(
                    [
                        "run",
                        "--runtime-dir", str(runtime_dir),
                        "--source", str(source),
                        "--page", "0",
                        "--subject", "math",
                    ]
                )
        output = buffer.getvalue()
        self.assertEqual(0, exit_code)
        run_mock.assert_called_once()
        for header in (
            "baseline_block_count", "baseline_problem_count", "changed", "blocks_changed",
            "problems_regrouped", "titles_changed", "boxes_overridden", "problem_metadata_changed",
        ):
            self.assertIn(header, output)
        for value in ("applied", "gemini-3.1-pro-preview", "6", "4", "True"):
            self.assertIn(value, output)
        # This is the exact live-run counter row docs/web-trial-quality.md's
        # 2026-09-17 section cites -- proves the table would actually have
        # shown blocks_changed=0/titles_changed=6, not a blank or mislabeled
        # row, had this table existed when that run happened.
        self.assertIn("| 0 | applied | 6 | 4 | True | 0 | False | 6 | 0 | 0 |", output)

    def test_attempts_is_forwarded_to_run_control(self):
        fake_result = {
            "observation": {"oracle": {"page_repair": oracle.summarize_page_repair([])}},
            "pages": [],
            "payloads": {},
            "attempts": [{"attempt": 1, "statuses": {"invalid_response": 1}, "pages_applied": 0, "pages_changed": 0, "errors": []}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            source = _write_pdf(Path(temp_dir) / "source.pdf", page_count=1)
            runtime_dir = Path(temp_dir) / "runtime"
            runtime_dir.mkdir()
            buffer = io.StringIO()
            with patch.object(control, "BENCH_ROOT", root), \
                    patch.object(control, "run_control", return_value=fake_result) as run_mock, \
                    patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                    contextlib.redirect_stdout(buffer):
                exit_code = control.main(
                    [
                        "run",
                        "--runtime-dir", str(runtime_dir),
                        "--source", str(source),
                        "--page", "0",
                        "--subject", "math",
                        "--attempts", "6",
                    ]
                )
        self.assertEqual(0, exit_code)
        self.assertEqual(6, run_mock.call_args.kwargs["attempts"])
        # A run that produced no validated payload must say so rather than
        # leaving the reader to assume one was recorded.
        self.assertIn("no validated repair payload this run", buffer.getvalue())

    def test_build_writes_under_control_inputs_never_corpus_inputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            source = _write_pdf(Path(temp_dir) / "source.pdf", page_count=1)
            with patch.object(control, "BENCH_ROOT", root):
                exit_code = control.main(["build", "--source", str(source), "--page", "0"])

            self.assertEqual(0, exit_code)
            control_inputs = root / "control" / "inputs"
            self.assertTrue(control_inputs.is_dir())
            self.assertEqual(1, len(list(control_inputs.iterdir())))
            # The corpus root's own inputs/ (what observe.select_inputs globs
            # for real cases) must never come into existence at all.
            self.assertFalse((root / "inputs").exists())

    def test_run_without_api_key_returns_2_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            source = _write_pdf(Path(temp_dir) / "source.pdf", page_count=1)
            runtime_dir = Path(temp_dir) / "runtime"
            runtime_dir.mkdir()
            with patch.object(control, "BENCH_ROOT", root), \
                    patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
                exit_code = control.main(
                    [
                        "run",
                        "--runtime-dir", str(runtime_dir),
                        "--source", str(source),
                        "--page", "0",
                    ]
                )

            self.assertEqual(2, exit_code)
            # build_control_pdf and run_control must never have run.
            self.assertFalse((root / "control" / "inputs").exists())
            self.assertFalse((root / "control" / "oracle").exists())


if __name__ == "__main__":
    unittest.main()
