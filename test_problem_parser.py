import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fitz
from PIL import Image

from assemble_page import group_problem_units
from build_problem_board_edb import build_problem_entries
from layout_template_schema import LayoutTemplate
from preprocess import PreparedPage
from problem_parser import PDF_RENDER_DPI, PdfUnreadableError, inspect_pdf, parse_problems
from structured_schema import BlockType, Box, ContentBlock, PageModel, ProblemUnit, Subject


def _block(block_id: str, block_type: BlockType, top: float, text: str | None, *, height: float = 80.0) -> ContentBlock:
    return ContentBlock(
        block_id=block_id,
        block_type=block_type,
        bbox=Box(left=40.0, top=top, width=760.0, height=height),
        reading_order=int(top),
        text=text,
    )


def _cross_page_passage_fixture(root: Path) -> tuple[list[PreparedPage], list[PageModel]]:
    root.mkdir(parents=True, exist_ok=True)
    page_1_path = root / "page-1.png"
    page_2_path = root / "page-2.png"
    for path in (page_1_path, page_2_path):
        Image.new("RGB", (900, 1400), "white").save(path)
    prepared_pages = [
        PreparedPage(
            page_id="korean-cross-001",
            source_path=str(page_1_path),
            page_number=1,
            image=Image.open(page_1_path).convert("RGB"),
            original_size=(900, 1400),
        ),
        PreparedPage(
            page_id="korean-cross-002",
            source_path=str(page_2_path),
            page_number=2,
            image=Image.open(page_2_path).convert("RGB"),
            original_size=(900, 1400),
        ),
    ]
    page_1 = PageModel(
        page_id="korean-cross-001",
        width_px=900,
        height_px=1400,
        subject=Subject.KOREAN,
        source_path=str(page_1_path),
        blocks=[
            _block("range-18-21", BlockType.STEM, 40, "[18~21] 다음 글을 읽고 물음에 답하시오."),
            _block("shared-passage-a", BlockType.STEM, 140, "긴 지문의 첫 페이지 내용이다.", height=520),
        ],
        problems=[
            ProblemUnit(
                unit_id="korean-cross-001-passage-fragment",
                subject=Subject.KOREAN,
                title="지문 18~21",
                stem_block_ids=["range-18-21", "shared-passage-a"],
                metadata={
                    "passage_group_id": "korean-cross-001-passage-18-21",
                    "passage_range": {"start": 18, "end": 21},
                    "passage_role": "passage_fragment",
                    "passage_child_problem_numbers": [18, 19, 20, 21],
                    "supplemental_item": True,
                },
            )
        ],
    )
    page_2 = group_problem_units(
        PageModel(
            page_id="korean-cross-002",
            width_px=900,
            height_px=1400,
            subject=Subject.KOREAN,
            source_path=str(page_2_path),
            blocks=[
                _block("shared-passage-b", BlockType.STEM, 40, "앞 페이지에서 이어지는 긴 지문 내용이다.", height=420),
                _block("q18", BlockType.STEM, 520, "18. 윗글의 내용으로 적절한 것은?"),
                _block("q19", BlockType.STEM, 680, "19. 윗글의 서술 방식으로 적절한 것은?"),
                _block("q20", BlockType.STEM, 840, "20. 윗글을 바탕으로 추론한 내용은?"),
                _block("q21", BlockType.STEM, 1000, "21. 윗글의 핵심 내용은?"),
            ],
        )
    )
    return prepared_pages, [page_1, page_2]


def _write_text_exam_pdf(path: Path, pages: list[list[int]], *, width: float = 600, height: float = 800) -> Path:
    doc = fitz.open()
    for numbers in pages:
        page = doc.new_page(width=width, height=height)
        slots = ((60, 120), (60, 430), (330, 120), (330, 430))
        for number, (x, y) in zip(numbers, slots):
            page.insert_text((x, y), f"{number}. problem stem", fontsize=14)
            page.draw_rect(fitz.Rect(x + 35, y + 50, x + 180, y + 140), color=(0, 0, 0), width=1)
            page.insert_text((x, y + 210), "① a   ② b   ③ c", fontsize=12)
    doc.save(path)
    doc.close()
    return path


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (400, 500), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class TestRenderBoardAssetsSwitch(unittest.TestCase):
    def test_disabling_board_assets_keeps_crops_and_skips_cutouts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with_board_pages, with_board_models = _cross_page_passage_fixture(root / "with")
            without_board_pages, without_board_models = _cross_page_passage_fixture(root / "without")

            with_board = build_problem_entries(
                with_board_pages,
                with_board_models,
                root / "with" / "out",
                LayoutTemplate(name="academy-default"),
            )
            without_board = build_problem_entries(
                without_board_pages,
                without_board_models,
                root / "without" / "out",
                LayoutTemplate(name="academy-default"),
                render_board_assets=False,
            )

            self.assertEqual(
                [entry.problem_id for entry in with_board],
                [entry.problem_id for entry in without_board],
            )
            self.assertTrue((root / "with" / "out" / "problem_cutouts").is_dir())
            self.assertFalse((root / "without" / "out" / "problem_cutouts").exists())
            for kept, lean in zip(with_board, without_board):
                self.assertTrue(lean.crop_path.is_file())
                self.assertFalse(lean.board_render_path.exists())
                self.assertEqual(kept.crop_path.read_bytes(), lean.crop_path.read_bytes())


class TestInspectPdf(unittest.TestCase):
    def test_text_pdf_reports_pages_and_no_textless_pages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_text_exam_pdf(Path(temp_dir) / "exam.pdf", [[1, 2, 3, 4], [5, 6]])
            info = inspect_pdf(path, max_pages=3)
        self.assertEqual(2, info.page_count)
        self.assertEqual(0, info.pages_without_text)
        self.assertAlmostEqual(600 * 800, info.max_page_area_pt)

    def test_image_only_page_counts_as_textless(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_text_exam_pdf(Path(temp_dir) / "mixed.pdf", [[1, 2, 3, 4]])
            doc = fitz.open(path)
            scanned = doc.new_page(width=600, height=800)
            scanned.insert_image(scanned.rect, stream=_png_bytes())
            doc.saveIncr()
            doc.close()
            info = inspect_pdf(path, max_pages=3)
        self.assertEqual(2, info.page_count)
        self.assertEqual(1, info.pages_without_text)

    def test_scans_only_leading_pages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_text_exam_pdf(Path(temp_dir) / "long.pdf", [[1, 2], [3, 4], [5, 6]])
            doc = fitz.open(path)
            for _ in range(2):
                scanned = doc.new_page(width=1684, height=2384)
                scanned.insert_image(scanned.rect, stream=_png_bytes())
            doc.saveIncr()
            doc.close()
            info = inspect_pdf(path, max_pages=3)
        self.assertEqual(5, info.page_count)
        self.assertEqual(3, info.scanned_pages)
        self.assertEqual(0, info.pages_without_text)
        self.assertAlmostEqual(600 * 800, info.max_page_area_pt)

    def test_short_document_scans_every_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_text_exam_pdf(Path(temp_dir) / "short.pdf", [[1, 2]])
            info = inspect_pdf(path, max_pages=3)
        self.assertEqual(1, info.page_count)
        self.assertEqual(1, info.scanned_pages)

    def test_reports_largest_page_area(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_text_exam_pdf(Path(temp_dir) / "big.pdf", [[1, 2, 3, 4]], width=1684, height=2384)
            info = inspect_pdf(path, max_pages=3)
        self.assertAlmostEqual(1684 * 2384, info.max_page_area_pt)

    def test_non_pdf_bytes_raise_unreadable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "fake.pdf"
            path.write_bytes(_png_bytes())
            with self.assertRaises(PdfUnreadableError):
                inspect_pdf(path, max_pages=3)

    def test_encrypted_pdf_raises_unreadable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "locked.pdf"
            doc = fitz.open()
            doc.new_page()
            doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
            doc.close()
            with self.assertRaises(PdfUnreadableError):
                inspect_pdf(path, max_pages=3)


class TestParseProblems(unittest.TestCase):
    def test_text_pdf_yields_numbered_problems_inside_their_pages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2, 3, 4], [5, 6]])
            result = parse_problems(path, work_dir=root / "work")

            self.assertEqual(2, len(result.pages))
            self.assertEqual([1, 2, 3, 4, 5, 6], [problem.number for problem in result.problems])
            pages_by_id = {page.page_id: page for page in result.pages}
            for problem in result.problems:
                self.assertTrue(problem.regions)
                for region in problem.regions:
                    page = pages_by_id[region.page_id]
                    self.assertGreaterEqual(region.bbox.left, 0.0)
                    self.assertGreaterEqual(region.bbox.top, 0.0)
                    self.assertLessEqual(region.bbox.left + region.bbox.width, page.width + 1)
                    self.assertLessEqual(region.bbox.top + region.bbox.height, page.height + 1)
                self.assertEqual("RGB", problem.image.mode)
                self.assertGreater(problem.image.width, 0)
            for page in result.pages:
                self.assertEqual((page.width, page.height), page.image.size)
            self.assertFalse((root / "work" / "problem_cutouts").exists())
            self.assertIn("recognize", result.timing_ms)
            self.assertIn("crops", result.timing_ms)

    def test_images_survive_work_dir_removal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2]])
            with tempfile.TemporaryDirectory() as work_dir:
                result = parse_problems(path, work_dir=Path(work_dir))
            self.assertEqual(result.problems[0].image.size, result.problems[0].image.copy().size)
            self.assertIsNotNone(result.pages[0].image.getpixel((0, 0)))

    def test_max_pages_parses_only_leading_pages_from_a_compacted_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]])
            result = parse_problems(path, work_dir=root / "work", max_pages=3)
            trimmed = root / "work" / "leading-pages.pdf"

            self.assertEqual(5, result.source_page_count)
            self.assertEqual(3, len(result.pages))
            self.assertEqual([1, 2, 3, 4, 5, 6], [problem.number for problem in result.problems])
            self.assertTrue(trimmed.is_file())
            self.assertLess(trimmed.stat().st_size, path.stat().st_size)

    def test_without_max_pages_parses_whole_document(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2], [3, 4]])
            result = parse_problems(path, work_dir=root / "work")
            self.assertEqual(2, result.source_page_count)
            self.assertEqual(2, len(result.pages))
            self.assertFalse((root / "work" / "leading-pages.pdf").exists())

    def test_timing_has_stage_breakdown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2], [3, 4]])
            result = parse_problems(path, work_dir=root / "work")
        expected_keys = {"render", "segment", "recognize", "entries", "assets", "coalesce", "finish", "load", "crops", "total"}
        self.assertTrue(expected_keys <= set(result.timing_ms), result.timing_ms)
        for key in expected_keys:
            self.assertIsInstance(result.timing_ms[key], int)
            self.assertGreaterEqual(result.timing_ms[key], 0)
        self.assertLessEqual(result.timing_ms["render"] + result.timing_ms["segment"], result.timing_ms["recognize"] + 50)
        self.assertLessEqual(
            result.timing_ms["entries"] + result.timing_ms["assets"] + result.timing_ms["coalesce"] + result.timing_ms["finish"],
            result.timing_ms["crops"] + 50,
        )

    def test_render_timing_stops_before_segmentation(self):
        # Seam test for the render/segment boundary. A fake clock replaces
        # time.perf_counter, so the test cannot depend on machine speed: every
        # clock read advances 1 ms and the patched segmentation step jumps 5 s.
        # If the render stop-mark drifted past segmentation, "render" would
        # absorb the 5 s instead of "segment".
        import build_problem_board_edb as board

        class FakeClock:
            def __init__(self) -> None:
                self.now = 1000.0

            def __call__(self) -> float:
                self.now += 0.001
                return self.now

        clock = FakeClock()
        real_segment = board.build_page_models_for_prepared_pages

        def slow_segment(*args, **kwargs):
            clock.now += 5.0
            return real_segment(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2]])
            timings: dict[str, int] = {}
            with (
                mock.patch.object(board.time, "perf_counter", clock),
                mock.patch.object(board, "build_page_models_for_prepared_pages", slow_segment),
            ):
                board.build_pages(
                    path,
                    subject=board.resolve_subject("unknown"),
                    ocr_mode="none",
                    ai_fallback_config=None,
                    pdf_dpi=PDF_RENDER_DPI,
                    detect_perspective=False,
                    deskew=True,
                    crop_margins=True,
                    max_dimension=None,
                    timings=timings,
                )
        self.assertGreaterEqual(timings["segment"], 5000, timings)
        self.assertLess(timings["render"], 1000, timings)

    def test_assets_timing_covers_only_asset_rendering(self):
        # Same fake-clock seam test for build_problem_entries: only the
        # _render_problem_assets span may absorb the 5 s jump.
        import build_problem_board_edb as board

        class FakeClock:
            def __init__(self) -> None:
                self.now = 2000.0

            def __call__(self) -> float:
                self.now += 0.001
                return self.now

        clock = FakeClock()
        real_render = board._render_problem_assets

        def slow_render(tasks):
            clock.now += 5.0
            return real_render(tasks)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prepared_pages, page_models = _cross_page_passage_fixture(root)
            timings: dict[str, int] = {}
            with (
                mock.patch.object(board.time, "perf_counter", clock),
                mock.patch.object(board, "_render_problem_assets", slow_render),
            ):
                board.build_problem_entries(
                    prepared_pages,
                    page_models,
                    root / "out",
                    LayoutTemplate(name="academy-default"),
                    render_board_assets=False,
                    timings=timings,
                )
        self.assertGreaterEqual(timings["assets"], 5000, timings)
        for key in ("entries", "coalesce", "finish"):
            self.assertLess(timings[key], 1000, timings)


if __name__ == "__main__":
    unittest.main()
