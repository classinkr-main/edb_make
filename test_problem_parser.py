import io
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

import fitz
from PIL import Image

from assemble_page import group_problem_units
from build_problem_board_edb import build_problem_entries
from layout_template_schema import LayoutTemplate
from preprocess import PreparedPage
from problem_parser import (
    MAX_CONTENT_STREAM_RAW_BYTES_PER_PAGE,
    PDF_RENDER_DPI,
    PdfUnreadableError,
    inspect_pdf,
    parse_problems,
)
from structured_schema import BlockType, Box, ContentBlock, PageModel, ProblemUnit, Subject
from trial_input import A3_AREA_PT, InputLimits, TrialRejected, check_pdf_info


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


def _write_flate_bomb_pdf(path: Path) -> Path:
    """A one-page PDF that is tiny on disk but whose content stream inflates to 34 MB."""
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(0, 0, 1, 1))  # establishes a content-stream xref
    doc.update_stream(page.get_contents()[0], b"100 100 1 1 re S\n" * 2_000_000)
    doc.save(path, deflate=True)
    doc.close()
    return path


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

    def test_counts_words_and_drawings_per_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_text_exam_pdf(Path(temp_dir) / "exam.pdf", [[1, 2], [3]])
            doc = fitz.open(path)
            doc[0].draw_rect(fitz.Rect(10, 10, 100, 100), color=(0, 0, 0))
            doc.saveIncr()
            doc.close()
            info = inspect_pdf(path, max_pages=3)
        # Page 0 (numbers [1, 2] plus the extra draw_rect above) is the fuller page: 18 words,
        # 3 drawings. Page 1 (number [3] only) has 9 words, 1 drawing. Exact equality (rather
        # than >=) is required so a min/max swap or a "first/last page only" bug fails.
        self.assertEqual(18, info.max_words_per_page)
        self.assertEqual(3, info.max_drawings_per_page)

    def test_pathological_content_stream_short_circuits_drawing_scan(self):
        # A page whose content stream repeats a trivial path-painting operator two million
        # times makes get_text() and get_drawings() the expensive, unbounded operations that
        # max_words_per_page/max_drawings_per_page exist to bound -- get_drawings() alone
        # measured 10.3 s on this fixture. Flate-compressed the whole PDF is only ~83 KB, so
        # it sails through the upload size limit, and inspect_pdf runs before the parse slot
        # and the quota charge: paying either cost here would be a free, repeatable overload.
        # Proven structurally (both calls raise if invoked) rather than by timing, per this
        # project's rule against wall-clock-dependent tests.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_flate_bomb_pdf(Path(temp_dir) / "bomb.pdf")
            self.assertLess(path.stat().st_size, 4_000_000)  # passes the 4 MB upload limit

            def _fail_if_called(name):
                def _raise(*_args, **_kwargs):
                    raise AssertionError(f"{name} must not be called on a pathological content stream")

                return _raise

            with mock.patch.object(fitz.Page, "get_drawings", _fail_if_called("get_drawings()")):
                with mock.patch.object(fitz.Page, "get_text", _fail_if_called("get_text()")):
                    info = inspect_pdf(path, max_pages=3)
        self.assertGreater(info.max_drawings_per_page, 10_000)
        self.assertGreater(info.max_words_per_page, 10_000)

    def test_uncompressed_content_stream_is_rejected_without_decompressing(self):
        # The raw gate exists to bound how much the decompressed gate is ever willing to
        # inflate, so it has to fire first. An uncompressed content stream over the raw cap
        # proves the ordering: xref_stream() (which decompresses) is never reached.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bulky.pdf"
            doc = fitz.open()
            page = doc.new_page()
            page.draw_rect(fitz.Rect(0, 0, 1, 1))  # establishes a content-stream xref
            # compress=False stores the stream verbatim, so the raw length is the real length.
            doc.update_stream(page.get_contents()[0], b"100 100 1 1 re S\n" * 100_000, compress=False)
            doc.save(path)  # ~1.7 MB raw, over MAX_CONTENT_STREAM_RAW_BYTES_PER_PAGE
            doc.close()
            self.assertGreater(path.stat().st_size, MAX_CONTENT_STREAM_RAW_BYTES_PER_PAGE)

            def _fail_if_called(*_args, **_kwargs):
                raise AssertionError("xref_stream() must not decompress a stream the raw gate already refused")

            with mock.patch.object(fitz.Document, "xref_stream", _fail_if_called):
                info = inspect_pdf(path, max_pages=3)
        self.assertGreater(info.max_drawings_per_page, 10_000)
        self.assertEqual(0, info.pages_without_text)

    def test_pathological_page_is_reported_as_complex_rather_than_textless(self):
        # Skipping get_text() leaves the page with no measured text, but check_pdf_info raises
        # no_text_layer before page_too_complex, so a pathological page must not be counted in
        # pages_without_text -- otherwise the user is told to upload a PDF with a text layer
        # when the real reason is complexity.
        with tempfile.TemporaryDirectory() as temp_dir:
            info = inspect_pdf(_write_flate_bomb_pdf(Path(temp_dir) / "bomb.pdf"), max_pages=3)
        self.assertEqual(0, info.pages_without_text)
        limits = InputLimits(max_bytes=4_000_000, max_pages=3, max_source_pages=100, max_page_area_pt=2 * A3_AREA_PT)
        with self.assertRaises(TrialRejected) as rejected:
            check_pdf_info(info, limits)
        self.assertEqual("page_too_complex", rejected.exception.rejection.code)


class TestImageLimits(unittest.TestCase):
    def test_decompression_bomb_limit_is_set_on_import(self):
        from PIL import Image

        import problem_parser  # noqa: F401  (import side effect under test)

        self.assertEqual(20_000_000, Image.MAX_IMAGE_PIXELS)

    def test_pillow_warns_at_the_limit_and_raises_at_twice_it(self):
        # Pillow's contract: warn above MAX_IMAGE_PIXELS, raise above 2x it. The setting is
        # therefore a 40M-pixel hard ceiling, well above the ~15.5M pixels of 2xA3 at 200 DPI
        # that this trial actually renders. Asserted against Pillow's own size check so an
        # upgrade that changes the warn/raise semantics fails here rather than in production.
        import problem_parser  # noqa: F401  (import side effect under test)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            Image._decompression_bomb_check((3000, 3000))  # 9M px: a page this trial renders
        self.assertEqual([], [str(entry.message) for entry in caught])

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            Image._decompression_bomb_check((4500, 4500))  # 20.25M px: over the warn threshold
        self.assertEqual(1, len(caught))
        self.assertTrue(issubclass(caught[0].category, Image.DecompressionBombWarning))

        with self.assertRaises(Image.DecompressionBombError):
            Image._decompression_bomb_check((7000, 7000))  # 49M px: over the 40M hard ceiling


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

    def test_page_repair_is_populated_with_one_disabled_entry_per_page_by_default(self):
        # The one link that makes ParseResult.page_repair real -- reading
        # ai_fallback out of each PageModel's metadata into
        # ParseResult.page_repair -- had no test at all: every oracle test
        # patches oracle.parse_problems with a hand-built ParseResult, so the
        # metadata extraction was only ever asserted against fake dicts
        # handed straight to summarize_page_repair. Pin it here against a
        # real parse instead, and with it the trial-side claim that every
        # trial entry (ai_fallback_config=None, the trial's default) is an
        # inert "disabled".
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2], [3, 4]])
            result = parse_problems(path, work_dir=root / "work")

        self.assertEqual(len(result.pages), len(result.page_repair))
        self.assertEqual(2, len(result.page_repair))
        self.assertEqual(["disabled", "disabled"], [entry["status"] for entry in result.page_repair])
        for entry in result.page_repair:
            self.assertEqual(
                {
                    "status": "disabled",
                    "enabled": False,
                    "attempted": False,
                    "applied": False,
                    "cache_hit": False,
                    "mode": "off",
                    # A page AI repair never touched carries no AI evidence:
                    # pin the defaults end-to-end, so a build that reported
                    # changed=True with no GEMINI_API_KEY set cannot ship.
                    "changed": False,
                    "blocks_changed": 0,
                },
                {
                    key: entry[key]
                    for key in ("status", "enabled", "attempted", "applied", "cache_hit", "mode", "changed", "blocks_changed")
                },
            )

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

    def test_explicit_no_ai_arguments_match_the_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2], [3, 4]])
            default = parse_problems(path, work_dir=root / "a")
            explicit = parse_problems(path, work_dir=root / "b", ocr_mode="none", ai_fallback_config=None)
        self.assertEqual([p.number for p in default.problems], [p.number for p in explicit.problems])
        self.assertEqual([p.regions[0].bbox for p in default.problems], [p.regions[0].bbox for p in explicit.problems])

    def test_recognition_arguments_reach_build_pages(self):
        # test_explicit_no_ai_arguments_match_the_default only proves the
        # keywords are accepted, not that they are forwarded: "none"/None
        # produce the same numbers/bboxes as the old hardcoded call site, so
        # a dropped-kwargs implementation would pass it too. Capture the
        # kwargs at the build_pages boundary instead.
        import build_problem_board_edb as board

        seen: list[tuple[str, dict | None]] = []
        real = board.build_pages

        def recorder(*args, **kwargs):
            seen.append((kwargs["ocr_mode"], kwargs["ai_fallback_config"]))
            return real(*args, **kwargs)

        config = {"mode": "force", "provider": "gemini"}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2]])
            with mock.patch.object(board, "build_pages", recorder):
                parse_problems(path, work_dir=root / "a")
                parse_problems(path, work_dir=root / "b", ocr_mode="none", ai_fallback_config=config)
        self.assertEqual([("none", None), ("none", config)], seen)


if __name__ == "__main__":
    unittest.main()
