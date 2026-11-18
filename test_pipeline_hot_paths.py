from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image, ImageDraw

import build_problem_board_edb as board
import preprocess
import segment
from structured_schema import Box


def _two_column_mask(width: int = 600, height: int = 800) -> Image.Image:
    """An L-mode mask where 255 marks ink, matching the segmenter's convention."""
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    for top in range(40, height - 40, 30):
        draw.rectangle((30, top, width // 2 - 40, top + 12), fill=255)
        draw.rectangle((width // 2 + 40, top, width - 30, top + 12), fill=255)
    return mask


class TestColumnProjection(unittest.TestCase):
    def test_vectorized_projection_matches_the_per_column_histogram(self) -> None:
        mask = _two_column_mask()
        expected = [
            int(mask.crop((x, 0, x + 1, mask.height)).histogram()[255])
            for x in range(mask.width)
        ]
        self.assertEqual(expected, segment._column_dark_projection(mask))

    def test_document_columns_still_split_a_two_column_page(self) -> None:
        mask = _two_column_mask()
        content_box = Box(left=0.0, top=0.0, width=float(mask.width), height=float(mask.height))
        options = segment.SegmentOptions()

        columns = segment._detect_document_columns(mask, content_box, options)

        self.assertEqual(2, len(columns))
        self.assertLess(columns[0].right, columns[1].left + 1.0)


class TestPassageColumnDividerIsResolvedPerPage(unittest.TestCase):
    def _task(self, source_image: Image.Image, root: Path, index: int) -> board._ProblemAssetTask:
        return board._ProblemAssetTask(
            source_image=source_image,
            bounds=Box(left=10.0, top=10.0 + index * 120.0, width=400.0, height=100.0),
            crop_path=root / f"crop-{index}.png",
            board_render_path=root / f"board-{index}.png",
            chalk_color=(255, 255, 255),
            preserve_horizontal_bounds=True,
        )

    def test_one_detection_serves_every_passage_problem_on_a_page(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            page = Image.new("RGB", (800, 1000), "white")
            draw = ImageDraw.Draw(page)
            for top in range(20, 960, 24):
                draw.rectangle((40, top, 360, top + 10), fill=(20, 20, 20))
                draw.rectangle((440, top, 760, top + 10), fill=(20, 20, 20))
            tasks = [self._task(page, root, index) for index in range(4)]

            calls: list[int] = []
            real_detector = board.detect_pdf_visual_column_divider_x

            def counting_detector(image):
                calls.append(1)
                return real_detector(image)

            with patch.object(board, "detect_pdf_visual_column_divider_x", counting_detector):
                board._render_problem_assets(tasks)

            self.assertEqual(1, len(calls))
            self.assertTrue(all(task.passage_column_divider_resolved for task in tasks))
            self.assertEqual(
                {tasks[0].passage_column_divider_x},
                {task.passage_column_divider_x for task in tasks},
            )

    def test_pages_are_resolved_independently(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            first_page = Image.new("RGB", (800, 1000), "white")
            second_page = Image.new("RGB", (800, 1000), "white")
            for page in (first_page, second_page):
                draw = ImageDraw.Draw(page)
                for top in range(20, 960, 24):
                    draw.rectangle((40, top, 360, top + 10), fill=(20, 20, 20))
            tasks = [self._task(first_page, root, 0), self._task(second_page, root, 1)]

            calls: list[int] = []
            real_detector = board.detect_pdf_visual_column_divider_x

            with patch.object(
                board,
                "detect_pdf_visual_column_divider_x",
                lambda image: (calls.append(1), real_detector(image))[1],
            ):
                board._render_problem_assets(tasks)

            self.assertEqual(2, len(calls))

    def test_a_task_rendered_on_its_own_still_detects(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            page = Image.new("RGB", (400, 400), "white")
            task = self._task(page, root, 0)

            calls: list[int] = []
            with patch.object(
                board,
                "detect_pdf_visual_column_divider_x",
                lambda image: (calls.append(1), None)[1],
            ):
                board._render_problem_asset(task)

            self.assertEqual(1, len(calls))


class TestSourceDigestReuse(unittest.TestCase):
    def setUp(self) -> None:
        preprocess._cached_file_sha1.cache_clear()

    def test_repeat_digests_of_one_revision_read_the_file_once(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            source = Path(raw_tmp) / "exam.pdf"
            source.write_bytes(b"%PDF-1.7 sample payload")

            digests = {preprocess._file_sha1(source) for _ in range(6)}

            self.assertEqual(1, len(digests))
            self.assertEqual(1, preprocess._cached_file_sha1.cache_info().misses)
            self.assertEqual(5, preprocess._cached_file_sha1.cache_info().hits)

    def test_a_rewritten_file_gets_a_fresh_digest(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            source = Path(raw_tmp) / "exam.pdf"
            source.write_bytes(b"first")
            first = preprocess._file_sha1(source)

            source.write_bytes(b"second payload of a different length")
            self.assertNotEqual(first, preprocess._file_sha1(source))

    def test_a_missing_file_still_raises(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            with self.assertRaises(OSError):
                preprocess._file_sha1(Path(raw_tmp) / "absent.pdf")


class TestDeskewCoordinateExtraction(unittest.TestCase):
    def test_skewed_text_is_straightened(self) -> None:
        if preprocess.cv2 is None or preprocess.np is None:  # pragma: no cover
            self.skipTest("OpenCV is not installed")

        page = Image.new("L", (620, 880), 255)
        draw = ImageDraw.Draw(page)
        for top in range(60, 820, 24):
            draw.rectangle((80, top, 540, top + 9), fill=0)
        skewed = page.rotate(-2.4, resample=Image.BICUBIC, fillcolor=255).convert("RGB")

        deskewed = preprocess.deskew_image(skewed)

        self.assertEqual(skewed.size, deskewed.size)
        self.assertNotEqual(skewed.tobytes(), deskewed.tobytes())

    def test_an_almost_straight_page_is_returned_untouched(self) -> None:
        if preprocess.cv2 is None or preprocess.np is None:  # pragma: no cover
            self.skipTest("OpenCV is not installed")

        page = Image.new("RGB", (400, 500), "white")
        draw = ImageDraw.Draw(page)
        for top in range(40, 460, 20):
            draw.rectangle((40, top, 360, top + 8), fill=(10, 10, 10))

        self.assertIs(page, preprocess.deskew_image(page))

    def test_a_blank_page_is_returned_untouched(self) -> None:
        if preprocess.cv2 is None or preprocess.np is None:  # pragma: no cover
            self.skipTest("OpenCV is not installed")

        blank = Image.new("RGB", (200, 200), "white")
        self.assertIs(blank, preprocess.deskew_image(blank))


if __name__ == "__main__":
    unittest.main()
