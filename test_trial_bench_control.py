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

import json
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
    doc.save(path)
    doc.close()
    return path


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
        self.assertEqual(oracle.force_config("gemini-3.6-flash"), captured.get("ai_fallback_config"))

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


if __name__ == "__main__":
    unittest.main()
