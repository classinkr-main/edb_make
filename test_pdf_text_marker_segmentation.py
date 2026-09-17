import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import fitz
from PIL import Image, ImageDraw

from build_problem_board_edb import build_problem_entries
from build_structured_page_json import build_page_model
from layout_template_schema import LayoutTemplate
from page_repair import build_ai_fallback_config
import preprocess as preprocess_module
from preprocess import prepare_source_pages
from segment import (
    PDF_CHOICE_MARKERS,
    _build_pdf_passage_range_blocks,
    _extract_pdf_passage_range,
    _indented_nested_enumeration_marker_ids,
    _looks_like_pdf_page_header_text_line,
    _trim_pdf_problem_bottom_to_last_choice,
    segment_page,
)
from structured_schema import Box, Subject


def _problem_block_ids(problem):
    return (
        list(problem.stem_block_ids)
        + list(problem.choice_block_ids)
        + list(problem.explanation_block_ids)
        + list(problem.figure_block_ids)
    )


class TestPdfTextMarkerSegmentation(unittest.TestCase):
    def test_passage_text_box_cannot_expand_across_center_divider(self):
        image = Image.new("RGB", (600, 800), "white")
        ImageDraw.Draw(image).rectangle((40, 130, 285, 180), outline="black")
        text_lines = [
            {
                "text": "[1~2] 다음 글을 읽고 물음에 답하시오.",
                "bbox": {"left": 40, "top": 90, "right": 350, "bottom": 116},
            },
            {
                "text": "지문 본문이 중앙선 가까이 이어집니다.",
                "bbox": {"left": 40, "top": 136, "right": 355, "bottom": 168},
            },
        ]
        right_markers = [
            {"number": 1, "bbox": {"left": 330, "top": 240, "right": 350, "bottom": 265}},
            {"number": 2, "bbox": {"left": 330, "top": 420, "right": 350, "bottom": 445}},
        ]

        blocks = _build_pdf_passage_range_blocks(
            image,
            "page-1",
            text_lines,
            [(1, [], (40.0, 285.0)), (2, right_markers, (315.0, 560.0))],
            page_area=float(image.width * image.height),
            start_index=1,
        )

        self.assertTrue(blocks)
        self.assertEqual(294.0, blocks[0].bbox.right)
        self.assertEqual(300.0, blocks[0].metadata.get("passage_center_divider_x"))

    def test_compact_exam_page_headers_are_not_passage_text(self):
        top_box = Box(left=330, top=10, width=220, height=30)

        for text in (
            "고2",
            "고 2",
            "고1 11",
            "영역",
            "영어영역",
            "국어 영역",
            "홀수형",
            "짝수형",
            "(언어와 매체)",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    _looks_like_pdf_page_header_text_line(text, top_box, 1200)
                )
        self.assertFalse(
            _looks_like_pdf_page_header_text_line(
                "실제 지문 첫 문장입니다.",
                top_box,
                1200,
            )
        )

    def test_pdf_problem_markers_drive_problem_count_without_ocr(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "two_column_exam.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            for number, x, y in ((1, 60, 120), (2, 60, 430), (3, 330, 120), (4, 330, 430)):
                page.insert_text((x, y), f"{number}. problem stem", fontsize=14)
                page.draw_rect(fitz.Rect(x + 35, y + 50, x + 180, y + 140), color=(0, 0, 0), width=1)
                page.insert_text((x, y + 210), "① a   ② b   ③ c", fontsize=12)
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            self.assertFalse(prepared.metadata.get("deskewed"))
            self.assertEqual("pdf_text_layer", prepared.metadata.get("deskew_skipped_reason"))
            self.assertGreater(len(prepared.metadata.get("pdf_text_lines") or []), 0)
            segmented = segment_page(prepared, page_id=prepared.page_id, subject=Subject.MATH)

            self.assertEqual("pdf-text-markers", segmented.metadata.get("segmenter"))
            self.assertEqual(4, len(segmented.blocks))
            self.assertEqual([1, 2, 3, 4], [block.metadata.get("problem_number") for block in segmented.blocks])

            page_model = build_page_model(
                prepared,
                subject=Subject.MATH,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )
            self.assertEqual(4, len(page_model.problems))

    def test_pdf_problem_markers_keep_leading_sequence_before_question_ten(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "leading_sequence_exam.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            for number in range(1, 13):
                column = 0 if number <= 6 else 1
                row = number - 1 if column == 0 else number - 7
                page.insert_text(
                    (48 + (column * 282), 90 + (row * 105)),
                    f"{number}. problem stem",
                    fontsize=12,
                )
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.ENGLISH,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            self.assertEqual(
                list(range(1, 13)),
                [
                    problem.metadata.get("problem_number")
                    for problem in page_model.problems
                    if problem.metadata.get("problem_number") is not None
                ],
            )
            self.assertEqual(0, page_model.metadata.get("pdf_nested_enumeration_marker_count"))

    def test_pdf_passage_range_block_attaches_to_child_problems_without_ocr(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "passage_range_exam.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((48, 92), "[1~2] 다음 글을 읽고 물음에 답하시오.", fontsize=14)
            page.insert_text((48, 138), "shared passage first line", fontsize=12)
            page.insert_text((48, 170), "shared passage second line", fontsize=12)
            page.insert_text((48, 320), "1. first question", fontsize=14)
            page.insert_text((330, 320), "2. second question", fontsize=14)
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.KOREAN,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            by_number = {
                problem.metadata.get("problem_number"): problem
                for problem in page_model.problems
                if problem.metadata.get("problem_number") is not None
            }
            self.assertEqual({1, 2}, set(by_number))
            passage_fragments = [
                problem
                for problem in page_model.problems
                if problem.metadata.get("passage_role") == "passage_fragment"
            ]
            self.assertEqual(1, len(passage_fragments))
            shared_ids = by_number[1].metadata.get("shared_passage_block_ids")
            self.assertTrue(shared_ids)
            self.assertEqual(shared_ids, _problem_block_ids(passage_fragments[0]))
            self.assertEqual(shared_ids, by_number[2].metadata.get("shared_passage_block_ids"))

            shared_block = next(
                block for block in page_model.blocks if block.block_id == shared_ids[0]
            )
            first_problem_block = next(
                block for block in page_model.blocks if block.metadata.get("problem_number") == 1
            )
            self.assertEqual("pdf-passage-range", shared_block.metadata.get("segmenter"))
            self.assertLess(shared_block.bbox.top, first_problem_block.bbox.top)
            self.assertLess(shared_block.bbox.bottom, first_problem_block.bbox.top)
            header_line = next(
                line
                for line in prepared.metadata.get("pdf_text_lines") or []
                if "[1~2]" in str(line.get("text") or "")
            )
            self.assertGreaterEqual(
                shared_block.bbox.right,
                float(header_line["bbox"]["right"]),
            )
            self.assertEqual(1.0, shared_block.metadata.get("passage_text_bounds_score"))

            # AI grouping may annotate the supplemental passage with the first
            # child question's bbox. Passage-only crop generation must keep the
            # passage block bounds instead of accepting that unrelated override.
            passage = passage_fragments[0]
            passage.metadata["grouping_source"] = "ai_fallback"
            passage.metadata["bbox_px"] = {
                "left": first_problem_block.bbox.left,
                "top": first_problem_block.bbox.top,
                "width": first_problem_block.bbox.width,
                "height": first_problem_block.bbox.height,
            }
            entries = build_problem_entries(
                [prepared],
                [page_model],
                Path(temp_dir) / "passage_only_out",
                LayoutTemplate(name="academy-default"),
                content_target="shared-passages",
            )
            self.assertEqual(1, len(entries))
            self.assertLess(entries[0].bounds.top, first_problem_block.bbox.top)
            self.assertGreater(entries[0].bounds.height, first_problem_block.bbox.height * 2)
            passage_quality = passage.metadata["passage_quality"]
            self.assertNotEqual("poor", passage_quality["grade"])
            self.assertNotIn("near_blank_passage_crop", passage_quality["warnings"])

    def test_pdf_numeric_interval_without_shared_cue_is_not_a_passage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "numeric_interval.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((48, 82), "1. first question", fontsize=14)
            page.insert_text((48, 150), "[1~3] x value interval", fontsize=12)
            page.insert_text((48, 320), "2. second question", fontsize=14)
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.MATH,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            self.assertFalse(any(
                problem.metadata.get("passage_role") == "passage_fragment"
                for problem in page_model.problems
            ))
            self.assertEqual(0, page_model.metadata.get("pdf_passage_range_block_count", 0))

    def test_pdf_multiline_korean_passage_header_is_joined(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "multiline_passage_header.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            # PyMuPDF's built-in CJK font is platform-independent. A macOS
            # system font path made this fixture fail on Linux CI before the
            # segmentation code was exercised.
            font_name = "korea"
            page.insert_text(
                (48, 82),
                "[24~27] (가)와 (나)는 학생이 읽은 글이고,",
                fontname=font_name,
                fontsize=11,
            )
            page.insert_text(
                (48, 104),
                "(다)는 이를 바탕으로 쓴 건의문의 초고이다. 물음에 답하시오.",
                fontname=font_name,
                fontsize=11,
            )
            page.insert_text((48, 145), "shared passage first line", fontsize=11)
            page.insert_text((48, 175), "shared passage second line", fontsize=11)
            page.insert_text((48, 330), "24. first question", fontsize=14)
            page.insert_text((330, 330), "25. second question", fontsize=14)
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.KOREAN,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            passage_fragments = [
                problem
                for problem in page_model.problems
                if problem.metadata.get("passage_role") == "passage_fragment"
            ]
            self.assertEqual(1, len(passage_fragments))
            self.assertEqual(
                {"start": 24, "end": 27},
                passage_fragments[0].metadata.get("passage_range"),
            )
            passage_blocks = [
                block
                for block in page_model.blocks
                if block.metadata.get("segmenter") == "pdf-passage-range"
            ]
            self.assertEqual(1, len(passage_blocks))
            display_title = passage_blocks[0].metadata.get("display_title", "").replace("\xa0", " ")
            self.assertIn("건의문의 초고", display_title)
            divider_x = passage_blocks[0].metadata.get("passage_center_divider_x")
            divider_exclusion = passage_blocks[0].metadata.get(
                "passage_center_divider_exclusion_px"
            )
            self.assertIsNotNone(divider_x)
            self.assertLessEqual(
                passage_blocks[0].bbox.right,
                float(divider_x) - float(divider_exclusion),
            )
            # The deliberately long two-line heading reaches into the center
            # gutter.  Clipping that gutter is intentional, while the retained
            # text coverage must remain high enough to preserve recognition.
            self.assertGreater(
                passage_blocks[0].metadata.get("passage_text_bounds_score"),
                0.65,
            )

    def test_pdf_shared_style_header_inside_started_problem_is_not_a_passage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "internal_passage_header.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((48, 82), "35. first question", fontsize=14)
            page.insert_text((48, 170), "[35~37] Read the passage and answer the questions.", fontsize=11)
            page.insert_text((48, 230), "internal excerpt line", fontsize=11)
            page.insert_text((48, 520), "36. second question", fontsize=14)
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.ENGLISH,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            self.assertFalse(any(
                problem.metadata.get("passage_role") == "passage_fragment"
                for problem in page_model.problems
            ))
            self.assertEqual(0, page_model.metadata.get("pdf_passage_range_block_count", 0))

    def test_pdf_passage_range_block_can_attach_to_prior_column_child_markers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "cross_column_passage_range_exam.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((330, 120), "1. first question", fontsize=14)
            page.insert_text((330, 320), "2. second question", fontsize=14)
            page.insert_text((48, 540), "[1~2] 다음 글을 읽고 물음에 답하시오.", fontsize=14)
            page.insert_text((48, 590), "shared passage first line", fontsize=12)
            page.insert_text((48, 630), "shared passage second line", fontsize=12)
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.ENGLISH,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            by_number = {
                problem.metadata.get("problem_number"): problem
                for problem in page_model.problems
                if problem.metadata.get("problem_number") is not None
            }
            self.assertEqual({1, 2}, set(by_number))
            passage_fragments = [
                problem
                for problem in page_model.problems
                if problem.metadata.get("passage_role") == "passage_fragment"
            ]
            self.assertEqual(1, len(passage_fragments))
            shared_ids = by_number[1].metadata.get("shared_passage_block_ids")
            self.assertTrue(shared_ids)
            self.assertEqual(shared_ids, _problem_block_ids(passage_fragments[0]))
            self.assertEqual(shared_ids, by_number[2].metadata.get("shared_passage_block_ids"))

            shared_block = next(
                block for block in page_model.blocks if block.block_id == shared_ids[0]
            )
            first_problem_block = next(
                block for block in page_model.blocks if block.metadata.get("problem_number") == 1
            )
            self.assertEqual("pdf-passage-range", shared_block.metadata.get("segmenter"))
            self.assertGreater(shared_block.bbox.top, first_problem_block.bbox.top)

    def test_pdf_passage_range_stitches_following_column_before_child_questions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = root / "two_column_continued_passage.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.draw_line((300, 45), (300, 755), color=(0, 0, 0), width=0.5)
            page.insert_text((48, 82), "[1~2] Read the passage and answer the questions.", fontsize=12)
            for row, y in enumerate(range(120, 730, 28), start=1):
                page.insert_text((48, y), f"left passage line {row:02d}", fontsize=11)
            for row, y in enumerate(range(82, 300, 28), start=1):
                page.insert_text((330, y), f"continued passage line {row:02d}", fontsize=11)
            page.insert_text((330, 350), "1. first question", fontsize=14)
            page.insert_text((342, 410), "① a   ② b   ③ c", fontsize=12)
            page.insert_text((330, 540), "2. second question", fontsize=14)
            page.insert_text((342, 600), "① a   ② b   ③ c", fontsize=12)
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.KOREAN,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            by_number = {
                problem.metadata.get("problem_number"): problem
                for problem in page_model.problems
                if problem.metadata.get("problem_number") is not None
            }
            self.assertEqual({1, 2}, set(by_number))
            passage = next(
                problem
                for problem in page_model.problems
                if problem.metadata.get("passage_role") == "passage_fragment"
            )
            shared_ids = _problem_block_ids(passage)
            self.assertEqual(2, len(shared_ids))
            self.assertEqual(shared_ids, by_number[1].metadata.get("shared_passage_block_ids"))
            self.assertEqual(shared_ids, by_number[2].metadata.get("shared_passage_block_ids"))
            shared_blocks = [
                next(block for block in page_model.blocks if block.block_id == block_id)
                for block_id in shared_ids
            ]
            self.assertEqual({1, 2}, {block.metadata.get("column_index") for block in shared_blocks})
            first_question = next(
                block for block in page_model.blocks if block.metadata.get("problem_number") == 1
            )
            right_fragment = next(
                block for block in shared_blocks if block.metadata.get("column_index") == 2
            )
            self.assertLess(right_fragment.bbox.bottom, first_question.bbox.top)
            right_passage_lines = [
                line
                for line in prepared.metadata.get("pdf_text_lines") or []
                if "continued passage line" in str(line.get("text") or "")
            ]
            last_right_line_bottom = max(
                float(line["bbox"]["bottom"])
                for line in right_passage_lines
            )
            self.assertGreaterEqual(
                right_fragment.bbox.bottom,
                last_right_line_bottom + 3.5,
            )
            first_question_marker = next(
                marker
                for marker in prepared.metadata.get("pdf_problem_markers") or []
                if marker.get("number") == 1
            )
            self.assertGreaterEqual(
                float(first_question_marker["bbox"]["top"]) - right_fragment.bbox.bottom,
                8.0,
            )

            entries = build_problem_entries(
                [prepared],
                [page_model],
                root / "out",
                LayoutTemplate(name="academy-default"),
            )
            passage_entry = next(entry for entry in entries if entry.problem_id == passage.unit_id)
            self.assertEqual(
                [1, 2],
                [segment["column_index"] for segment in passage_entry.source_segments],
            )
            self.assertEqual(
                [prepared.page_id, prepared.page_id],
                [segment["source_page_id"] for segment in passage_entry.source_segments],
            )
            self.assertLess(
                passage_entry.source_segments[0]["bbox"]["left"],
                passage_entry.source_segments[1]["bbox"]["left"],
            )
            with Image.open(passage_entry.crop_path) as stitched:
                # The instruction header can be wider than the passage body;
                # preserving its full text takes priority over forcing the
                # stitched asset into the narrower visual column width.
                self.assertLess(stitched.width, prepared.image.width * 0.8)
                self.assertGreater(stitched.height, prepared.image.height * 0.75)

    def test_pdf_passage_range_without_same_page_questions_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "passage_only_page.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.draw_line((300, 45), (300, 755), color=(0, 0, 0), width=0.5)
            page.insert_text((48, 82), "[31~34] Read the passage and answer the questions.", fontsize=12)
            for row, y in enumerate(range(120, 730, 28), start=1):
                page.insert_text((48, y), f"left passage line {row:02d}", fontsize=11)
            for row, y in enumerate(range(82, 730, 28), start=1):
                page.insert_text((330, y), f"right passage line {row:02d}", fontsize=11)
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.KOREAN,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            passages = [
                problem
                for problem in page_model.problems
                if problem.metadata.get("passage_role") == "passage_fragment"
            ]
            self.assertEqual(1, len(passages))
            self.assertEqual({"start": 31, "end": 34}, passages[0].metadata.get("passage_range"))
            self.assertEqual(2, len(_problem_block_ids(passages[0])))
            self.assertEqual("pdf-passage-ranges", page_model.metadata.get("segmenter"))

            # Passage rendering must not run the generic vertical-guide trim:
            # on real exam columns it can interpret final glyph strokes as a
            # guide and remove the rightmost 1-3 characters.
            with patch(
                "build_problem_board_edb._trim_edge_vertical_guides",
                side_effect=AssertionError("passage crop must preserve horizontal bounds"),
            ), patch(
                "build_problem_board_edb._trim_edge_attached_page_chrome",
                side_effect=AssertionError("passage crop must not trim edge-adjacent glyphs"),
            ):
                entries = build_problem_entries(
                    [prepared],
                    [page_model],
                    Path(temp_dir) / "out",
                    LayoutTemplate(name="academy-default"),
                )
            passage_entry = next(
                entry for entry in entries if entry.problem_id == passages[0].unit_id
            )
            passage_quality = passages[0].metadata.get("passage_quality")
            self.assertIsInstance(passage_quality, dict)
            self.assertGreater(float(passage_quality.get("score_10") or 0), 0)
            self.assertGreater(int(passage_quality.get("width_px") or 0), 0)
            self.assertIn(passage_quality.get("grade"), {"good", "review", "poor"})
            shared_blocks = [
                block
                for block in page_model.blocks
                if block.block_id in set(_problem_block_ids(passages[0]))
            ]
            with Image.open(passage_entry.crop_path) as preserved_crop:
                self.assertGreaterEqual(
                    preserved_crop.width,
                    max(round(block.bbox.width) for block in shared_blocks) + 40,
                )

    def test_pdf_passage_range_prevents_exw_from_becoming_example_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "exw_passage_page.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((48, 82), "10. preceding question", fontsize=14)
            page.insert_text((48, 280), "[11~15] Read the passage and answer the questions.", fontsize=12)
            page.insert_text((48, 330), "shared passage first line", fontsize=11)
            page.insert_text((330, 82), "EXW means Ex Works in international trade.", fontsize=11)
            page.insert_text((330, 120), "continued passage line", fontsize=11)
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.KOREAN,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            self.assertEqual("pdf-text-markers", page_model.metadata.get("segmenter"))
            self.assertIn(10, {
                problem.metadata.get("problem_number")
                for problem in page_model.problems
            })
            passages = [
                problem
                for problem in page_model.problems
                if problem.metadata.get("passage_role") == "passage_fragment"
            ]
            self.assertEqual(1, len(passages))
            self.assertEqual({"start": 11, "end": 15}, passages[0].metadata.get("passage_range"))

    def test_pdf_problem_markers_ignore_nested_low_number_procedure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "nested_procedure.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((48, 82), "7. seventh question", fontsize=14)
            page.insert_text((48, 260), "8. eighth question", fontsize=14)
            page.insert_text((72, 350), "1. first procedure step", fontsize=11)
            page.insert_text((72, 400), "2. second procedure step", fontsize=11)
            page.insert_text((330, 82), "9. ninth question", fontsize=14)
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.KOREAN,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            self.assertEqual(
                [7, 8, 9],
                [
                    problem.metadata.get("problem_number")
                    for problem in page_model.problems
                    if problem.metadata.get("problem_number") is not None
                ],
            )
            self.assertEqual(2, page_model.metadata.get("pdf_nested_enumeration_marker_count"))

    def test_pdf_problem_markers_ignore_indented_list_inside_a_later_question(self):
        """A boxed notice's "1. 2. 3. 4." list must not become four problems.

        Reproduces the 2026 3월 고2 영어 학력평가 page 4 trap: question 28's
        안내문 carries a "How It Works" list numbered 1.-4. that restarts below
        question 28 and is indented from the column's question markers. The
        list steps belong to question 28, so no extra problem may appear and
        question 28's block must still reach past the last list line. As on
        the real page the list is boxed, question 28 prints no answer choices
        before the box, and its own ①-⑤ choices resume below it -- the
        shape ``_indented_nested_enumeration_marker_ids`` requires before it
        drops anything, and the one a restarted section of real questions
        does not have (see the "restarted section" tests below).
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "boxed_notice_list.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((48, 100), "25. twenty-fifth question", fontsize=14)
            page.insert_text((48, 400), "26. twenty-sixth question", fontsize=14)
            page.insert_text((330, 100), "27. twenty-seventh question", fontsize=14)
            page.insert_text((330, 400), "28. Library of Things notice question", fontsize=14)
            page.draw_rect(fitz.Rect(336, 430, 570, 620))
            page.insert_text((354, 470), "1. Download the app and log in.", fontsize=11)
            page.insert_text((354, 510), "2. Check out what you need in the app.", fontsize=11)
            page.insert_text((354, 550), "3. Pick up your item from Monday to Sunday.", fontsize=11)
            page.insert_text((354, 590), "4. Return it in good condition.", fontsize=11)
            # PyMuPDF's default "helv" font has no glyph for the circled
            # digits and silently substitutes "." for them, so the choice
            # lines are inserted with a font that actually carries them --
            # otherwise _text_contains_choice_marker would never see one.
            page.insert_text(
                (330, 630), "① Log-in is optional before borrowing.", fontsize=11, fontname="korea"
            )
            page.insert_text(
                (330, 660), "② Reservation is only possible on site.", fontsize=11, fontname="korea"
            )
            page.insert_text(
                (330, 690), "③ Items can only be picked up on weekdays.", fontsize=11, fontname="korea"
            )
            page.insert_text(
                (330, 720), "④ The rental period cannot be extended.", fontsize=11, fontname="korea"
            )
            page.insert_text(
                (330, 750), "⑤ The late fee is two dollars a day.", fontsize=11, fontname="korea"
            )
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.ENGLISH,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            self.assertEqual(
                [25, 26, 27, 28],
                [
                    problem.metadata.get("problem_number")
                    for problem in page_model.problems
                    if problem.metadata.get("problem_number") is not None
                ],
            )
            self.assertEqual(4, page_model.metadata.get("pdf_nested_enumeration_marker_count"))
            blocks = {block.block_id: block for block in page_model.blocks}
            last_list_line_bottom = 590 / 800 * page_model.height_px
            question_28 = next(
                problem
                for problem in page_model.problems
                if problem.metadata.get("problem_number") == 28
            )
            bottoms = [
                blocks[block_id].bbox.bottom
                for block_id in _problem_block_ids(question_28)
                if block_id in blocks
            ]
            self.assertTrue(bottoms)
            self.assertGreater(max(bottoms), last_list_line_bottom)

    def test_pdf_problem_markers_keep_a_flush_restarted_section(self):
        """Repeated numbers alone must not drop a marker.

        No case in the 13-case trial bench actually restarts its numbering
        -- every marker column in the corpus runs strictly ascending -- so
        whether a real restarted section prints flush with its column (and
        would therefore survive on indentation alone) is unverified, not a
        validated corpus pattern. This manufactured page only pins that a
        restart's numbers being out of sequence is not, by itself, enough to
        drop a marker: with the run printed flush (no indentation) the first
        signal never applies, so the second signal is never even reached.
        The next test below pins the case this one cannot: a restart that is
        shifted just enough to look indented.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "restarted_section.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((48, 100), "5. fifth question of the first section", fontsize=14)
            page.insert_text((48, 300), "6. sixth question of the first section", fontsize=14)
            page.insert_text((48, 500), "1. first question of the second section", fontsize=14)
            page.insert_text((48, 700), "2. second question of the second section", fontsize=14)
            page.insert_text((330, 100), "3. third question of the second section", fontsize=14)
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.KOREAN,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            self.assertEqual(
                [5, 6, 1, 2, 3],
                [
                    problem.metadata.get("problem_number")
                    for problem in page_model.problems
                    if problem.metadata.get("problem_number") is not None
                ],
            )
            self.assertEqual(0, page_model.metadata.get("pdf_nested_enumeration_marker_count"))

    def test_pdf_problem_markers_keep_an_inset_restarted_section_without_host_choices(self):
        """A restarted section without a trailing host choice list survives.

        Same page as ``test_pdf_problem_markers_keep_a_flush_restarted_section``,
        except the restarted section is shifted 7 pt to the right (about
        2.5 mm) instead of printed flush -- a boxed or inset restarted
        section, which the corpus does not contain an example of either way
        (see that test's docstring). That shift alone trips the indentation
        threshold and, combined with the restart's out-of-sequence numbers,
        used to silently delete questions 1 and 2 into question 6's crop.
        This page pins the trailing-choice signal on its own: nothing at all
        is printed after questions 1/2 (they are the last things in their
        column), so there is no resuming host choice line and the run
        survives. The two tests that follow pin the signals that have to
        carry the same page once it does print answer choices, which is what
        every question in the 13-case corpus does.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "restarted_section_inset.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((48, 100), "5. fifth question of the first section", fontsize=14)
            page.insert_text((48, 300), "6. sixth question of the first section", fontsize=14)
            page.insert_text((55, 500), "1. first question of the second section", fontsize=14)
            page.insert_text((55, 700), "2. second question of the second section", fontsize=14)
            page.insert_text((330, 100), "3. third question of the second section", fontsize=14)
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.KOREAN,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            self.assertEqual(
                [5, 6, 1, 2, 3],
                [
                    problem.metadata.get("problem_number")
                    for problem in page_model.problems
                    if problem.metadata.get("problem_number") is not None
                ],
            )
            self.assertEqual(0, page_model.metadata.get("pdf_nested_enumeration_marker_count"))

    def test_pdf_problem_markers_keep_an_inset_restarted_section_with_its_own_choices(self):
        """A trailing ①-⑤ line is not evidence that the run is a list.

        Same inset restarted section as the test above, except question 2 --
        the last question of the restarted run -- prints its own answer
        choices, as every question in the 13-case corpus does. That line
        sits exactly where a host question's resuming choices would, so the
        trailing-choice signal cannot tell the two apart and says "drop"
        here just as it does for the 안내문 list. Nothing boxes this section
        off, which is what has to keep questions 1 and 2 alive.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "restarted_section_inset_choices.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((48, 100), "5. fifth question of the first section", fontsize=14)
            page.insert_text((48, 300), "6. sixth question of the first section", fontsize=14)
            page.insert_text((55, 500), "1. first question of the second section", fontsize=14)
            page.insert_text((55, 640), "2. second question of the second section", fontsize=14)
            # "korea" for the same reason as in the boxed-notice test above:
            # the default font silently drops the circled digits.
            page.insert_text((55, 700), "① a ② b ③ c ④ d ⑤ e", fontsize=11, fontname="korea")
            page.insert_text((330, 100), "3. third question of the second section", fontsize=14)
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.KOREAN,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            self.assertEqual(
                [5, 6, 1, 2, 3],
                [
                    problem.metadata.get("problem_number")
                    for problem in page_model.problems
                    if problem.metadata.get("problem_number") is not None
                ],
            )
            self.assertEqual(0, page_model.metadata.get("pdf_nested_enumeration_marker_count"))

    def test_pdf_problem_markers_keep_a_boxed_restarted_section_with_its_own_choices(self):
        """A bordered workbook section that restarts at 1 keeps its questions.

        A restart-per-section workbook page: questions 11 and 12 flush in
        their column with their own ①-⑤ choices, then a bordered 유형 연습
        box whose questions restart at 1, 2, 3 inset 20 pt inside the
        border, each with its own choices. Everything the 안내문 list this
        filter exists for looks like is here -- the markers are inset, the
        numbers restart, the section is boxed, and a ①-⑤ line follows the
        last marker just where the host's resuming choices would be -- so
        what has to tell the two apart is where the *other* choice lines
        fall: question 12 has already printed its choices before the box,
        and questions 1 and 2 print theirs between the run's markers,
        neither of which happens inside a 안내문. Dropping the run deletes
        three real questions and stretches question 12's crop over the
        whole box.
        """
        choices = "① 가 ② 나 ③ 다 ④ 라 ⑤ 마"
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "boxed_restarted_workbook.pdf"
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            page.insert_text((40, 80), "11. 열한 번째 문제입니다.", fontsize=12, fontname="korea")
            page.insert_text((40, 110), choices, fontsize=11, fontname="korea")
            page.insert_text((40, 160), "12. 열두 번째 문제입니다.", fontsize=12, fontname="korea")
            page.insert_text((40, 190), choices, fontsize=11, fontname="korea")
            page.draw_rect(fitz.Rect(40, 230, 555, 700))
            page.insert_text((60, 265), "유형 연습", fontsize=12, fontname="korea")
            page.insert_text((60, 310), "1. 유형 연습 첫 번째 문제입니다.", fontsize=12, fontname="korea")
            page.insert_text((60, 345), choices, fontsize=11, fontname="korea")
            page.insert_text((60, 425), "2. 유형 연습 두 번째 문제입니다.", fontsize=12, fontname="korea")
            page.insert_text((60, 460), choices, fontsize=11, fontname="korea")
            page.insert_text((60, 545), "3. 유형 연습 세 번째 문제입니다.", fontsize=12, fontname="korea")
            page.insert_text((60, 580), choices, fontsize=11, fontname="korea")
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=200,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.KOREAN,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            self.assertEqual(
                [11, 12, 1, 2, 3],
                [
                    problem.metadata.get("problem_number")
                    for problem in page_model.problems
                    if problem.metadata.get("problem_number") is not None
                ],
            )
            self.assertEqual(0, page_model.metadata.get("pdf_nested_enumeration_marker_count"))
            blocks = {block.block_id: block for block in page_model.blocks}
            question_12 = next(
                problem
                for problem in page_model.problems
                if problem.metadata.get("problem_number") == 12
            )
            bottoms = [
                blocks[block_id].bbox.bottom
                for block_id in _problem_block_ids(question_12)
                if block_id in blocks
            ]
            self.assertTrue(bottoms)
            # The box's top border; question 12 ends above it.
            self.assertLess(max(bottoms), 230 / 842 * page_model.height_px)

    def test_indented_nested_run_kept_when_its_own_markers_carry_choices(self):
        """Choice lines between the run's markers block the drop on their own.

        Isolates the signal that stops the drop on the boxed workbook page
        above, where it fires before the border check is ever reached. Both
        halves of this test have the same geometry -- one host marker, an
        inset run that restarts below it, ruled borders above and below the
        run, and a resuming ①-⑤ line under the whole thing, which together
        are enough to drop the run -- and the only difference is a choice
        line printed between the run's two markers. A 안내문 list has no
        such line; a restarted section of real questions does.
        """
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.line([(60, 200), (560, 200)], fill="black", width=3)
        draw.line([(60, 600), (560, 600)], fill="black", width=3)
        host = {"number": 28, "bbox": {"left": 40, "top": 140, "right": 70, "bottom": 170}}
        first = {"number": 1, "bbox": {"left": 80, "top": 250, "right": 100, "bottom": 275}}
        second = {"number": 2, "bbox": {"left": 80, "top": 350, "right": 100, "bottom": 375}}
        column_entries = [(1, [host, first, second], (0.0, 600.0))]
        host_choice_line = {
            "text": "① a ② b ③ c ④ d ⑤ e",
            "bbox": {"left": 40, "top": 640, "right": 400, "bottom": 665},
        }
        run_choice_line = {
            "text": "① a ② b ③ c ④ d ⑤ e",
            "bbox": {"left": 80, "top": 290, "right": 400, "bottom": 315},
        }

        dropped = _indented_nested_enumeration_marker_ids(
            column_entries,
            image.width,
            text_lines=[host_choice_line],
            image=image,
        )
        self.assertEqual({id(first), id(second)}, dropped)

        kept = _indented_nested_enumeration_marker_ids(
            column_entries,
            image.width,
            text_lines=[run_choice_line, host_choice_line],
            image=image,
        )
        self.assertEqual(set(), kept)

    def test_pdf_problem_markers_keep_an_ascending_indented_run(self):
        """An indented but ascending run is not list content.

        Pins the ascending-sequence signal in isolation: markers 12 and 13
        are indented past 9-11 (same as an inset list would be) and a host
        choice line follows them (same as a genuine embedded list would
        have), so only the "does not continue the ascending run" signal
        keeps them from being dropped. Without it (e.g. if the ``number <=
        highest_number`` check were removed) every other guard in this test
        is satisfied and 12/13 would be swallowed.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "ascending_indented_run.pdf"
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            for number, x, y in ((9, 60, 80), (10, 60, 160), (11, 60, 240)):
                page.insert_textbox(
                    fitz.Rect(x, y, 545, y + 50),
                    f"{number}. problem stem for question {number}",
                    fontsize=12,
                )
            for number, x, y in ((12, 100, 340), (13, 100, 420)):
                page.insert_textbox(
                    fitz.Rect(x, y, 545, y + 50),
                    f"{number}. problem stem for question {number}",
                    fontsize=12,
                )
            page.insert_text(
                (60, 500), "① a   ② b   ③ c   ④ d   ⑤ e", fontsize=11, fontname="korea"
            )
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=200,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.KOREAN,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            self.assertEqual(
                [9, 10, 11, 12, 13],
                [
                    problem.metadata.get("problem_number")
                    for problem in page_model.problems
                    if problem.metadata.get("problem_number") is not None
                ],
            )
            self.assertEqual(0, page_model.metadata.get("pdf_nested_enumeration_marker_count"))

    def test_pdf_problem_markers_keep_a_narrow_indent_below_the_px_floor(self):
        """A sub-character indent must not reach ``PDF_NESTED_MARKER_MIN_INDENT_PX``.

        On this page the page-width ratio alone (~0.7%) would never trip the
        threshold, so only the fixed pixel floor is in play. The restarted
        section is shifted about one character width (7.2 pt) -- less than
        the 12 px floor -- and a host choice line follows it, so if the
        floor were not enforced (e.g. dropped to 0) this indent alone, with
        the run's out-of-sequence numbers, would be enough to drop it.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "narrow_indent_px_floor.pdf"
            doc = fitz.open()
            page = doc.new_page(width=504, height=700)
            stem = "{0}. This line of stem text runs most of the way across the page width for the crop"
            y = 80
            for number in (9, 10, 11):
                page.insert_textbox(
                    fitz.Rect(60, y, 484, y + 40), stem.format(number), fontsize=11
                )
                y += 90
            for number in (1, 2):
                page.insert_textbox(
                    fitz.Rect(67.2, y, 484, y + 40), stem.format(number), fontsize=11
                )
                y += 90
            page.insert_text((60, y + 20), "① a   ② b   ③ c", fontsize=11, fontname="korea")
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=100,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.KOREAN,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            self.assertEqual(
                [9, 10, 11, 1, 2],
                [
                    problem.metadata.get("problem_number")
                    for problem in page_model.problems
                    if problem.metadata.get("problem_number") is not None
                ],
            )
            self.assertEqual(0, page_model.metadata.get("pdf_nested_enumeration_marker_count"))

    def test_pdf_problem_markers_keep_a_narrow_indent_below_the_ratio_floor(self):
        """A sub-character indent must not reach ``PDF_NESTED_MARKER_MIN_INDENT_RATIO``.

        This page is wide enough (raster width ~1533 px) that the page-width
        ratio (1.2%, ~18 px) governs instead of the fixed 12 px floor. The
        restarted section is shifted about one character width (5.5 pt,
        ~15 px at this DPI) -- above the 12 px floor but below the ratio's
        18 px -- and a host choice line follows it, so this test would only
        fail if the ratio term were weakened (e.g. dropped to 0, leaving
        just the 12 px floor, which this indent clears).
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "narrow_indent_ratio_floor.pdf"
            doc = fitz.open()
            page = doc.new_page(width=850, height=1100)
            stem = "{0}. This line of stem text runs most of the way across the page width for the crop boundary test case here today"
            y = 80
            for number in (9, 10, 11):
                page.insert_textbox(
                    fitz.Rect(60, y, 830, y + 40), stem.format(number), fontsize=11
                )
                y += 90
            for number in (1, 2):
                page.insert_textbox(
                    fitz.Rect(65.5, y, 830, y + 40), stem.format(number), fontsize=11
                )
                y += 90
            page.insert_text((60, y + 20), "① a   ② b   ③ c", fontsize=11, fontname="korea")
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=200,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.KOREAN,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            self.assertEqual(
                [9, 10, 11, 1, 2],
                [
                    problem.metadata.get("problem_number")
                    for problem in page_model.problems
                    if problem.metadata.get("problem_number") is not None
                ],
            )
            self.assertEqual(0, page_model.metadata.get("pdf_nested_enumeration_marker_count"))

    def test_pdf_passage_range_block_stops_before_cross_column_child_questions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "passage_range_cross_column_children.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((48, 92), "[1~2] 다음 글을 읽고 물음에 답하시오.", fontsize=14)
            page.insert_text((48, 138), "shared passage first line", fontsize=12)
            page.insert_text((48, 170), "shared passage second line", fontsize=12)
            page.insert_text((330, 220), "1. first question in right column", fontsize=14)
            page.insert_text((330, 360), "2. second question in right column", fontsize=14)
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.ENGLISH,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            shared_block = next(
                block for block in page_model.blocks if block.metadata.get("segmenter") == "pdf-passage-range"
            )
            first_problem_block = next(
                block for block in page_model.blocks if block.metadata.get("problem_number") == 1
            )
            self.assertLess(shared_block.bbox.bottom, first_problem_block.bbox.top)

    def test_pdf_workbook_example_markers_ignore_section_headings_and_footer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "math_workbook_examples.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((32, 54), "1. 삼각비", fontsize=18)
            page.insert_text((250, 118), "#1. 삼각비의 뜻", fontsize=14)
            page.insert_text((32, 220), "ex) 다음 그림에서 x의 값을 구하시오.", fontsize=14)
            page.draw_rect(fitz.Rect(90, 270, 300, 390), color=(0, 0, 0), width=1)
            page.insert_text((120, 330), "45°", fontsize=14)
            page.insert_text((250, 440), "#2. 특수각", fontsize=14)
            page.insert_text((32, 520), "ex) 다음 표를 완성하시오.", fontsize=14)
            page.draw_rect(fitz.Rect(90, 570, 360, 680), color=(0, 0, 0), width=1)
            page.insert_text((32, 760), "중3-2 수학", fontsize=12)
            page.insert_text((280, 760), "- 1 -", fontsize=12)
            page.insert_text((455, 760), "YouTube - 친절한카수박", fontsize=12)
            doc.save(pdf_path)
            doc.close()

            prepared = prepare_source_pages(
                pdf_path,
                pdf_dpi=144,
                detect_perspective=False,
                deskew=True,
                crop_margins=True,
            )[0]
            page_model = build_page_model(
                prepared,
                subject=Subject.MATH,
                ocr_mode="none",
                ai_config=build_ai_fallback_config(mode="off"),
            )

            self.assertEqual("pdf-example-markers", page_model.metadata.get("segmenter"))
            self.assertEqual(2, len(page_model.problems))
            self.assertEqual(
                [None, None],
                [problem.metadata.get("problem_number") for problem in page_model.problems],
            )
            for problem in page_model.problems:
                self.assertTrue(problem.figure_block_ids)

            footer_top = prepared.image.height * 0.9
            self.assertTrue(all(block.bbox.bottom < footer_top for block in page_model.blocks))

    def test_pdf_render_uses_external_pymupdf_when_module_missing(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            pdf_path = Path(temp_dir) / "single_problem.pdf"
            doc = fitz.open()
            page = doc.new_page(width=300, height=240)
            page.insert_text((48, 80), "1. problem stem", fontsize=14)
            doc.save(pdf_path)
            doc.close()

            original_fitz = preprocess_module.fitz
            preprocess_module.fitz = None
            try:
                pages = preprocess_module.render_pdf_pages(
                    pdf_path,
                    Path(temp_dir) / "rendered",
                    dpi=72,
                )
            finally:
                preprocess_module.fitz = original_fitz

            self.assertEqual(1, len(pages))
            self.assertTrue(Path(pages[0].normalized_path).exists())
            self.assertEqual("external_pymupdf", pages[0].metadata.get("pdf_renderer"))
            self.assertEqual(1, len(pages[0].metadata.get("pdf_problem_markers") or []))

    def test_external_pymupdf_candidates_include_local_posix_venv(self):
        candidates = preprocess_module._iter_external_pymupdf_python_candidates()
        expected = Path(preprocess_module.__file__).resolve().parent / ".venv" / "bin" / "python"

        self.assertIn(expected, candidates)

    def test_pdf_problem_markers_ignore_chrome_print_date_header(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "chrome_header.pdf"
            doc = fitz.open()
            page = doc.new_page(width=300, height=400)
            page.insert_text((8, 18), "26. 6. 13. 오후 12:51", fontsize=8)
            page.insert_text((48, 120), "1. real problem stem", fontsize=14)
            doc.save(pdf_path)
            doc.close()

            pages = preprocess_module.render_pdf_pages(
                pdf_path,
                Path(temp_dir) / "rendered",
                dpi=72,
            )

            markers = pages[0].metadata.get("pdf_problem_markers") or []
            self.assertEqual([1], [marker.get("number") for marker in markers])

    def test_pdf_problem_markers_ignore_decimal_measurement_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "decimal_measurement.pdf"
            doc = fitz.open()
            page = doc.new_page(width=300, height=400)
            page.insert_text((48, 120), "13. real problem stem", fontsize=14)
            page.insert_text((180, 220), "3.4 ㎛", fontsize=12)
            page.insert_text((48, 300), "14. next problem stem", fontsize=14)
            doc.save(pdf_path)
            doc.close()

            pages = preprocess_module.render_pdf_pages(
                pdf_path,
                Path(temp_dir) / "rendered",
                dpi=72,
            )

            markers = pages[0].metadata.get("pdf_problem_markers") or []
            self.assertEqual([13, 14], [marker.get("number") for marker in markers])

    def test_pdf_problem_markers_keep_year_started_problem_title(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "year_problem_title.pdf"
            doc = fitz.open()
            page = doc.new_page(width=360, height=420)
            page.insert_text((48, 120), "9. 2024 Grand Butterfly Circus", fontsize=14)
            page.insert_text((48, 260), "10. next problem stem", fontsize=14)
            doc.save(pdf_path)
            doc.close()

            pages = preprocess_module.render_pdf_pages(
                pdf_path,
                Path(temp_dir) / "rendered",
                dpi=72,
            )

            markers = pages[0].metadata.get("pdf_problem_markers") or []
            self.assertEqual([9, 10], [marker.get("number") for marker in markers])

    def test_pdf_marker_last_problem_excludes_isolated_footer_page_number(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.text((60, 120), "1. problem stem", fill=(20, 20, 20))
        draw.text((60, 320), "2. problem stem", fill=(20, 20, 20))
        draw.rectangle((95, 380, 245, 500), outline=(20, 20, 20), width=2)
        draw.text((286, 760), "- 3 -", fill=(20, 20, 20))

        class Source:
            def __init__(self, source_image):
                self.image = source_image
                self.metadata = {
                    "source_type": "pdf",
                    "pdf_problem_markers": [
                        {
                            "number": 1,
                            "text": "1. problem stem",
                            "bbox": {"left": 60, "top": 116, "right": 152, "bottom": 134},
                        },
                        {
                            "number": 2,
                            "text": "2. problem stem",
                            "bbox": {"left": 60, "top": 316, "right": 152, "bottom": 334},
                        },
                    ],
                }
                self.source_path = "synthetic-footer.pdf"

        segmented = segment_page(Source(image), page_id="footer-page", subject=Subject.MATH)

        self.assertEqual("pdf-text-markers", segmented.metadata.get("segmenter"))
        self.assertEqual(2, len(segmented.blocks))
        self.assertLess(segmented.blocks[-1].bbox.bottom, 735)

    def test_pdf_marker_last_problem_keeps_real_choices_near_page_bottom(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.text((60, 120), "1. problem stem", fill=(20, 20, 20))
        draw.text((60, 320), "2. problem stem", fill=(20, 20, 20))
        draw.rectangle((95, 380, 245, 500), outline=(20, 20, 20), width=2)
        draw.text((70, 742), "① a        ② b", fill=(20, 20, 20))
        draw.text((70, 766), "③ c        ④ d", fill=(20, 20, 20))

        class Source:
            def __init__(self, source_image):
                self.image = source_image
                self.metadata = {
                    "source_type": "pdf",
                    "pdf_problem_markers": [
                        {
                            "number": 1,
                            "text": "1. problem stem",
                            "bbox": {"left": 60, "top": 116, "right": 152, "bottom": 134},
                        },
                        {
                            "number": 2,
                            "text": "2. problem stem",
                            "bbox": {"left": 60, "top": 316, "right": 152, "bottom": 334},
                        },
                    ],
                }
                self.source_path = "synthetic-bottom-choices.pdf"

        segmented = segment_page(Source(image), page_id="bottom-choices-page", subject=Subject.MATH)

        self.assertEqual("pdf-text-markers", segmented.metadata.get("segmenter"))
        self.assertEqual(2, len(segmented.blocks))
        self.assertGreater(segmented.blocks[-1].bbox.bottom, 780)

    def test_single_right_column_pdf_marker_does_not_include_left_passage(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.line((300, 72, 300, 728), fill=(20, 20, 20), width=2)
        for y in range(110, 520, 30):
            draw.text((58, y), "left passage text", fill=(20, 20, 20))
        draw.text((330, 120), "4. problem stem", fill=(20, 20, 20))
        draw.text((342, 190), "① a        ② b", fill=(20, 20, 20))
        draw.text((342, 222), "③ c        ④ d", fill=(20, 20, 20))

        class Source:
            def __init__(self, source_image):
                self.image = source_image
                self.metadata = {
                    "source_type": "pdf",
                    "pdf_problem_markers": [
                        {
                            "number": 4,
                            "text": "4. problem stem",
                            "bbox": {"left": 330, "top": 116, "right": 430, "bottom": 134},
                        }
                    ],
                    "pdf_text_lines": [
                        {
                            "text": "4. problem stem",
                            "bbox": {"left": 330, "top": 116, "right": 430, "bottom": 134},
                        },
                        {
                            "text": "① a        ② b",
                            "bbox": {"left": 342, "top": 186, "right": 430, "bottom": 204},
                        },
                        {
                            "text": "③ c        ④ d",
                            "bbox": {"left": 342, "top": 218, "right": 430, "bottom": 236},
                        },
                    ],
                }
                self.source_path = "synthetic-right-column.pdf"

        segmented = segment_page(Source(image), page_id="right-column-page", subject=Subject.KOREAN)

        self.assertEqual("pdf-text-markers", segmented.metadata.get("segmenter"))
        self.assertEqual(1, len(segmented.blocks))
        block = segmented.blocks[0]
        self.assertEqual(2, block.metadata.get("column_index"))
        self.assertTrue(block.metadata.get("visual_column_bounds_used"))
        self.assertGreater(block.bbox.left, 285)
        self.assertLess(block.bbox.width, 315)

    def test_pdf_marker_terminal_problem_stops_after_last_choice_text_line(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.text((60, 120), "13. problem stem", fill=(20, 20, 20))
        draw.text((72, 190), "① a        ② b", fill=(20, 20, 20))
        draw.text((72, 222), "③ c        ④ d", fill=(20, 20, 20))
        draw.text((92, 254), "continued choice text", fill=(20, 20, 20))
        for y in range(430, 720, 30):
            draw.text((58, y), "following passage should not be included", fill=(20, 20, 20))

        class Source:
            def __init__(self, source_image):
                self.image = source_image
                self.metadata = {
                    "source_type": "pdf",
                    "pdf_problem_markers": [
                        {
                            "number": 13,
                            "text": "13. problem stem",
                            "bbox": {"left": 60, "top": 116, "right": 170, "bottom": 134},
                        }
                    ],
                    "pdf_text_lines": [
                        {
                            "text": "13. problem stem",
                            "bbox": {"left": 60, "top": 116, "right": 170, "bottom": 134},
                        },
                        {
                            "text": "① a        ② b",
                            "bbox": {"left": 72, "top": 186, "right": 180, "bottom": 204},
                        },
                        {
                            "text": "③ c        ④ d",
                            "bbox": {"left": 72, "top": 218, "right": 180, "bottom": 236},
                        },
                        {
                            "text": "continued choice text",
                            "bbox": {"left": 92, "top": 250, "right": 230, "bottom": 268},
                        },
                        {
                            "text": "following passage should not be included",
                            "bbox": {"left": 58, "top": 426, "right": 360, "bottom": 444},
                        },
                    ],
                }
                self.source_path = "synthetic-terminal-choice.pdf"

        segmented = segment_page(Source(image), page_id="terminal-choice-page", subject=Subject.KOREAN)

        self.assertEqual("pdf-text-markers", segmented.metadata.get("segmenter"))
        self.assertEqual(1, len(segmented.blocks))
        block = segmented.blocks[0]
        self.assertTrue(block.metadata.get("choice_bottom_trimmed"))
        self.assertGreater(block.bbox.bottom, 280)
        self.assertLess(block.bbox.bottom, 330)

    def test_pdf_marker_terminal_problem_trims_blank_tail_to_last_ink(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.text((60, 120), "29. problem stem", fill=(20, 20, 20))
        draw.text((72, 190), "formula line", fill=(20, 20, 20))
        draw.text((72, 250), "answer request", fill=(20, 20, 20))

        class Source:
            def __init__(self, source_image):
                self.image = source_image
                self.metadata = {
                    "source_type": "pdf",
                    "pdf_problem_markers": [
                        {
                            "number": 29,
                            "text": "29. problem stem",
                            "bbox": {"left": 60, "top": 116, "right": 180, "bottom": 134},
                        }
                    ],
                    "pdf_text_lines": [
                        {
                            "text": "29. problem stem",
                            "bbox": {"left": 60, "top": 116, "right": 180, "bottom": 134},
                        },
                        {
                            "text": "formula line",
                            "bbox": {"left": 72, "top": 186, "right": 170, "bottom": 204},
                        },
                        {
                            "text": "answer request",
                            "bbox": {"left": 72, "top": 246, "right": 190, "bottom": 264},
                        },
                    ],
                }
                self.source_path = "synthetic-terminal-blank-tail.pdf"

        segmented = segment_page(Source(image), page_id="terminal-blank-tail-page", subject=Subject.MATH)

        self.assertEqual("pdf-text-markers", segmented.metadata.get("segmenter"))
        self.assertEqual(1, len(segmented.blocks))
        block = segmented.blocks[0]
        self.assertLess(block.bbox.bottom, 320)
        self.assertEqual(1, segmented.metadata.get("pdf_content_bottom_trim_count"))

    def test_pdf_marker_choice_trim_keeps_visual_diagram_below_choices(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        choice_line = "    ".join(PDF_CHOICE_MARKERS)
        draw.text((60, 80), "8. geometry problem stem", fill=(20, 20, 20))
        draw.text((72, 150), "given conditions", fill=(20, 20, 20))
        draw.text((72, 230), choice_line, fill=(20, 20, 20))
        draw.ellipse((155, 355, 445, 645), outline=(20, 20, 20), width=2)
        draw.arc((155, 430, 445, 555), 0, 180, fill=(20, 20, 20), width=2)
        draw.line((190, 450, 410, 560), fill=(20, 20, 20), width=2)

        class Source:
            def __init__(self, source_image):
                self.image = source_image
                self.metadata = {
                    "source_type": "pdf",
                    "pdf_problem_markers": [
                        {
                            "number": 8,
                            "text": "8. geometry problem stem",
                            "bbox": {"left": 60, "top": 76, "right": 220, "bottom": 94},
                        }
                    ],
                    "pdf_text_lines": [
                        {
                            "text": "8. geometry problem stem",
                            "bbox": {"left": 60, "top": 76, "right": 220, "bottom": 94},
                        },
                        {
                            "text": "given conditions",
                            "bbox": {"left": 72, "top": 146, "right": 200, "bottom": 164},
                        },
                        {
                            "text": choice_line,
                            "bbox": {"left": 72, "top": 226, "right": 360, "bottom": 244},
                        },
                    ],
                }
                self.source_path = "synthetic-choice-diagram-tail.pdf"

        segmented = segment_page(Source(image), page_id="choice-diagram-tail-page", subject=Subject.MATH)

        self.assertEqual("pdf-text-markers", segmented.metadata.get("segmenter"))
        self.assertEqual(1, len(segmented.blocks))
        block = segmented.blocks[0]
        self.assertTrue(block.metadata.get("choice_bottom_trimmed"))
        self.assertTrue(block.metadata.get("choice_visual_tail_attached"))
        self.assertGreater(block.bbox.bottom, 650)

    def test_pdf_marker_choice_trim_keeps_thin_math_graph_below_choices(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        choice_line = "    ".join(PDF_CHOICE_MARKERS)
        draw.text((60, 80), "9. graph problem stem", fill=(20, 20, 20))
        draw.text((72, 150), "choose the matching graph", fill=(20, 20, 20))
        draw.text((72, 230), choice_line, fill=(20, 20, 20))
        draw.line((155, 346, 445, 346), fill=(20, 20, 20), width=2)
        draw.line((300, 332, 300, 360), fill=(20, 20, 20), width=2)
        draw.line((220, 354, 380, 338), fill=(20, 20, 20), width=2)

        class Source:
            def __init__(self, source_image):
                self.image = source_image
                self.metadata = {
                    "source_type": "pdf",
                    "pdf_problem_markers": [
                        {
                            "number": 9,
                            "text": "9. graph problem stem",
                            "bbox": {"left": 60, "top": 76, "right": 220, "bottom": 94},
                        }
                    ],
                    "pdf_text_lines": [
                        {
                            "text": "9. graph problem stem",
                            "bbox": {"left": 60, "top": 76, "right": 220, "bottom": 94},
                        },
                        {
                            "text": "choose the matching graph",
                            "bbox": {"left": 72, "top": 146, "right": 230, "bottom": 164},
                        },
                        {
                            "text": choice_line,
                            "bbox": {"left": 72, "top": 226, "right": 360, "bottom": 244},
                        },
                    ],
                }
                self.source_path = "synthetic-choice-thin-graph-tail.pdf"

        segmented = segment_page(Source(image), page_id="choice-thin-graph-tail-page", subject=Subject.MATH)

        self.assertEqual("pdf-text-markers", segmented.metadata.get("segmenter"))
        self.assertEqual(1, len(segmented.blocks))
        block = segmented.blocks[0]
        self.assertTrue(block.metadata.get("choice_bottom_trimmed"))
        self.assertTrue(block.metadata.get("choice_visual_tail_attached"))
        self.assertGreater(block.bbox.bottom, 375)

    def test_pdf_marker_choice_visual_tail_ignores_column_rule_below_choices(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        choice_line = "    ".join(PDF_CHOICE_MARKERS)
        draw.text((60, 80), "10. graph problem stem", fill=(20, 20, 20))
        draw.text((72, 150), "choose the matching graph", fill=(20, 20, 20))
        draw.text((72, 230), choice_line, fill=(20, 20, 20))
        draw.line((155, 346, 445, 346), fill=(20, 20, 20), width=2)
        draw.line((300, 332, 300, 360), fill=(20, 20, 20), width=2)
        draw.line((220, 354, 380, 338), fill=(20, 20, 20), width=2)
        draw.line((540, 0, 540, 799), fill=(20, 20, 20), width=2)

        class Source:
            def __init__(self, source_image):
                self.image = source_image
                self.metadata = {
                    "source_type": "pdf",
                    "pdf_problem_markers": [
                        {
                            "number": 10,
                            "text": "10. graph problem stem",
                            "bbox": {"left": 60, "top": 76, "right": 220, "bottom": 94},
                        }
                    ],
                    "pdf_text_lines": [
                        {
                            "text": "10. graph problem stem",
                            "bbox": {"left": 60, "top": 76, "right": 220, "bottom": 94},
                        },
                        {
                            "text": "choose the matching graph",
                            "bbox": {"left": 72, "top": 146, "right": 230, "bottom": 164},
                        },
                        {
                            "text": choice_line,
                            "bbox": {"left": 72, "top": 226, "right": 360, "bottom": 244},
                        },
                    ],
                }
                self.source_path = "synthetic-choice-column-rule-tail.pdf"

        segmented = segment_page(Source(image), page_id="choice-column-rule-tail-page", subject=Subject.MATH)

        self.assertEqual("pdf-text-markers", segmented.metadata.get("segmenter"))
        self.assertEqual(1, len(segmented.blocks))
        block = segmented.blocks[0]
        self.assertTrue(block.metadata.get("choice_bottom_trimmed"))
        self.assertTrue(block.metadata.get("choice_visual_tail_attached"))
        self.assertGreater(block.bbox.bottom, 375)
        self.assertLess(block.bbox.bottom, 450)

    def test_pdf_marker_choice_visual_tail_ignores_detached_footer(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        choice_line = "    ".join(PDF_CHOICE_MARKERS)
        draw.text((60, 80), "11. graph problem stem", fill=(20, 20, 20))
        draw.text((72, 150), "choose the matching graph", fill=(20, 20, 20))
        draw.text((72, 230), choice_line, fill=(20, 20, 20))
        draw.line((155, 346, 445, 346), fill=(20, 20, 20), width=2)
        draw.line((300, 332, 300, 360), fill=(20, 20, 20), width=2)
        draw.line((220, 354, 380, 338), fill=(20, 20, 20), width=2)
        draw.text((500, 760), "11 / 20", fill=(20, 20, 20))

        class Source:
            def __init__(self, source_image):
                self.image = source_image
                self.metadata = {
                    "source_type": "pdf",
                    "pdf_problem_markers": [
                        {
                            "number": 11,
                            "text": "11. graph problem stem",
                            "bbox": {"left": 60, "top": 76, "right": 220, "bottom": 94},
                        }
                    ],
                    "pdf_text_lines": [
                        {
                            "text": "11. graph problem stem",
                            "bbox": {"left": 60, "top": 76, "right": 220, "bottom": 94},
                        },
                        {
                            "text": "choose the matching graph",
                            "bbox": {"left": 72, "top": 146, "right": 230, "bottom": 164},
                        },
                        {
                            "text": choice_line,
                            "bbox": {"left": 72, "top": 226, "right": 360, "bottom": 244},
                        },
                    ],
                }
                self.source_path = "synthetic-choice-detached-footer-tail.pdf"

        segmented = segment_page(Source(image), page_id="choice-detached-footer-tail-page", subject=Subject.MATH)

        self.assertEqual("pdf-text-markers", segmented.metadata.get("segmenter"))
        self.assertEqual(1, len(segmented.blocks))
        block = segmented.blocks[0]
        self.assertTrue(block.metadata.get("choice_bottom_trimmed"))
        self.assertTrue(block.metadata.get("choice_visual_tail_attached"))
        self.assertGreater(block.bbox.bottom, 375)
        self.assertLess(block.bbox.bottom, 450)

    def test_pdf_text_stem_markers_segment_without_problem_numbers(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.text((60, 120), "다음 자료에 대한 설명으로 옳은 것은?", fill=(20, 20, 20))
        draw.rectangle((95, 170, 245, 250), outline=(20, 20, 20), width=2)
        draw.text((60, 340), "밑줄 친 ㉠에 대한 설명으로 옳은 것은?", fill=(20, 20, 20))
        draw.text((70, 520), "① a        ② b", fill=(20, 20, 20))

        class Source:
            def __init__(self, source_image):
                self.image = source_image
                self.metadata = {
                    "source_type": "hwp",
                    "pdf_problem_markers": [
                        {
                            "marker_kind": "text_stem",
                            "text": "다음 자료에 대한 설명으로 옳은 것은?",
                            "bbox": {"left": 60, "top": 116, "right": 260, "bottom": 134},
                        },
                        {
                            "marker_kind": "text_stem",
                            "text": "밑줄 친 ㉠에 대한 설명으로 옳은 것은?",
                            "bbox": {"left": 60, "top": 336, "right": 292, "bottom": 354},
                        },
                    ],
                }
                self.source_path = "synthetic-stems.pdf"

        segmented = segment_page(Source(image), page_id="stem-page", subject=Subject.SOCIAL)

        self.assertEqual("pdf-text-markers", segmented.metadata.get("segmenter"))
        self.assertEqual(2, len(segmented.blocks))
        self.assertEqual([None, None], [block.metadata.get("problem_number") for block in segmented.blocks])
        self.assertEqual(
            ["text_stem", "text_stem"],
            [block.metadata.get("problem_number_source") for block in segmented.blocks],
        )

    def test_hwp_layout_markers_ignore_near_zero_height_problem_numbers(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.text((330, 96), "33. real problem stem", fill=(20, 20, 20))
        draw.rectangle((365, 160, 500, 250), outline=(20, 20, 20), width=2)
        draw.text((330, 320), "34. real problem stem", fill=(20, 20, 20))
        draw.rectangle((365, 380, 500, 480), outline=(20, 20, 20), width=2)

        class Source:
            def __init__(self, source_image):
                self.image = source_image
                self.metadata = {
                    "source_type": "hwp",
                    "pdf_problem_markers": [
                        {
                            "number": 32,
                            "text": "32. hidden problem marker from split passage",
                            "marker_kind": "hwp_layout_number",
                            "bbox": {
                                "left": 60,
                                "top": 791.5,
                                "right": 150,
                                "bottom": 792.0,
                            },
                        },
                        {
                            "number": 33,
                            "text": "33. real problem stem",
                            "marker_kind": "hwp_layout_number",
                            "bbox": {"left": 330, "top": 92, "right": 450, "bottom": 112},
                        },
                        {
                            "number": 34,
                            "text": "34. real problem stem",
                            "marker_kind": "hwp_layout_number",
                            "bbox": {"left": 330, "top": 316, "right": 450, "bottom": 336},
                        },
                    ],
                }
                self.source_path = "synthetic-hwp-layout.pdf"

        segmented = segment_page(Source(image), page_id="hwp-layout-page", subject=Subject.KOREAN)

        self.assertEqual("pdf-text-markers", segmented.metadata.get("segmenter"))
        self.assertEqual([33, 34], [block.metadata.get("problem_number") for block in segmented.blocks])
        self.assertEqual(1, segmented.metadata.get("ignored_tiny_pdf_marker_count"))
        self.assertEqual([2, 2], [block.metadata.get("column_index") for block in segmented.blocks])
        self.assertGreaterEqual(min(block.bbox.left for block in segmented.blocks), 300.0)
        self.assertGreaterEqual(min(block.bbox.height for block in segmented.blocks), 40.0)

    def test_hwp_layout_markers_ignore_off_page_problem_numbers(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.text((330, 96), "33. real problem stem", fill=(20, 20, 20))
        draw.rectangle((365, 160, 500, 250), outline=(20, 20, 20), width=2)
        draw.text((330, 320), "34. real problem stem", fill=(20, 20, 20))
        draw.rectangle((365, 380, 500, 480), outline=(20, 20, 20), width=2)

        class Source:
            def __init__(self, source_image):
                self.image = source_image
                self.metadata = {
                    "source_type": "hwp",
                    "pdf_problem_markers": [
                        {
                            "number": 32,
                            "text": "32. off-page problem marker from split passage",
                            "marker_kind": "hwp_layout_number",
                            "bbox": {
                                "left": 60,
                                "top": 812.0,
                                "right": 150,
                                "bottom": 850.0,
                            },
                        },
                        {
                            "number": 33,
                            "text": "33. real problem stem",
                            "marker_kind": "hwp_layout_number",
                            "bbox": {"left": 330, "top": 92, "right": 450, "bottom": 112},
                        },
                        {
                            "number": 34,
                            "text": "34. real problem stem",
                            "marker_kind": "hwp_layout_number",
                            "bbox": {"left": 330, "top": 316, "right": 450, "bottom": 336},
                        },
                    ],
                }
                self.source_path = "synthetic-hwp-layout.pdf"

        segmented = segment_page(Source(image), page_id="hwp-off-page-layout", subject=Subject.KOREAN)

        self.assertEqual("pdf-text-markers", segmented.metadata.get("segmenter"))
        self.assertEqual([33, 34], [block.metadata.get("problem_number") for block in segmented.blocks])
        self.assertEqual(1, segmented.metadata.get("ignored_tiny_pdf_marker_count"))
        self.assertEqual([32], segmented.metadata.get("ignored_tiny_pdf_marker_numbers"))
        self.assertEqual([2, 2], [block.metadata.get("column_index") for block in segmented.blocks])
        self.assertGreaterEqual(min(block.bbox.left for block in segmented.blocks), 300.0)

    def test_pdf_bodyless_passage_range_header_still_starts_a_passage(self):
        """A bracketed range header is a passage of its own, body or not.

        docs/web-trial-quality.md records the project convention: a bracketed
        range header is recorded as a passage unit whether or not the material
        it governs is printed on the page (a listening script is not). The
        geometry here is the measured one from
        ``english_2020suneung_go3_20191107`` page 2 of the trial bench (a
        1881x2766 page): the header sits just under question 15's last choice
        line and just above question 16's marker. At that spacing the header's
        own fragment is only 50.0px tall against the page-relative minimum of
        55.3px (2% of the 2766px page height -- the fixed 40.0px floor is not
        what is active at this page size), and the header line is inside the
        choice list's continuation gap, so the header used to produce no
        passage block at all and to be cropped into question 15 instead --
        while the sister paper ``english_go2_hakpyeong_20260324``, whose
        header is a few pixels further from its question 16 (56.1px against a
        55.5px threshold), produced the passage correctly without the fix
        below.

        The ink is drawn in latin script because PIL's default font carries no
        Hangul glyphs; the text layer shape the detector keys on (a bracketed
        range plus a shared-material cue) is the same in both languages.
        """
        image = Image.new("RGB", (1881, 2766), "white")
        draw = ImageDraw.Draw(image)
        choice_line = "    ".join(PDF_CHOICE_MARKERS)
        header_text = "[16 ~ 17] Listen to the following and answer the questions."
        draw.text((60, 1850), "15. previous question stem", fill=(20, 20, 20))
        draw.text((90, 1900), choice_line, fill=(20, 20, 20))
        draw.text((60, 1973), header_text, fill=(20, 20, 20))
        draw.text((60, 2031), "16. main topic question", fill=(20, 20, 20))
        draw.text((60, 2400), "17. not mentioned question", fill=(20, 20, 20))

        class Source:
            def __init__(self, source_image):
                self.image = source_image
                self.metadata = {
                    "source_type": "pdf",
                    "pdf_problem_markers": [
                        {
                            "number": 15,
                            "text": "15. previous question stem",
                            "bbox": {"left": 60, "top": 1850.0, "right": 790, "bottom": 1886.0},
                        },
                        {
                            "number": 16,
                            "text": "16. main topic question",
                            "bbox": {"left": 60, "top": 2031.9, "right": 800, "bottom": 2067.9},
                        },
                        {
                            "number": 17,
                            "text": "17. not mentioned question",
                            "bbox": {"left": 60, "top": 2400.0, "right": 850, "bottom": 2436.0},
                        },
                    ],
                    "pdf_text_lines": [
                        {
                            "text": "15. previous question stem",
                            "bbox": {"left": 60, "top": 1850.0, "right": 790, "bottom": 1886.0},
                        },
                        {
                            "text": choice_line,
                            "bbox": {"left": 90, "top": 1900.0, "right": 1200, "bottom": 1936.0},
                        },
                        {
                            "text": header_text,
                            "bbox": {"left": 60, "top": 1973.6, "right": 1400, "bottom": 2010.9},
                        },
                        {
                            "text": "16. main topic question",
                            "bbox": {"left": 60, "top": 2031.9, "right": 800, "bottom": 2067.9},
                        },
                        {
                            "text": "17. not mentioned question",
                            "bbox": {"left": 60, "top": 2400.0, "right": 850, "bottom": 2436.0},
                        },
                    ],
                }
                self.source_path = "synthetic-bodyless-passage-header.pdf"

        segmented = segment_page(
            Source(image),
            page_id="bodyless-passage-header-page",
            subject=Subject.ENGLISH,
        )

        self.assertEqual("pdf-text-markers", segmented.metadata.get("segmenter"))
        self.assertEqual(1, segmented.metadata.get("pdf_passage_range_block_count"))
        passage_blocks = [
            block
            for block in segmented.blocks
            if block.metadata.get("segmenter") == "pdf-passage-range"
        ]
        self.assertEqual(1, len(passage_blocks))
        passage_block = passage_blocks[0]
        self.assertEqual({"start": 16, "end": 17}, passage_block.metadata.get("passage_range"))
        self.assertEqual([16, 17], passage_block.metadata.get("passage_child_marker_numbers"))
        # The crop must carry the whole header line, top and bottom.
        self.assertLessEqual(passage_block.bbox.top, 1973.6)
        self.assertGreaterEqual(passage_block.bbox.bottom, 2010.9)

        by_number = {
            block.metadata.get("problem_number"): block
            for block in segmented.blocks
            if block.metadata.get("problem_number") is not None
        }
        self.assertEqual({15, 16, 17}, set(by_number))
        # Question 15 stops above the header instead of swallowing it: the
        # header is not a continuation of question 15's choice list.
        self.assertLessEqual(by_number[15].bbox.bottom, 1973.6)
        self.assertTrue(by_number[15].metadata.get("choice_bottom_trimmed"))
        self.assertGreaterEqual(by_number[16].bbox.top, 2010.9)

    def test_pdf_bodyless_passage_range_header_kept_at_small_page_floor(self):
        """The fixed 40.0px floor, not the 2% term, is what is pinned here.

        This is the original (pre-measurement) synthetic geometry, kept as a
        second scenario: on a 600x1000 page the page-relative term
        (``image.height * 0.02`` = 20.0) is smaller than the fixed 40.0px
        floor, so ``max(40.0, image.height * 0.02)`` resolves to the constant
        40.0 and that is the term the header's 34.0px fragment is measured
        against. See
        test_pdf_bodyless_passage_range_header_still_starts_a_passage above
        for the companion scenario that pins the page-relative term instead.
        """
        image = Image.new("RGB", (600, 1000), "white")
        draw = ImageDraw.Draw(image)
        choice_line = "    ".join(PDF_CHOICE_MARKERS)
        header_text = "[16 ~ 17] Listen to the following and answer the questions."
        draw.text((60, 600), "15. previous question stem", fill=(20, 20, 20))
        draw.text((72, 700), choice_line, fill=(20, 20, 20))
        draw.text((60, 750), header_text, fill=(20, 20, 20))
        draw.text((60, 790), "16. main topic question", fill=(20, 20, 20))
        draw.text((60, 880), "17. not mentioned question", fill=(20, 20, 20))

        class Source:
            def __init__(self, source_image):
                self.image = source_image
                self.metadata = {
                    "source_type": "pdf",
                    "pdf_problem_markers": [
                        {
                            "number": 15,
                            "text": "15. previous question stem",
                            "bbox": {"left": 60, "top": 600, "right": 260, "bottom": 618},
                        },
                        {
                            "number": 16,
                            "text": "16. main topic question",
                            "bbox": {"left": 60, "top": 790, "right": 250, "bottom": 808},
                        },
                        {
                            "number": 17,
                            "text": "17. not mentioned question",
                            "bbox": {"left": 60, "top": 880, "right": 270, "bottom": 898},
                        },
                    ],
                    "pdf_text_lines": [
                        {
                            "text": "15. previous question stem",
                            "bbox": {"left": 60, "top": 600, "right": 260, "bottom": 618},
                        },
                        {
                            "text": choice_line,
                            "bbox": {"left": 72, "top": 700, "right": 360, "bottom": 718},
                        },
                        {
                            "text": header_text,
                            "bbox": {"left": 60, "top": 750, "right": 420, "bottom": 768},
                        },
                        {
                            "text": "16. main topic question",
                            "bbox": {"left": 60, "top": 790, "right": 250, "bottom": 808},
                        },
                        {
                            "text": "17. not mentioned question",
                            "bbox": {"left": 60, "top": 880, "right": 270, "bottom": 898},
                        },
                    ],
                }
                self.source_path = "synthetic-bodyless-passage-header-small.pdf"

        segmented = segment_page(
            Source(image),
            page_id="bodyless-passage-header-small-page",
            subject=Subject.ENGLISH,
        )

        self.assertEqual("pdf-text-markers", segmented.metadata.get("segmenter"))
        self.assertEqual(1, segmented.metadata.get("pdf_passage_range_block_count"))
        passage_blocks = [
            block
            for block in segmented.blocks
            if block.metadata.get("segmenter") == "pdf-passage-range"
        ]
        self.assertEqual(1, len(passage_blocks))
        passage_block = passage_blocks[0]
        self.assertEqual({"start": 16, "end": 17}, passage_block.metadata.get("passage_range"))
        self.assertLessEqual(passage_block.bbox.top, 750.0)
        self.assertGreaterEqual(passage_block.bbox.bottom, 768.0)

    def test_short_passage_continuation_fragment_is_still_dropped(self):
        """The header keeps its own short fragment; a same-height sliver stays dropped.

        On this 800px-tall page the fixed 40.0px floor -- not the 2% term
        (``image.height * 0.02`` = 16.0px here) -- is what a too-short
        fragment is measured against. Only the fragment that both (a) sits in
        the header's own column and (b) actually spans the header's own
        vertical midpoint is exempt from that floor.

        The column-2 sliver here is deliberately placed level with the header
        line itself, so its span also contains the header's midpoint (613):
        a version of this exemption that forgets to check the column would
        wrongly exempt this sliver too and keep it as a second block, even
        though it holds no part of the passage.
        """
        image = Image.new("RGB", (600, 800), "white")
        text_lines = [
            {
                "text": "[1~2] 다음 글을 읽고 물음에 답하시오.",
                "bbox": {"left": 40, "top": 600, "right": 280, "bottom": 626},
            },
            {
                "text": "직전 문항의 남은 한 줄",
                "bbox": {"left": 330, "top": 606, "right": 520, "bottom": 632},
            },
        ]
        right_markers = [
            {"number": 1, "bbox": {"left": 330, "top": 642, "right": 350, "bottom": 668}},
            {"number": 2, "bbox": {"left": 330, "top": 700, "right": 350, "bottom": 726}},
        ]

        blocks = _build_pdf_passage_range_blocks(
            image,
            "page-1",
            text_lines,
            [(1, [], (40.0, 285.0)), (2, right_markers, (315.0, 560.0))],
            page_area=float(image.width * image.height),
            start_index=1,
        )

        self.assertEqual(
            ["passage_range"],
            [block.metadata.get("marker_kind") for block in blocks],
        )
        self.assertEqual(1, blocks[0].metadata.get("passage_fragment_count"))
        self.assertLessEqual(blocks[0].bbox.top, 600.0)
        self.assertGreaterEqual(blocks[0].bbox.bottom, 626.0)
        self.assertEqual(1, blocks[0].metadata.get("column_index"))

        # Pin the containment bound itself: a marker immediately under the
        # header, in the header's OWN column, gives the header's fragment a
        # computed bottom (616) that lands above the header's own bottom edge
        # (626) but at/after its midpoint (613). The exemption must still
        # fire here -- a version that checks against the header's full bottom
        # instead of its midpoint would wrongly reject this fragment and drop
        # the block entirely.
        pin_text_lines = [
            {
                "text": "[3~4] 다음 글을 읽고 물음에 답하시오.",
                "bbox": {"left": 40, "top": 600, "right": 280, "bottom": 626},
            },
        ]
        pin_markers = [
            {"number": 3, "bbox": {"left": 60, "top": 630, "right": 80, "bottom": 656}},
            {"number": 4, "bbox": {"left": 60, "top": 700, "right": 80, "bottom": 726}},
        ]
        pin_blocks = _build_pdf_passage_range_blocks(
            image,
            "page-2",
            pin_text_lines,
            [(1, pin_markers, (40.0, 285.0))],
            page_area=float(image.width * image.height),
            start_index=1,
        )

        self.assertEqual(
            ["passage_range"],
            [block.metadata.get("marker_kind") for block in pin_blocks],
        )
        self.assertEqual({"start": 3, "end": 4}, pin_blocks[0].metadata.get("passage_range"))

    def test_trim_choice_continuation_ignores_in_question_range_look_alike(self):
        """A free-text line that merely looks like a range must not break the scan.

        ``_extract_pdf_passage_range`` accepts a bare leading range with no
        brackets, plus loose shared-material cues, so an in-question line such
        as a table caption ("1~3족 원소의 성질을...") can match it even though it
        starts no real passage -- the passage builder itself already guards
        against exporting a range like this as a header (its own
        preceding-number check). The choice-continuation scan must apply the
        same reasoning: only treat a match as a real header break when the
        claimed range actually starts after the question being trimmed.
        """
        image = Image.new("RGB", (600, 1000), "white")
        choice_line = "    ".join(PDF_CHOICE_MARKERS)
        false_positive_text = "1~3족 원소의 성질을 나타낸 표이다. 옳은 것을 <보기>에서 고르시오."
        # Confirms this is the same false-positive shape the finding names --
        # a bare in-question range, not a real shared-passage header.
        self.assertEqual((1, 3), _extract_pdf_passage_range(false_positive_text))

        text_lines = [
            {
                "text": choice_line,
                "bbox": {"left": 60, "top": 100, "right": 380, "bottom": 126},
            },
            {
                "text": false_positive_text,
                "bbox": {"left": 60, "top": 140, "right": 380, "bottom": 166},
            },
            {
                "text": "표를 보고 다음 물음에 답하시오.",
                "bbox": {"left": 60, "top": 180, "right": 380, "bottom": 206},
            },
        ]

        # Knowing the current question is 15, the range (1, 3) cannot be a
        # header governing questions below 15 -- it starts well before it --
        # so the scan must not break here and must keep scanning past it.
        fixed_bottom, fixed_trimmed, fixed_tail = _trim_pdf_problem_bottom_to_last_choice(
            image,
            text_lines,
            left=40.0,
            right=400.0,
            top=50.0,
            bottom=400.0,
            problem_number=15,
        )
        self.assertTrue(fixed_trimmed)
        self.assertFalse(fixed_tail)
        # The whole trailing content -- the look-alike line and the line after
        # it -- is retained in the crop, not orphaned outside every block.
        self.assertGreaterEqual(fixed_bottom, 206.0)
        self.assertEqual(224.0, fixed_bottom)

        # Without a known problem number the guard falls back to the old,
        # always-break behaviour (this is what the bug looked like before the
        # fix): the scan stops at the look-alike line's own top and its bottom
        # (140..166) lands split across two crops instead of belonging to
        # either -- exactly the "disappears from every output unit" defect.
        old_bottom, old_trimmed, _old_tail = _trim_pdf_problem_bottom_to_last_choice(
            image,
            text_lines,
            left=40.0,
            right=400.0,
            top=50.0,
            bottom=400.0,
        )
        self.assertTrue(old_trimmed)
        self.assertGreater(old_bottom, 140.0)
        self.assertLess(old_bottom, 166.0)


if __name__ == "__main__":
    unittest.main()
