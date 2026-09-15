import io
import tempfile
import unittest
from pathlib import Path

import fitz
from PIL import Image

from assemble_page import group_problem_units
from build_problem_board_edb import build_problem_entries
from layout_template_schema import LayoutTemplate
from preprocess import PreparedPage
from problem_parser import PdfUnreadableError, inspect_pdf
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

    def test_over_limit_skips_page_scan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "long.pdf"
            doc = fitz.open()
            for _ in range(5):
                doc.new_page(width=600, height=800)
            doc.save(path)
            doc.close()
            info = inspect_pdf(path, max_pages=3)
        self.assertEqual(5, info.page_count)
        self.assertEqual(0, info.pages_without_text)
        self.assertEqual(0.0, info.max_page_area_pt)

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


if __name__ == "__main__":
    unittest.main()
