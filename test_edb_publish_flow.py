import json
import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlparse
from urllib.request import url2pathname

from PIL import Image, ImageDraw

import build_problem_board_edb as problem_board
import edb_builder
from inspect_edb import parse_edb, parse_embedded_images
from app_server import (
    _problems_to_entries,
    _session_publish_history,
    _session_publish_blocking_preflight,
    _session_publish_summary,
    content_disposition_attachment,
    validate_edb_file,
)
from build_mvp_export import _render_problem_crops, build_ui_session as build_mvp_ui_session, run_export as run_mvp_export
from build_problem_board_edb import (
    ONE_PROBLEM_SLOT_HEIGHT_PAGES,
    PROCESSING_STEP_RECONSTRUCT,
    ProblemEntry,
    V1_DEFAULT_DISPLAY_WIDTH_PX,
    build_problem_entries,
    configure_problem_entries_for_export,
    build_ui_session as build_problem_ui_session,
    build_image_only_records,
    run_problem_export,
    split_problem_entries_for_classin_page_limit,
    write_classin_limited_edb_files,
    _pad_problem_crop_edges,
    _pad_problem_crop_bottom,
    _hwp_conversion_has_pdf_problem_markers,
    _trim_bottom_blue_watermark,
    _trim_edge_vertical_guides,
    _trim_source_page_chrome,
    _trim_text_priority_bottom_page_badge,
)
from edb_builder import CROP_FORMAT_V1
from layout_template_schema import LayoutTemplate, ProblemLayoutInput
from placement_engine import place_problems
from preprocess import PreparedPage
from structured_schema import BlockType, Box, ContentBlock, PageModel, ProblemUnit, Subject


def _path_from_file_uri(value: str) -> Path:
    parsed = urlparse(value)
    if parsed.scheme != "file":
        return Path(value)
    return Path(url2pathname(parsed.path))


class TestEdbPublishFlow(unittest.TestCase):
    def test_atomic_edb_write_preserves_existing_file_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            target = Path(raw_tmp) / "lesson.edb"
            target.write_bytes(b"known-good")
            with mock.patch.object(edb_builder.os, "replace", side_effect=OSError("locked")):
                with self.assertRaises(OSError):
                    edb_builder.write_edb(target, b"new")
            self.assertEqual(b"known-good", target.read_bytes())
            self.assertEqual([target], list(target.parent.iterdir()))

    def test_atomic_edb_write_uses_short_staging_name_for_long_destination(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            target = Path(raw_tmp) / f"{'a' * 250}.edb"
            edb_builder.write_edb(target, b"complete-edb")
            self.assertEqual(b"complete-edb", target.read_bytes())
            self.assertEqual([target], list(target.parent.iterdir()))

    def test_edb_builder_rejects_empty_record_set(self):
        with self.assertRaisesRegex(ValueError, "at least one record"):
            edb_builder.build_edb([], header_flag=3)

    def test_session_publish_preflight_blocks_duplicate_problem_ids(self):
        problems = [
            {"id": "same-id", "title": "1.", "riskFlags": []},
            {"id": "same-id", "title": "2.", "riskFlags": []},
        ]

        preflight, _duplicate_number_groups = _session_publish_blocking_preflight(
            problems,
            session={"input_intent": "page-as-is"},
        )

        self.assertFalse(preflight["passed"])
        duplicate_id_issues = [
            issue for issue in preflight["issues"] if issue["type"] == "duplicate_problem_id"
        ]
        self.assertEqual(1, len(duplicate_id_issues))
        self.assertEqual("same-id", duplicate_id_issues[0]["problemId"])
        self.assertEqual(2, duplicate_id_issues[0]["occurrenceCount"])

    def test_hwp_layout_problem_markers_count_as_marker_document_signal(self):
        self.assertTrue(
            _hwp_conversion_has_pdf_problem_markers(
                {
                    "source_type": "hwp",
                    "hwp_conversion_quality": {
                        "hwp_layout_problem_marker_count": 56,
                        "hwp_layout_problem_markers": [
                            {"pageIndex": 0, "number": 1},
                        ],
                    },
                }
            )
        )

    def test_build_problem_entries_parallelizes_cutout_generation_without_reordering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            image = Image.new("RGB", (420, 520), "white")
            draw = ImageDraw.Draw(image)
            draw.text((52, 62), "1. first", fill="black")
            draw.text((52, 282), "2. second", fill="black")
            image.save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.open(source).convert("RGB"),
                original_size=(420, 520),
            )
            blocks = [
                ContentBlock(
                    block_id="b-1",
                    block_type=BlockType.STEM,
                    bbox=Box(40, 40, 240, 130),
                    reading_order=0,
                    text="1. first",
                ),
                ContentBlock(
                    block_id="b-2",
                    block_type=BlockType.STEM,
                    bbox=Box(40, 260, 240, 130),
                    reading_order=1,
                    text="2. second",
                ),
            ]
            page = PageModel(
                page_id="page-1",
                width_px=420,
                height_px=520,
                subject=Subject.KOREAN,
                source_path=str(source),
                blocks=blocks,
                problems=[
                    ProblemUnit(
                        unit_id="problem-1",
                        subject=Subject.KOREAN,
                        title="1.",
                        stem_block_ids=["b-1"],
                        metadata={"problem_number": 1},
                    ),
                    ProblemUnit(
                        unit_id="problem-2",
                        subject=Subject.KOREAN,
                        title="2.",
                        stem_block_ids=["b-2"],
                        metadata={"problem_number": 2},
                    ),
                ],
            )
            barrier = threading.Barrier(2)
            lock = threading.Lock()
            calls = 0

            def fake_cutout(crop, *, chalk_color=None, text_priority=False):
                nonlocal calls
                with lock:
                    calls += 1
                    call_index = calls
                if call_index <= 2:
                    barrier.wait(timeout=0.8)
                return crop.convert("RGBA")

            with mock.patch.object(problem_board, "_extract_problem_cutout", side_effect=fake_cutout):
                entries = build_problem_entries(
                    [prepared],
                    [page],
                    root / "out",
                    LayoutTemplate(name="academy-default"),
                )

            self.assertEqual(["problem-1", "problem-2"], [entry.problem_id for entry in entries])
            self.assertTrue(all(entry.crop_path.exists() for entry in entries))
            self.assertTrue(all(entry.board_render_path.exists() for entry in entries))

    def test_build_problem_entries_treats_shared_passage_as_supplemental_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            image = Image.new("RGB", (900, 1400), "white")
            draw = ImageDraw.Draw(image)
            draw.text((72, 52), "[13~14] passage", fill="black")
            draw.text((72, 112), "shared passage body", fill="black")
            draw.text((72, 522), "13. child question", fill="black")
            draw.text((72, 682), "14. child question", fill="black")
            image.save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.open(source).convert("RGB"),
                original_size=(900, 1400),
            )
            blocks = [
                ContentBlock(
                    block_id="range-header",
                    block_type=BlockType.STEM,
                    bbox=Box(60, 40, 520, 40),
                    reading_order=0,
                    text="[13~14] 다음 글을 읽고 물음에 답하시오.",
                ),
                ContentBlock(
                    block_id="shared-passage",
                    block_type=BlockType.STEM,
                    bbox=Box(60, 100, 520, 340),
                    reading_order=1,
                    text="shared passage",
                ),
                ContentBlock(
                    block_id="q13",
                    block_type=BlockType.STEM,
                    bbox=Box(60, 500, 420, 80),
                    reading_order=2,
                    text="13. child question",
                ),
                ContentBlock(
                    block_id="q14",
                    block_type=BlockType.STEM,
                    bbox=Box(60, 660, 420, 80),
                    reading_order=3,
                    text="14. child question",
                ),
            ]
            shared_metadata = {
                "passage_group_id": "page-1-passage-13-14",
                "passage_range": {"start": 13, "end": 14},
                "shared_passage_block_ids": ["range-header", "shared-passage"],
                "passage_child_problem_numbers": [13, 14],
            }
            page = PageModel(
                page_id="page-1",
                width_px=900,
                height_px=1400,
                subject=Subject.KOREAN,
                source_path=str(source),
                blocks=blocks,
                problems=[
                    ProblemUnit(
                        unit_id="page-1-passage-13-14",
                        subject=Subject.KOREAN,
                        title="지문 13~14",
                        stem_block_ids=["range-header", "shared-passage"],
                        metadata={
                            **shared_metadata,
                            "passage_role": "passage_fragment",
                            "supplemental_item": True,
                        },
                    ),
                    ProblemUnit(
                        unit_id="page-1-problem-13",
                        subject=Subject.KOREAN,
                        title="13.",
                        stem_block_ids=["q13"],
                        metadata={
                            **shared_metadata,
                            "problem_number": 13,
                            "passage_role": "child_question",
                        },
                    ),
                    ProblemUnit(
                        unit_id="page-1-problem-14",
                        subject=Subject.KOREAN,
                        title="14.",
                        stem_block_ids=["q14"],
                        metadata={
                            **shared_metadata,
                            "problem_number": 14,
                            "passage_role": "child_question",
                        },
                    ),
                ],
            )

            with mock.patch.object(problem_board, "_extract_problem_cutout", side_effect=lambda crop, **_kwargs: crop.convert("RGBA")):
                entries = build_problem_entries([prepared], [page], root / "out", LayoutTemplate(name="academy-default"))

            self.assertEqual(
                ["page-1-passage-13-14", "page-1-problem-13", "page-1-problem-14"],
                [entry.problem_id for entry in entries],
            )
            self.assertIsNone(entries[0].problem_number)
            self.assertEqual("지문 13~14", entries[0].title)
            self.assertEqual(["range-header", "shared-passage"], [block.block_id for block in entries[0].blocks])
            self.assertEqual(["q13"], [block.block_id for block in entries[1].blocks])
            self.assertGreater(entries[1].bounds.top, 440.0)

            placements = [
                {
                    "problem_id": entry.problem_id,
                    "title": entry.title,
                    "problem_number": entry.problem_number,
                    "subject": entry.subject.value,
                    "source_page_id": entry.source_page_id,
                    "source_path": entry.source_path,
                    "crop_path": str(entry.crop_path),
                    "board_render_path": str(entry.board_render_path),
                    "actual_content_height_pages": entry.actual_height_pages,
                    "overflow_allowed": entry.overflow_allowed,
                    "overflow_violation": False,
                    "overflow_amount_pages": 0.0,
                    "slot_span_count": 1,
                    "start_y_pages": index * 1.2,
                    "snapped_next_start_y_pages": (index + 1) * 1.2,
                    "placement_x_ratio": 0.0,
                    "placement_y_ratio": 0.0,
                    "placement_scale_ratio": 1.0,
                    "record_mode": "image-only",
                    "processing_step": entry.processing_step,
                    "text_record_count": 0,
                    "image_record_count": 1,
                    "bbox": {
                        "left": entry.bounds.left,
                        "top": entry.bounds.top,
                        "width": entry.bounds.width,
                        "height": entry.bounds.height,
                    },
                    "risk_flags": entry.risk_flags,
                }
                for index, entry in enumerate(entries)
            ]
            ui_session = build_problem_ui_session(
                [prepared],
                placements,
                root / "out",
                None,
                [source],
                record_mode="image-only",
                pages=[page],
            )

            self.assertEqual(3, ui_session["detected_problem_count"])
            self.assertEqual(2, ui_session["core_problem_count"])
            self.assertEqual(1, ui_session["supplemental_item_count"])
            fragment = next(problem for problem in ui_session["problems"] if problem["id"] == "page-1-passage-13-14")
            self.assertEqual("passage_fragment", fragment["passageRole"])
            self.assertEqual(fragment["imagePath"], fragment["originalImagePath"])

    def test_page_as_is_problem_entries_default_to_chalk_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "page.png"
            Image.new("RGB", (640, 900), "white").save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.open(source).convert("RGB"),
                original_size=(640, 900),
            )
            block = ContentBlock(
                block_id="b-1",
                block_type=BlockType.STEM,
                bbox=Box(80, 120, 420, 280),
                reading_order=0,
                text="1. full page problem",
            )
            page = PageModel(
                page_id="page-1",
                width_px=640,
                height_px=900,
                subject=Subject.MATH,
                source_path=str(source),
                blocks=[block],
                problems=[
                    ProblemUnit(
                        unit_id="page-as-is-1",
                        subject=Subject.MATH,
                        title="1.",
                        stem_block_ids=["b-1"],
                        metadata={
                            "problem_number": 1,
                            "force_full_page_bounds": True,
                            "input_intent": "page-as-is",
                        },
                    )
                ],
            )

            with mock.patch.object(problem_board, "_extract_problem_cutout", side_effect=lambda crop, **_kwargs: crop.convert("RGBA")):
                entries = build_problem_entries([prepared], [page], root / "out", LayoutTemplate(name="academy-default"))

            self.assertEqual(problem_board.PROCESSING_STEP_CHALK, entries[0].processing_step)
            self.assertEqual(problem_board.PLACEMENT_FIT_WIDTH_SCALE_MAX, entries[0].placement_scale_ratio)
            self.assertEqual("page-as-is", entries[0].input_intent)
            self.assertTrue(entries[0].force_full_page_bounds)
            self.assertEqual(Box(left=0.0, top=0.0, width=640.0, height=900.0), entries[0].bounds)

    def test_pdf_marker_problem_crop_does_not_pull_in_page_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            image = Image.new("RGB", (500, 700), "white")
            draw = ImageDraw.Draw(image)
            draw.text((160, 30), "과학탐구 영역", fill="black")
            draw.text((90, 120), "7. problem stem", fill="black")
            draw.line((50, 110, 50, 389), fill="black", width=2)
            image.save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.open(source).convert("RGB"),
                original_size=(500, 700),
            )
            block = ContentBlock(
                block_id="b-1",
                block_type=BlockType.TITLE,
                bbox=Box(80, 110, 340, 260),
                reading_order=0,
                text="7.",
                metadata={
                    "segmenter": "pdf-text-markers",
                    "problem_number": 7,
                    "problem_number_source": "pdf_text_marker",
                    "question_band_index": 1,
                },
            )
            page = PageModel(
                page_id="page-1",
                width_px=500,
                height_px=700,
                subject=Subject.SCIENCE,
                source_path=str(source),
                blocks=[block],
                problems=[
                    ProblemUnit(
                        unit_id="problem-7",
                        subject=Subject.SCIENCE,
                        title="7.",
                        stem_block_ids=["b-1"],
                        metadata={"problem_number": 7, "problem_number_source": "pdf_text_marker"},
                    )
                ],
            )

            with mock.patch.object(problem_board, "_extract_problem_cutout", side_effect=lambda crop, **_kwargs: crop.convert("RGBA")):
                entries = build_problem_entries([prepared], [page], root / "out", LayoutTemplate(name="academy-default"))

            self.assertEqual(110.0, entries[0].bounds.top)
            self.assertEqual(80.0 - problem_board.PDF_TEXT_MARKER_HORIZONTAL_PADDING_PX, entries[0].bounds.left)
            crop_width = Image.open(entries[0].crop_path).size[0]
            self.assertEqual(
                round(entries[0].bounds.width)
                + problem_board.TEXT_PRIORITY_CROP_HORIZONTAL_SAFE_PADDING_PX * 2,
                crop_width,
            )

    def test_build_problem_entries_restores_ignored_hwp_marker_from_text_snippet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            source_image = root / "page-012.png"
            Image.new("RGB", (900, 1200), "white").save(source_image)
            prepared = PreparedPage(
                page_id="page-012",
                source_path=str(source_image),
                page_number=12,
                image=Image.open(source_image).convert("RGB"),
                original_size=(900, 1200),
                metadata={
                    "source_type": "hwp",
                    "source_hwp_path": str(source),
                    "document_like": True,
                },
            )
            quality = {
                "hwp_text_numbered_problem_count": 3,
                "hwp_text_problem_snippets": [
                    {
                        "number": 32,
                        "text": (
                            "32. (가), (나)의 표현상 특징에 대한 설명으로 가장 적절한 것은?\n"
                            "① 첫 번째 선택지\n"
                            "② 두 번째 선택지\n"
                            "⑤ 다섯 번째 선택지"
                        ),
                    }
                ],
            }
            page = PageModel(
                page_id="page-012",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(source_image),
                blocks=[
                    ContentBlock(
                        block_id="q33",
                        block_type=BlockType.TITLE,
                        bbox=Box(480, 80, 340, 280),
                        reading_order=0,
                        text="33.",
                        metadata={"column_index": 2, "question_band_index": 1, "problem_number": 33},
                    ),
                    ContentBlock(
                        block_id="q34",
                        block_type=BlockType.TITLE,
                        bbox=Box(480, 420, 340, 460),
                        reading_order=1,
                        text="34.",
                        metadata={"column_index": 2, "question_band_index": 2, "problem_number": 34},
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-012-problem-1",
                        subject=Subject.KOREAN,
                        title="33.",
                        stem_block_ids=["q33"],
                        metadata={"problem_number": 33, "problem_number_source": "pdf_text_marker"},
                    ),
                    ProblemUnit(
                        unit_id="page-012-problem-2",
                        subject=Subject.KOREAN,
                        title="34.",
                        stem_block_ids=["q34"],
                        metadata={"problem_number": 34, "problem_number_source": "pdf_text_marker"},
                    ),
                ],
                metadata={
                    "source_type": "hwp",
                    "source_hwp_path": str(source),
                    "document_like": True,
                    "segmenter": "pdf-text-markers",
                    "ignored_tiny_pdf_marker_numbers": [32],
                    "hwp_conversion_quality": quality,
                },
            )

            entries = build_problem_entries(
                [prepared],
                [page],
                root / "out",
                LayoutTemplate(name="academy-default"),
            )

            self.assertEqual([32, 33, 34], [entry.problem_number for entry in entries])
            restored = entries[0]
            self.assertIn("hwp_text_fallback_problem", restored.risk_flags)
            self.assertGreaterEqual(restored.crop_path.stat().st_size, 1000)
            with Image.open(restored.crop_path) as crop:
                self.assertGreaterEqual(crop.width, 900)
                self.assertGreaterEqual(crop.height, 420)

    def test_regular_problem_scale_above_editor_limit_requires_legacy_marker(self):
        unmarked = ProblemUnit(
            unit_id="unmarked",
            subject=Subject.MATH,
            title="1.",
            stem_block_ids=[],
            metadata={"placement_scale_ratio": 2.4},
        )
        marked = ProblemUnit(
            unit_id="marked",
            subject=Subject.MATH,
            title="2.",
            stem_block_ids=[],
            metadata={
                "placement_scale_ratio": 2.4,
                "preserveLegacyPlacementScale": True,
            },
        )
        marked_above_compatibility_limit = ProblemUnit(
            unit_id="marked-max",
            subject=Subject.MATH,
            title="3.",
            stem_block_ids=[],
            metadata={
                "placement_scale_ratio": 4.0,
                "preserve_legacy_placement_scale": True,
            },
        )

        self.assertEqual(
            problem_board.PLACEMENT_SCALE_MAX,
            problem_board._default_placement_scale_for_problem(unmarked),
        )
        self.assertEqual(2.4, problem_board._default_placement_scale_for_problem(marked))
        self.assertEqual(
            problem_board.PLACEMENT_FIT_WIDTH_SCALE_MAX,
            problem_board._default_placement_scale_for_problem(marked_above_compatibility_limit),
        )

    def _make_source_image(self, path: Path) -> None:
        image = Image.new("RGB", (860, 620), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((48, 48, 812, 572), outline="black", width=4)
        draw.text((80, 92), "1. Smoke problem", fill="black")
        draw.text((80, 160), "A generated EDB should validate.", fill="black")
        image.save(path)

    def _make_problem_entry(self, root: Path, name: str, bounds: Box) -> ProblemEntry:
        crop_path = root / f"{name}.png"
        Image.new("RGB", (int(bounds.width), int(bounds.height)), "white").save(crop_path)
        prepared = PreparedPage(
            page_id="page-1",
            source_path=str(root / "source.png"),
            page_number=1,
            image=Image.new("RGB", (900, 1200), "white"),
            original_size=(900, 1200),
        )
        return ProblemEntry(
            problem_id=name,
            title=name,
            problem_number=None,
            subject=Subject.UNKNOWN,
            source_page_id="page-1",
            source_path=str(root / "source.png"),
            prepared_page=prepared,
            bounds=bounds,
            crop_path=crop_path,
            board_render_path=crop_path,
            blocks=[],
            actual_height_pages=0.72,
            overflow_allowed=False,
            reading_heavy=False,
            risk_flags=[],
        )

    def test_text_cutout_finalizer_removes_alpha_dust_and_normalizes_chalk_rgb(self):
        image = Image.new("RGBA", (5, 1), (255, 255, 255, 0))
        image.putdata(
            [
                (255, 255, 255, 0),
                (32, 220, 180, 1),
                (20, 190, 150, problem_board.TEXT_DEHALO_ALPHA_CUTOFF),
                (180, 210, 200, problem_board.TEXT_DEHALO_ALPHA_CUTOFF + 1),
                (255, 255, 255, 255),
            ]
        )
        chalk = (248, 249, 246)

        finalized = problem_board._finalize_text_cutout(image, chalk_color=chalk)

        pixels = list(finalized.get_flattened_data())
        self.assertEqual([0, 0, 0, 13, 255], [pixel[3] for pixel in pixels])
        self.assertEqual({chalk}, {pixel[:3] for pixel in pixels})

    def test_text_priority_cutout_skips_diagram_dilation_without_losing_core_ink(self):
        source = Image.new("RGB", (80, 48), "white")
        draw = ImageDraw.Draw(source)
        draw.rectangle((24, 8, 27, 39), fill="black")
        draw.rectangle((42, 18, 43, 20), fill="black")

        generic = problem_board._extract_problem_cutout(source)
        text = problem_board._extract_problem_cutout(source, text_priority=True)
        generic_alpha = list(generic.getchannel("A").get_flattened_data())
        text_alpha = list(text.getchannel("A").get_flattened_data())

        self.assertGreater(
            sum(value > 0 for value in generic_alpha),
            sum(value > 0 for value in text_alpha),
        )
        self.assertFalse(
            any(0 < value <= problem_board.TEXT_DEHALO_ALPHA_CUTOFF for value in text_alpha)
        )
        self.assertEqual(255, text.getpixel((25, 20))[3])
        self.assertEqual(255, text.getpixel((42, 19))[3])

    def test_text_priority_board_export_rebuilds_from_raw_crop_not_tinted_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            board_render_path = root / "tinted-board.png"
            rendered = Image.new("RGBA", (100, 60), (255, 255, 255, 0))
            draw = ImageDraw.Draw(rendered)
            draw.rectangle((2, 2, 95, 55), fill=(30, 220, 180, 80))
            board_render_path.parent.mkdir(parents=True, exist_ok=True)
            rendered.save(board_render_path)

            crop = Image.new("RGB", (80, 48), "white")
            draw = ImageDraw.Draw(crop)
            draw.rectangle((24, 8, 27, 39), fill="black")
            chalk = problem_board._resolve_chalk_color(problem_board.DEFAULT_BOARD_THEME)

            exported = problem_board._load_board_export_image(
                board_render_path,
                crop,
                board_theme=problem_board.DEFAULT_BOARD_THEME,
                target_size=crop.size,
                text_priority=True,
            )

            alpha = exported.getchannel("A")
            self.assertEqual((24, 8, 28, 40), alpha.getbbox())
            self.assertEqual({chalk}, {pixel[:3] for pixel in exported.get_flattened_data()})
            self.assertFalse(
                any(
                    0 < value <= problem_board.TEXT_DEHALO_ALPHA_CUTOFF
                    for value in alpha.get_flattened_data()
                )
            )

    def test_fresh_preprocessed_board_render_skips_duplicate_transparency_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            board_render_path = root / "fresh-board.png"
            rendered = Image.new("RGBA", (80, 48), (238, 238, 226, 0))
            ImageDraw.Draw(rendered).rectangle(
                (20, 8, 27, 39),
                fill=(238, 238, 226, 255),
            )
            rendered.save(board_render_path)
            crop = Image.new("RGB", rendered.size, "white")

            with mock.patch.object(
                problem_board,
                "clean_problem_image_transparency",
                side_effect=AssertionError("fresh render must not be cleaned twice"),
            ), mock.patch.object(
                problem_board,
                "_extract_problem_cutout",
                side_effect=AssertionError("fresh render must not be extracted twice"),
            ):
                exported = problem_board._load_board_export_image(
                    board_render_path,
                    crop,
                    trusted_preprocessed_render=True,
                )

            self.assertEqual(rendered.tobytes(), exported.tobytes())

    def test_fresh_text_board_render_defers_single_finalization_to_record_builder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            board_render_path = root / "fresh-text-board.png"
            rendered = Image.new("RGBA", (80, 48), (238, 238, 226, 0))
            ImageDraw.Draw(rendered).rectangle(
                (20, 8, 27, 39),
                fill=(238, 238, 226, 255),
            )
            rendered.save(board_render_path)
            crop = Image.new("RGB", rendered.size, "white")

            with mock.patch.object(
                problem_board,
                "_finalize_text_cutout",
                side_effect=AssertionError("trusted loader must not finalize before record sizing"),
            ):
                exported = problem_board._load_board_export_image(
                    board_render_path,
                    crop,
                    text_priority=True,
                    trusted_preprocessed_render=True,
                )

            self.assertEqual(rendered.tobytes(), exported.tobytes())

    def test_passage_stitch_removes_footer_badge_and_collapses_join(self):
        first = Image.new("RGB", (240, 300), "white")
        first_draw = ImageDraw.Draw(first)
        first_draw.rectangle((24, 20, 215, 160), outline="black", width=2)
        first_draw.text((36, 52), "passage end", fill="black")
        first_draw.ellipse((150, 220, 215, 260), outline="black", width=3)
        first_draw.text((172, 232), "G2", fill="black")
        first_draw.line((12, 280, 228, 280), fill="black", width=3)
        second = Image.new("RGB", (240, 140), "white")
        second_draw = ImageDraw.Draw(second)
        second_draw.text((24, 10), "continued first glyph", fill="black")
        # Ordinary continuation content must not resemble a near-full-width
        # page-header rule, otherwise the fixture would intentionally trigger
        # the header cleanup exercised by the next test.
        second_draw.rectangle((24, 44, 100, 118), outline="black", width=2)

        prepared = problem_board._prepare_passage_segments_for_stitch([first, second])

        self.assertEqual(2, len(prepared))
        self.assertLess(prepared[0].height, 210)
        self.assertEqual(second.size, prepared[1].size)
        self.assertEqual((0, 0, 0), prepared[0].getpixel((24, 160)))

    def test_passage_stitch_removes_following_column_page_header(self):
        first = Image.new("RGB", (240, 120), "white")
        ImageDraw.Draw(first).text((24, 72), "left column ending", fill="black")
        second = Image.new("RGB", (240, 180), "white")
        draw = ImageDraw.Draw(second)
        draw.text((10, 8), "DOMAIN", fill="black")
        draw.ellipse((150, 2, 220, 30), outline="black", width=2)
        draw.text((172, 8), "G2", fill="black")
        draw.line((8, 42, 232, 42), fill="black", width=3)
        draw.text((24, 64), "continued first glyph", fill="black")
        draw.rectangle((24, 90, 215, 160), outline="black", width=2)

        prepared = problem_board._prepare_passage_segments_for_stitch([first, second])

        self.assertLess(prepared[1].height, 140)
        self.assertGreater(prepared[1].height, 110)
        self.assertLess(prepared[1].convert("L").crop((0, 0, 240, 24)).getextrema()[0], 200)

    def test_passage_stitch_removes_cropped_page_header_rule_without_header_text(self):
        first = Image.new("RGB", (240, 120), "white")
        ImageDraw.Draw(first).text((24, 72), "left column ending", fill="black")
        second = Image.new("RGB", (240, 180), "white")
        draw = ImageDraw.Draw(second)
        draw.line((8, 12, 232, 12), fill="black", width=3)
        draw.text((24, 44), "continued first glyph", fill="black")

        prepared = problem_board._prepare_passage_segments_for_stitch([first, second])

        self.assertLess(prepared[1].height, 165)
        self.assertEqual(
            (255, 255, 255),
            prepared[1].getpixel((120, 0)),
        )
        self.assertLess(
            prepared[1].convert("L").crop((20, 8, 180, 40)).getextrema()[0],
            200,
        )

    def test_passage_stitch_keeps_centered_section_label_above_box(self):
        first = Image.new("RGB", (240, 120), "white")
        ImageDraw.Draw(first).text((24, 72), "section A ending", fill="black")
        second = Image.new("RGB", (240, 180), "white")
        draw = ImageDraw.Draw(second)
        draw.text((112, 8), "(B)", fill="black")
        draw.rectangle((20, 42, 220, 160), outline="black", width=2)
        draw.text((32, 64), "section B body", fill="black")

        prepared = problem_board._prepare_passage_segments_for_stitch([first, second])

        self.assertEqual(second.size, prepared[1].size)
        self.assertLess(
            prepared[1].convert("L").crop((96, 0, 144, 32)).getextrema()[0],
            200,
        )

    def test_passage_cleanup_erases_outer_guides_without_cropping_box_or_label(self):
        image = Image.new("RGB", (240, 220), "white")
        draw = ImageDraw.Draw(image)
        draw.text((112, 8), "(B)", fill="black")
        draw.rectangle((14, 42, 226, 160), outline="black", width=2)
        draw.line((4, 0, 4, 219), fill="black", width=2)
        draw.line((4, 168, 10, 168), fill="black", width=2)
        draw.line((235, 0, 235, 219), fill="black", width=2)
        draw.line((229, 32, 235, 32), fill="black", width=2)
        draw.rectangle((180, 195, 181, 197), fill="black")

        cleaned = problem_board._erase_passage_outer_margin_page_guides(image)

        self.assertEqual(image.size, cleaned.size)
        self.assertEqual((255, 255, 255), cleaned.getpixel((4, 80)))
        self.assertEqual((0, 0, 0), cleaned.getpixel((10, 168)))
        self.assertEqual((255, 255, 255), cleaned.getpixel((235, 80)))
        self.assertEqual((0, 0, 0), cleaned.getpixel((229, 32)))
        self.assertEqual((0, 0, 0), cleaned.getpixel((180, 196)))
        self.assertEqual((0, 0, 0), cleaned.getpixel((14, 80)))
        self.assertLess(
            cleaned.convert("L").crop((96, 0, 144, 32)).getextrema()[0],
            200,
        )

    def test_passage_cleanup_does_not_treat_repeated_underlines_as_box_edges(self):
        image = Image.new("RGB", (500, 640), "white")
        draw = ImageDraw.Draw(image)
        draw.line((12, 0, 12, 639), fill="black", width=2)
        draw.rectangle((25, 52, 478, 620), outline="black", width=2)
        draw.text((52, 16), "[26 ~ 28] passage", fill="black")
        draw.text((52, 84), "first glyph must stay", fill="black")
        # Repeated answer underlines are long enough to be frame candidates,
        # but their endpoints vary and must not displace the real box border.
        draw.line((96, 150, 466, 150), fill="black", width=2)
        draw.line((96, 214, 466, 214), fill="black", width=2)
        draw.line((58, 278, 466, 278), fill="black", width=2)
        draw.line((180, 342, 466, 342), fill="black", width=2)

        cleaned = problem_board._erase_passage_outer_margin_page_guides(image)

        self.assertEqual((255, 255, 255), cleaned.getpixel((12, 300)))
        self.assertEqual((0, 0, 0), cleaned.getpixel((25, 300)))
        self.assertLess(
            cleaned.convert("L").crop((48, 80, 90, 104)).getextrema()[0],
            200,
        )

    def test_passage_cleanup_does_not_erase_text_beside_an_inner_material_box(self):
        image = Image.new("RGB", (240, 220), "white")
        draw = ImageDraw.Draw(image)
        # A long outer rule and tightly spaced glyph-like strokes model a
        # speaker label.  The smaller material box is intentionally cleaner
        # and therefore easier for the frame detector to find.
        draw.line((8, 0, 8, 219), fill="black", width=2)
        for left in (13, 22, 31, 40):
            draw.rectangle((left, 80, left + 5, 88), fill="black")
        draw.rectangle((48, 42, 228, 190), outline="black", width=2)

        cleaned = problem_board._erase_passage_outer_margin_page_guides(image)

        self.assertEqual(image.tobytes(), cleaned.tobytes())
        self.assertEqual((0, 0, 0), cleaned.getpixel((14, 84)))

    def test_passage_cleanup_prefers_vertical_frame_over_repeated_underlines(self):
        image = Image.new("RGB", (240, 220), "white")
        draw = ImageDraw.Draw(image)
        draw.line((4, 0, 4, 180), fill="black", width=2)  # page divider
        draw.line((14, 0, 14, 219), fill="black", width=2)  # passage frame
        draw.line((228, 0, 228, 219), fill="black", width=2)
        for y in (48, 82, 116, 150):
            draw.line((48, y, 210, y), fill="black", width=2)
        draw.rectangle((31, 176, 38, 184), fill="black")  # first glyph

        cleaned = problem_board._erase_passage_outer_margin_page_guides(image)

        self.assertEqual((255, 255, 255), cleaned.getpixel((4, 100)))
        self.assertEqual((0, 0, 0), cleaned.getpixel((14, 100)))
        self.assertEqual((0, 0, 0), cleaned.getpixel((34, 180)))

    def test_passage_stitch_collapses_duplicate_edge_padding(self):
        first = Image.new("RGB", (240, 220), "white")
        ImageDraw.Draw(first).text((24, 130), "last passage line", fill="black")
        second = Image.new("RGB", (240, 220), "white")
        ImageDraw.Draw(second).text((24, 80), "continued passage line", fill="black")

        prepared = problem_board._prepare_passage_segments_for_stitch([first, second])

        self.assertLess(prepared[0].height, 175)
        self.assertLess(prepared[1].height, 155)

    def test_passage_stitch_preserves_substantial_footnote_below_rule(self):
        first = Image.new("RGB", (240, 300), "white")
        draw = ImageDraw.Draw(first)
        draw.rectangle((24, 32, 214, 136), outline="black", width=2)
        draw.line((12, 236, 228, 236), fill="black", width=3)
        footnote_color = (30, 90, 210)
        for y in (252, 270, 288):
            draw.rectangle((22, y, 205, y + 2), fill=footnote_color)
        second = Image.new("RGB", (240, 100), "white")
        ImageDraw.Draw(second).text((24, 24), "continued body", fill="black")

        prepared = problem_board._prepare_passage_segments_for_stitch([first, second])

        self.assertEqual(first.size, prepared[0].size)
        self.assertEqual(footnote_color, prepared[0].getpixel((40, 270)))

    def test_passage_stitch_preserves_midbody_box_rule_on_continuation(self):
        first = Image.new("RGB", (240, 100), "white")
        ImageDraw.Draw(first).text((24, 24), "first page", fill="black")
        second = Image.new("RGB", (240, 300), "white")
        draw = ImageDraw.Draw(second)
        marker_color = (210, 45, 45)
        draw.rectangle((24, 30, 60, 58), fill=marker_color)
        draw.line((8, 120, 232, 120), fill="black", width=3)
        draw.rectangle((24, 145, 215, 272), outline="black", width=2)

        prepared = problem_board._prepare_passage_segments_for_stitch([first, second])

        self.assertGreaterEqual(prepared[1].height, 280)
        self.assertEqual(
            sum(pixel == marker_color for pixel in second.get_flattened_data()),
            sum(pixel == marker_color for pixel in prepared[1].get_flattened_data()),
        )

    def test_passage_source_bounds_recover_edge_glyphs(self):
        expanded = problem_board._expand_passage_source_bounds_horizontally(
            Box(100, 40, 200, 320),
            image_width=400,
        )

        self.assertEqual(76.0, expanded.left)
        self.assertEqual(248.0, expanded.width)
        self.assertEqual(40.0, expanded.top)
        self.assertEqual(320.0, expanded.height)
        right_column = problem_board._expand_passage_source_bounds_horizontally(
            Box(240, 40, 120, 320),
            image_width=400,
        )
        self.assertEqual(206.0, right_column.left)
        self.assertEqual(178.0, right_column.width)
        left_column = problem_board._expand_passage_source_bounds_horizontally(
            Box(40, 40, 120, 320),
            image_width=400,
        )
        self.assertEqual(16.0, left_column.left)
        self.assertEqual(178.0, left_column.width)

    def test_passage_source_bounds_exclude_off_center_column_divider(self):
        left_column = problem_board._expand_passage_source_bounds_horizontally(
            Box(40, 40, 120, 320),
            image_width=400,
            column_divider_x=180.0,
        )
        right_column = problem_board._expand_passage_source_bounds_horizontally(
            Box(200, 40, 120, 320),
            image_width=400,
            column_divider_x=180.0,
        )

        self.assertEqual(174.0, left_column.right)
        self.assertEqual(186.0, right_column.left)
        self.assertLess(left_column.right, 180.0)
        self.assertGreater(right_column.left, 180.0)

    def test_passage_source_bounds_use_explicit_column_when_text_box_straddles_divider(self):
        left_column = problem_board._expand_passage_source_bounds_horizontally(
            Box(120, 40, 100, 320),
            image_width=400,
            column_divider_x=180.0,
            column_index=1,
        )
        right_column = problem_board._expand_passage_source_bounds_horizontally(
            Box(160, 40, 100, 320),
            image_width=400,
            column_divider_x=180.0,
            column_index=2,
        )

        self.assertEqual(174.0, left_column.right)
        self.assertEqual(186.0, right_column.left)
        self.assertLess(left_column.right, right_column.left)

    def test_passage_segment_source_bounds_recover_vertical_edge_glyphs(self):
        expanded = problem_board._expand_passage_segment_source_bounds(
            Box(100, 8, 200, 380),
            image_width=400,
            image_height=400,
        )

        self.assertEqual(76.0, expanded.left)
        self.assertEqual(0.0, expanded.top)
        self.assertEqual(248.0, expanded.width)
        self.assertEqual(400.0, expanded.height)

    def test_stitched_passage_does_not_apply_a_second_vertical_outset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = Image.new("RGB", (400, 400), "white")
            draw = ImageDraw.Draw(source)
            draw.text((40, 96), "first passage fragment", fill="black")
            draw.text((240, 216), "second passage fragment", fill="black")
            task = problem_board._ProblemAssetTask(
                source_image=source,
                bounds=Box(32, 80, 320, 240),
                segment_bounds=(
                    Box(32, 80, 128, 80),
                    Box(224, 200, 128, 80),
                ),
                crop_path=root / "crop.png",
                board_render_path=root / "board.png",
                chalk_color=(238, 238, 226),
                trim_edge_guides=False,
                preserve_horizontal_bounds=True,
                pad_edges=False,
            )
            captured_heights: list[int] = []
            original_compose = problem_board._compose_passage_segments

            def capture_segments(images, **kwargs):
                captured_heights.extend(image.height for image in images)
                return original_compose(images, **kwargs)

            with mock.patch.object(
                problem_board,
                "_compose_passage_segments",
                side_effect=capture_segments,
            ):
                problem_board._render_problem_asset(task)

            self.assertEqual([80, 80], captured_heights)

    def test_text_priority_source_bounds_recover_only_crossing_glyphs(self):
        image = Image.new("RGB", (600, 400), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((96, 120, 104, 136), fill="black")
        draw.rectangle((236, 180, 244, 196), fill="black")
        bounds = Box(100, 80, 140, 220)

        expanded = problem_board._expand_text_priority_source_bounds_horizontally(
            image,
            bounds,
        )

        self.assertEqual(84.0, expanded.left)
        self.assertEqual(172.0, expanded.width)
        self.assertEqual(80.0, expanded.top)
        self.assertEqual(220.0, expanded.height)

    def test_text_priority_source_bounds_ignore_long_vertical_guide(self):
        image = Image.new("RGB", (600, 400), "white")
        ImageDraw.Draw(image).line((99, 80, 99, 299), fill="black", width=2)
        bounds = Box(100, 80, 140, 220)

        expanded = problem_board._expand_text_priority_source_bounds_horizontally(
            image,
            bounds,
        )

        self.assertEqual(bounds, expanded)

    def test_horizontal_inner_edge_guard_finds_hidden_clip_but_keeps_frame(self):
        clipped = Image.new("RGB", (320, 240), "white")
        ImageDraw.Draw(clipped).rectangle((14, 80, 28, 96), fill="black")

        clipped_stats = problem_board._problem_image_horizontal_inner_edge_risk_stats(
            clipped,
            expected_padding_px=16,
        )

        self.assertTrue(clipped_stats["hasRisk"])
        self.assertEqual(["left"], clipped_stats["riskSides"])

        framed = Image.new("RGB", (320, 240), "white")
        ImageDraw.Draw(framed).rectangle((16, 30, 303, 210), outline="black", width=2)

        framed_stats = problem_board._problem_image_horizontal_inner_edge_risk_stats(
            framed,
            expected_padding_px=16,
        )

        self.assertFalse(framed_stats["hasRisk"])
        self.assertEqual([], framed_stats["riskSides"])

    def test_classin_preflight_reports_hidden_horizontal_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "clipped.png"
            clipped = Image.new("RGB", (320, 240), "white")
            ImageDraw.Draw(clipped).rectangle((14, 80, 28, 96), fill="black")
            clipped.save(image_path)

            report = problem_board._classin_handoff_preflight(
                {
                    "contentTarget": "all",
                    "problems": [
                        {
                            "id": "korean-problem-1",
                            "title": "1.",
                            "problemNumber": 1,
                            "subject": "korean",
                            "imagePath": image_path.resolve().as_uri(),
                            "processingStep": "s2",
                            "riskFlags": [],
                            "reviewStatus": "passed",
                        }
                    ],
                }
            )

        edge_issues = [
            issue
            for issue in report["issues"]
            if issue.get("type") == "horizontal_crop_edge_risk"
        ]
        self.assertEqual(1, len(edge_issues))
        self.assertEqual(["left"], edge_issues[0]["horizontalEdgeRiskStats"]["riskSides"])

    def test_classin_preflight_skips_crop_edge_scan_for_full_page_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "full-page.png"
            Image.new("RGB", (320, 240), "white").save(image_path)

            with mock.patch.object(
                problem_board,
                "_problem_image_horizontal_inner_edge_risk_stats",
                side_effect=AssertionError("full-page export must not scan a nonexistent crop seam"),
            ):
                report = problem_board._classin_handoff_preflight(
                    {
                        "contentTarget": "all",
                        "problems": [
                            {
                                "id": "korean-page-1",
                                "title": "1.",
                                "problemNumber": 1,
                                "subject": "korean",
                                "imagePath": image_path.resolve().as_uri(),
                                "processingStep": problem_board.PROCESSING_STEP_RAW,
                                "inputIntent": "page-as-is",
                                "forceFullPageBounds": True,
                                "riskFlags": [],
                                "reviewStatus": "passed",
                            }
                        ],
                    }
                )

        self.assertFalse(
            any(issue.get("type") == "horizontal_crop_edge_risk" for issue in report["issues"])
        )

    def test_tiny_top_column_header_is_not_used_as_passage_continuation(self):
        page = PageModel(
            page_id="page-1",
            width_px=900,
            height_px=1200,
            subject=Subject.KOREAN,
        )
        header = ContentBlock(
            block_id="header",
            block_type=BlockType.IMAGE,
            bbox=Box(470, 8, 420, 48),
            reading_order=1,
            metadata={
                "segmenter": "pdf-passage-range",
                "marker_kind": "passage_continuation",
                "passage_text_line_count": 1,
                "passage_text_character_count": 2,
            },
        )
        real_continuation = ContentBlock(
            block_id="continuation",
            block_type=BlockType.IMAGE,
            bbox=Box(470, 8, 420, 520),
            reading_order=1,
            metadata={
                "segmenter": "pdf-passage-range",
                "marker_kind": "passage_continuation",
                "passage_text_line_count": 18,
                "passage_text_character_count": 480,
            },
        )

        self.assertTrue(
            problem_board._is_probable_passage_continuation_page_header(page, header)
        )
        self.assertFalse(
            problem_board._is_probable_passage_continuation_page_header(
                page,
                real_continuation,
            )
        )

    def test_passage_segments_are_centered_without_rescaling_at_the_join(self):
        narrow = Image.new("RGB", (100, 80), "white")
        ImageDraw.Draw(narrow).rectangle((0, 10, 9, 69), fill="black")
        wide = Image.new("RGB", (140, 80), "white")
        ImageDraw.Draw(wide).rectangle((0, 10, 9, 69), fill="black")

        stitched = problem_board._compose_passage_segments(
            [narrow, wide],
            transparent=False,
        )

        self.assertEqual((140, 176), stitched.size)
        self.assertEqual((255, 255, 255), stitched.getpixel((0, 10)))
        self.assertEqual((0, 0, 0), stitched.getpixel((20, 10)))
        self.assertEqual((0, 0, 0), stitched.getpixel((0, 106)))

    def test_passage_segment_frames_share_one_visual_axis(self):
        first = Image.new("RGB", (200, 200), "white")
        first_draw = ImageDraw.Draw(first)
        first_draw.line((10, 8, 10, 191), fill="black", width=2)
        first_draw.line((190, 8, 190, 191), fill="black", width=2)
        second = Image.new("RGB", (200, 200), "white")
        second_draw = ImageDraw.Draw(second)
        second_draw.line((14, 8, 14, 191), fill="black", width=2)
        second_draw.line((194, 8, 194, 191), fill="black", width=2)

        stitched = problem_board._compose_passage_segments(
            [first, second],
            transparent=False,
        )

        self.assertEqual(204, stitched.width)
        self.assertEqual((0, 0, 0), stitched.getpixel((14, 20)))
        self.assertEqual((0, 0, 0), stitched.getpixel((14, 236)))
        self.assertEqual((0, 0, 0), stitched.getpixel((14, 207)))

    def test_passage_segment_frame_bridge_preserves_transparent_chalk_color(self):
        first = Image.new("RGBA", (160, 100), (255, 255, 255, 0))
        first_draw = ImageDraw.Draw(first)
        first_draw.line((12, 8, 12, 91), fill=(244, 248, 241, 255), width=1)
        first_draw.line((148, 8, 148, 91), fill=(244, 248, 241, 255), width=1)
        second = first.copy()

        stitched = problem_board._compose_passage_segments(
            [first, second],
            transparent=True,
        )

        self.assertEqual((244, 248, 241, 255), stitched.getpixel((12, 107)))

    def test_passage_segment_frame_bridge_skips_mismatched_widths(self):
        first = Image.new("RGB", (200, 120), "white")
        first_draw = ImageDraw.Draw(first)
        first_draw.line((10, 8, 10, 111), fill="black", width=2)
        first_draw.line((190, 8, 190, 111), fill="black", width=2)
        second = Image.new("RGB", (200, 120), "white")
        second_draw = ImageDraw.Draw(second)
        second_draw.line((20, 8, 20, 111), fill="black", width=2)
        second_draw.line((180, 8, 180, 111), fill="black", width=2)

        stitched = problem_board._compose_passage_segments(
            [first, second],
            transparent=False,
        )

        self.assertEqual((255, 255, 255), stitched.getpixel((10, 127)))
        self.assertEqual((255, 255, 255), stitched.getpixel((20, 127)))

    def test_passage_segment_frame_bridge_skips_closed_section_boxes(self):
        first = Image.new("RGB", (200, 120), "white")
        ImageDraw.Draw(first).rectangle((10, 8, 190, 111), outline="black", width=2)
        second = Image.new("RGB", (200, 120), "white")
        ImageDraw.Draw(second).rectangle((10, 8, 190, 111), outline="black", width=2)

        stitched = problem_board._compose_passage_segments(
            [first, second],
            transparent=False,
        )

        self.assertEqual((255, 255, 255), stitched.getpixel((10, 126)))
        self.assertEqual((255, 255, 255), stitched.getpixel((190, 126)))

    def test_continuous_full_width_records_keep_explicit_non_overlap_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [
                self._make_problem_entry(root, "passage-1", Box(0, 0, 900, 1200)),
                self._make_problem_entry(root, "passage-2", Box(0, 0, 900, 1200)),
            ]
            for entry in entries:
                entry.subject = Subject.KOREAN
                entry.input_intent = "page-as-is"
                entry.processing_step = problem_board.PROCESSING_STEP_CHALK
                entry.placement_scale_ratio = problem_board.PLACEMENT_FIT_WIDTH_SCALE_MAX
            _records, placements = build_image_only_records(
                entries,
                LayoutTemplate(name="academy-default", board_page_count=20),
                crop_format=CROP_FORMAT_V1,
            )

            actual_gap = placements[1]["record_top_y_pages"] - placements[0]["record_bottom_y_pages"]
            expected_gap = problem_board.CONTINUOUS_RECORD_GAP_PX / problem_board.CANVAS_WIDTH
            self.assertGreater(actual_gap, 0.0)
            self.assertAlmostEqual(expected_gap, actual_gap, places=5)

    def test_passage_only_s2_full_width_export_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            passage = self._make_problem_entry(root, "page-1-passage-28-30", Box(0, 0, 900, 1800))
            question = self._make_problem_entry(root, "page-1-problem-28", Box(0, 0, 900, 600))

            selected = configure_problem_entries_for_export(
                [passage, question],
                passage_problem_ids={passage.problem_id},
                passages_only=True,
                processing_step="s2",
                full_width=True,
            )

            self.assertEqual([passage.problem_id], [entry.problem_id for entry in selected])
            self.assertEqual(problem_board.PROCESSING_STEP_CHALK, selected[0].processing_step)
            self.assertEqual("page-as-is", selected[0].input_intent)
            self.assertEqual(0.0, selected[0].placement_x_ratio)
            self.assertEqual(
                problem_board.PLACEMENT_FIT_WIDTH_SCALE_MAX,
                selected[0].placement_scale_ratio,
            )

    def test_content_target_only_treats_separate_shared_passage_as_passage(self):
        common_passage = ProblemUnit(
            unit_id="page-1-passage-1-3",
            subject=Subject.KOREAN,
            title="지문 1~3",
            metadata={"passage_role": "passage_fragment", "passage_group_id": "passage-1-3"},
        )
        child_question = ProblemUnit(
            unit_id="page-1-problem-1",
            subject=Subject.KOREAN,
            title="1.",
            metadata={"passage_role": "child_question", "passage_group_id": "passage-1-3"},
        )
        question_with_table = ProblemUnit(
            unit_id="page-1-problem-4",
            subject=Subject.MATH,
            title="4.",
            metadata={"contains_table": True},
        )

        self.assertFalse(problem_board._problem_matches_content_target(common_passage, "questions"))
        self.assertTrue(problem_board._problem_matches_content_target(child_question, "questions"))
        self.assertTrue(problem_board._problem_matches_content_target(question_with_table, "questions"))
        self.assertTrue(problem_board._problem_matches_content_target(common_passage, "shared-passages"))
        self.assertFalse(problem_board._problem_matches_content_target(child_question, "shared-passages"))
        self.assertFalse(problem_board._problem_matches_content_target(question_with_table, "shared-passages"))

    def test_passage_only_defaults_to_text_preserving_fit_width(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            passage = self._make_problem_entry(root, "page-1-passage-28-30", Box(0, 0, 900, 1800))
            question = self._make_problem_entry(root, "page-1-problem-28", Box(0, 0, 900, 600))

            selected = configure_problem_entries_for_export(
                [passage, question],
                passage_problem_ids={passage.problem_id},
                passages_only=True,
            )

            self.assertEqual([passage.problem_id], [entry.problem_id for entry in selected])
            self.assertEqual(problem_board.PROCESSING_STEP_CHALK, selected[0].processing_step)
            self.assertEqual("page-as-is", selected[0].input_intent)
            self.assertEqual(0.0, selected[0].placement_x_ratio)
            self.assertEqual(
                problem_board.PLACEMENT_FIT_WIDTH_SCALE_MAX,
                selected[0].placement_scale_ratio,
            )

    def test_passage_only_keeps_an_explicit_processing_step_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            passage = self._make_problem_entry(root, "page-1-passage-28-30", Box(0, 0, 900, 1800))

            selected = configure_problem_entries_for_export(
                [passage],
                passage_problem_ids={passage.problem_id},
                passages_only=True,
                processing_step=problem_board.PROCESSING_STEP_ORIGINAL,
            )

            self.assertEqual(problem_board.PROCESSING_STEP_ORIGINAL, selected[0].processing_step)
            self.assertEqual("page-as-is", selected[0].input_intent)

    def test_passage_only_defaults_to_one_stitched_image_record(self):
        self.assertEqual(
            "image-only",
            problem_board.resolve_export_record_mode(None, passages_only=True),
        )

    def test_passage_only_keeps_explicit_mixed_record_mode(self):
        self.assertEqual(
            "mixed",
            problem_board.resolve_export_record_mode("mixed", passages_only=True),
        )

    def test_regular_export_keeps_mixed_record_mode_default(self):
        self.assertEqual(
            "mixed",
            problem_board.resolve_export_record_mode(None, passages_only=False),
        )

    def test_generated_single_problem_edb_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            self._make_source_image(source)

            result = run_problem_export(
                source,
                output_dir=root / "out",
                input_intent="single-problem",
                ocr="noop",
                record_mode="image-only",
                export_edb=True,
            )

            validation = validate_edb_file(result["edb_path"], expected_min_records=1)
            self.assertEqual(validation["recordCountActual"], 1)
            self.assertGreater(validation["outerSize"], 0)

    def test_classin_split_chunks_keep_each_edb_at_or_below_fifty_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = []
            for index in range(42):
                entry = self._make_problem_entry(root, f"p{index:02d}", Box(0, 0, 640, 640))
                entry.actual_height_pages = 1.2
                entries.append(entry)

            chunks = split_problem_entries_for_classin_page_limit(
                entries,
                LayoutTemplate(name="academy-default", board_page_count=80),
            )

            self.assertEqual([41, 1], [len(chunk) for chunk in chunks])

    def test_record_assembly_rejects_duplicate_problem_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._make_problem_entry(root, "duplicate-id", Box(0, 0, 640, 640))
            second = self._make_problem_entry(root, "second", Box(0, 0, 640, 640))
            second.problem_id = first.problem_id

            with self.assertRaisesRegex(ValueError, "Duplicate problem ID.*duplicate-id"):
                build_image_only_records(
                    [first, second],
                    LayoutTemplate(name="academy-default"),
                )

    def test_classin_split_rejects_single_problem_over_fifty_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = self._make_problem_entry(root, "oversized", Box(0, 0, 640, 640))
            entry.actual_height_pages = 50.1

            with self.assertRaisesRegex(ValueError, "oversized.*50-page limit"):
                split_problem_entries_for_classin_page_limit(
                    [entry],
                    LayoutTemplate(name="academy-default", board_page_count=80),
                )

    def test_classin_writer_rejects_single_rendered_record_over_fifty_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = self._make_problem_entry(root, "rendered-oversized", Box(0, 0, 640, 640))
            placement = {
                "problem_id": entry.problem_id,
                "actual_bottom_y_pages": 0.72,
                "snapped_next_start_y_pages": 1.2,
                "record_bottom_y_pages": 50.5,
                "recordPageCountHint": 50,
            }

            with self.assertRaisesRegex(ValueError, "rendered-oversized.*50-page limit"):
                write_classin_limited_edb_files(
                    [entry],
                    LayoutTemplate(name="academy-default", board_page_count=50),
                    root,
                    "lesson.edb",
                    record_mode="image-only",
                    text_confidence_threshold=0.78,
                    dark_board=True,
                    board_theme="charcoal",
                    crop_format=edb_builder.CROP_FORMAT_V2,
                    existing_records=[b"placeholder"],
                    existing_placements=[placement],
                    existing_header_flag=0,
                )

    def test_classin_split_does_not_rebuild_each_candidate_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = []
            for index in range(42):
                entry = self._make_problem_entry(root, f"p{index:02d}", Box(0, 0, 640, 640))
                entry.actual_height_pages = 1.2
                entries.append(entry)

            with mock.patch.object(
                problem_board,
                "_entries_flow_end_pages",
                side_effect=AssertionError("split should use incremental placement"),
            ):
                chunks = split_problem_entries_for_classin_page_limit(
                    entries,
                    LayoutTemplate(name="academy-default", board_page_count=84),
                )

            self.assertEqual([41, 1], [len(chunk) for chunk in chunks])

    def test_classin_page_limit_uses_largest_available_end_metric(self):
        placements = [
            {
                "record_bottom_y_pages": 49.2,
                "actual_bottom_y_pages": 49.1,
                "snapped_next_start_y_pages": 49.0,
            },
            {
                "record_bottom_y_pages": 49.8,
                "actual_bottom_y_pages": 50.2,
                "snapped_next_start_y_pages": 49.9,
            },
        ]

        self.assertAlmostEqual(50.2, problem_board._placement_summaries_flow_end_pages(placements))
        self.assertEqual(1, problem_board._first_placement_over_page_limit(placements, 50))

    def test_classin_writer_rejects_sequential_record_overlap(self):
        placements = [
            {
                "problem_id": "problem-1",
                "record_top_y_pages": 0.0,
                "record_bottom_y_pages": 2.0,
            },
            {
                "problem_id": "problem-2",
                "record_top_y_pages": 1.2,
                "record_bottom_y_pages": 2.4,
            },
        ]

        with self.assertRaisesRegex(ValueError, r"problem-1->problem-2 \(0\.800000 pages\)"):
            problem_board._validate_sequential_record_placements(placements)

    def test_classin_split_counts_slot_rounding_for_long_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = LayoutTemplate(
                name="academy-default",
                board_page_count=80,
                base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
            )
            entries = []
            for index in range(40):
                entry = self._make_problem_entry(root, f"normal-{index:02d}", Box(0, 0, 640, 640))
                entry.actual_height_pages = 0.72
                entries.append(entry)
            long_entry = self._make_problem_entry(root, "long-40", Box(0, 0, 640, 960))
            long_entry.actual_height_pages = 1.6
            entries.append(long_entry)

            self.assertAlmostEqual(50.4, problem_board._entries_flow_end_pages(entries, template))

            chunks = split_problem_entries_for_classin_page_limit(entries, template)

            self.assertEqual([40, 1], [len(chunk) for chunk in chunks])

    def test_classin_slight_overflow_fits_single_slot_without_split(self):
        # A problem barely taller than one visible page (1.2) shrinks to fit
        # its slot instead of reserving a second, mostly-empty page.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = LayoutTemplate(
                name="academy-default",
                board_page_count=80,
                base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
            )
            entries = []
            for index in range(40):
                entry = self._make_problem_entry(root, f"normal-{index:02d}", Box(0, 0, 640, 640))
                entry.actual_height_pages = 0.72
                entries.append(entry)
            slight_entry = self._make_problem_entry(root, "slight-40", Box(0, 0, 640, 726))
            slight_entry.actual_height_pages = 1.21
            slight_entry.overflow_allowed = False
            entries.append(slight_entry)

            self.assertAlmostEqual(49.2, problem_board._entries_flow_end_pages(entries, template))

            chunks = split_problem_entries_for_classin_page_limit(entries, template)

            self.assertEqual([41], [len(chunk) for chunk in chunks])

    def test_problem_export_writes_real_split_edbs_with_fifty_page_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            self._make_source_image(source)
            entries = []
            for index in range(42):
                entry = self._make_problem_entry(root, f"p{index:02d}", Box(0, 0, 640, 640))
                entry.actual_height_pages = 1.2
                entries.append(entry)

            with mock.patch.object(problem_board, "build_problem_entries", return_value=entries):
                result = run_problem_export(
                    source,
                    output_dir=root / "out",
                    input_intent="single-problem",
                    ocr="noop",
                    record_mode="image-only",
                    export_edb=True,
                    edb_name="lesson.edb",
                )

            self.assertEqual(2, result["summary"]["edb_part_count"])
            self.assertTrue(result["summary"]["edb_split"])
            self.assertEqual(["lesson_part01.edb", "lesson_part02.edb"], [
                Path(part["edbPath"]).name for part in result["summary"]["edb_parts"]
            ])
            validations = [
                validate_edb_file(part["edbPath"], expected_min_records=part["recordCount"])
                for part in result["summary"]["edb_parts"]
            ]
            self.assertTrue(all(validation["pageCountHint"] <= 50 for validation in validations))
            self.assertEqual([41, 1], [validation["recordCountActual"] for validation in validations])
            self.assertEqual(2, result["ui_session"]["edbPartCount"])
            by_id = {problem["id"]: problem for problem in result["ui_session"]["problems"]}
            self.assertEqual(1, by_id["p00"]["edbPartIndex"])
            self.assertEqual(2, by_id["p41"]["edbPartIndex"])
            self.assertEqual(0.0, by_id["p41"]["edbLocalStartYPages"])

            handoff = json.loads(Path(result["classin_handoff_path"]).read_text(encoding="utf-8"))
            self.assertEqual(50, handoff["classinPageCountHint"])
            self.assertEqual(84, handoff["globalBoardPageCountEstimate"])

    def test_edb_split_uses_rendered_record_bottom_not_estimated_slot_height(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = LayoutTemplate(name="academy-default", board_page_count=84)
            entries = []
            for index in range(42):
                entry = self._make_problem_entry(root, f"p{index:02d}", Box(0, 0, 400, 972))
                entry.actual_height_pages = problem_board.estimate_height_pages(
                    (400, 972),
                    template,
                )
                entries.append(entry)

            def fake_record_image(placement, entry, **_kwargs):
                return problem_board._ImageOnlyRecordImage(
                    crop_path=entry.crop_path,
                    board_render_path=entry.board_render_path,
                    image_bytes=f"primary-{entry.problem_id}".encode("ascii"),
                    secondary_bytes=f"secondary-{entry.problem_id}".encode("ascii"),
                    width_px=400,
                    height_px=972,
                    scale_ratio=None,
                )

            with mock.patch.object(problem_board, "_build_image_only_record_image", side_effect=fake_record_image):
                parts = problem_board.write_classin_limited_edb_files(
                    entries,
                    template,
                    root,
                    "height-gap.edb",
                    record_mode="image-only",
                    text_confidence_threshold=0.78,
                    dark_board=True,
                    board_theme=problem_board.DEFAULT_BOARD_THEME,
                    crop_format=CROP_FORMAT_V1,
                )

            self.assertEqual([20, 20, 2], [len(part["problemIds"]) for part in parts])
            self.assertEqual(["height-gap_part01.edb", "height-gap_part02.edb", "height-gap_part03.edb"], [
                Path(part["edbPath"]).name for part in parts
            ])
            for part in parts:
                self.assertLessEqual(part["flowEndPages"], 50)
                self.assertLessEqual(
                    max(placement["record_bottom_y_pages"] for placement in part["placements"]),
                    50,
                )
                for current, next_placement in zip(part["placements"], part["placements"][1:]):
                    self.assertLessEqual(
                        current["record_bottom_y_pages"],
                        next_placement["record_top_y_pages"],
                    )
                validation = validate_edb_file(part["edbPath"], expected_min_records=part["recordCount"])
                self.assertLessEqual(validation["pageCountHint"], 50)

    def test_problem_export_records_stage_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            self._make_source_image(source)

            result = run_problem_export(
                source,
                output_dir=root / "out",
                input_intent="single-problem",
                ocr="noop",
                record_mode="image-only",
                export_edb=True,
            )

            timing = result["summary"]["timing_ms"]
            self.assertGreaterEqual(timing["total"], 0)
            self.assertGreaterEqual(timing["source_build"], 0)
            self.assertGreaterEqual(timing["problem_assets"], 0)
            self.assertGreaterEqual(timing["records"], 0)
            self.assertGreaterEqual(timing["ui_session"], 0)
            self.assertEqual(timing, result["ui_session"]["timing_ms"])

    def test_image_only_preview_skips_edb_record_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            self._make_source_image(source)

            with mock.patch.object(
                problem_board,
                "_encode_image_bytes",
                side_effect=AssertionError("preview should not encode EDB image records"),
            ):
                result = run_problem_export(
                    source,
                    output_dir=root / "out",
                    input_intent="multi-problem",
                    ocr="none",
                    record_mode="image-only",
                    export_edb=False,
                    detect_perspective=False,
                    skip_deskew=True,
                )

            self.assertGreaterEqual(len(result["ui_session"]["problems"]), 1)
            self.assertEqual(
                len(result["ui_session"]["problems"]),
                len(result["summary"]["placements"]),
            )
            self.assertEqual(0, result["summary"]["record_count"])

    def test_problem_export_writes_classin_handoff_manifest_for_manual_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            self._make_source_image(source)

            result = run_problem_export(
                source,
                output_dir=root / "out",
                input_intent="single-problem",
                ocr="noop",
                record_mode="image-only",
                export_edb=True,
            )

            handoff_path = result["classin_handoff_path"]
            handoff = json.loads(handoff_path.read_text(encoding="utf-8"))

            self.assertEqual("ready_for_classin_review", handoff["status"])
            self.assertEqual(str(result["edb_path"]), handoff["edbPath"])
            self.assertEqual(1, handoff["expectedRecordCount"])
            self.assertEqual(1, handoff["expectedCoreProblemCount"])
            self.assertTrue(handoff["manualReviewRequired"])
            self.assertIn("ClassIn에서 EDB 파일 열기", handoff["classinReviewChecklist"])
            self.assertTrue((root / "out" / "classin_handoff.md").is_file())
            self.assertEqual("ready_for_classin_review", result["ui_session"]["classinHandoffStatus"])
            self.assertTrue(result["ui_session"]["readyForClassIn"])
            self.assertEqual("passed", result["ui_session"]["classinPreflight"]["status"])
            self.assertEqual("ready_for_classin_review", result["summary"]["classin_handoff_status"])
            self.assertTrue(result["summary"]["ready_for_classin"])

    def test_problem_ui_session_summarizes_duplicate_problem_number_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            crop = root / "crop.png"
            Image.new("RGB", (320, 240), "white").save(crop)

            placements = []
            for index, number in enumerate([35, 36, 37, 35, 36, 37], start=1):
                page_id = "page-choice-a" if index <= 3 else "page-choice-b"
                placements.append(
                    {
                        "problem_id": f"problem-{index}",
                        "title": f"{number}.",
                        "problem_number": number,
                        "subject": Subject.KOREAN,
                        "source_page_id": page_id,
                        "source_path": str(source),
                        "crop_path": str(crop),
                        "board_render_path": str(crop),
                        "bbox": {"left": 0, "top": 0, "width": 320, "height": 240},
                        "actual_content_height_pages": 0.8,
                        "overflow_allowed": False,
                        "start_y_pages": float(index),
                        "snapped_next_start_y_pages": float(index + 1),
                        "overflow_amount_pages": 0.0,
                        "overflow_violation": False,
                        "slot_span_count": 1,
                        "placement_x_ratio": 0.0,
                        "placement_y_ratio": 0.0,
                        "placement_scale_ratio": 1.0,
                        "record_mode": "image-only",
                        "text_record_count": 0,
                        "image_record_count": 1,
                        "risk_flags": [],
                    }
                )

            ui_session = build_problem_ui_session(
                [],
                placements,
                root / "out",
                None,
                [source],
                record_mode="image-only",
            )

            groups = ui_session["duplicateProblemNumberGroups"]
            self.assertEqual(1, len(groups))
            self.assertEqual(35, groups[0]["numberStart"])
            self.assertEqual(37, groups[0]["numberEnd"])
            self.assertEqual("35-37", groups[0]["numberLabel"])
            self.assertEqual(2, groups[0]["occurrencesPerNumber"])
            self.assertEqual(3, groups[0]["duplicateRecordCount"])
            self.assertEqual(6, groups[0]["totalRecordCount"])
            self.assertEqual(["page-choice-a", "page-choice-b"], groups[0]["sourcePageIds"])
            self.assertFalse(groups[0]["blocking"])
            self.assertTrue(groups[0]["pageOrderPreserved"])
            self.assertEqual("edb_page_order", groups[0]["orderBasis"])
            self.assertEqual(groups, ui_session["duplicate_problem_number_groups"])
            self.assertEqual(1, ui_session["duplicateProblemNumberGroupCount"])
            self.assertEqual([], ui_session["blockingDuplicateProblemNumberGroups"])

    def test_problem_ui_session_marks_official_alternate_section_duplicate_ranges_nonblocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            crop = root / "crop.png"
            Image.new("RGB", (320, 240), "white").save(crop)

            placements = []
            problem_numbers = [35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45] * 2
            for index, number in enumerate(problem_numbers, start=1):
                section_page_offset = 0 if index <= 11 else 10
                placements.append(
                    {
                        "problem_id": f"problem-{index}",
                        "title": f"{number}.",
                        "problem_number": number,
                        "subject": Subject.KOREAN,
                        "source_page_id": f"page-{section_page_offset + index:03d}",
                        "source_path": str(source),
                        "crop_path": str(crop),
                        "board_render_path": str(crop),
                        "bbox": {"left": 0, "top": 0, "width": 320, "height": 240},
                        "actual_content_height_pages": 0.8,
                        "overflow_allowed": False,
                        "start_y_pages": float(index),
                        "snapped_next_start_y_pages": float(index + 1),
                        "overflow_amount_pages": 0.0,
                        "overflow_violation": False,
                        "slot_span_count": 1,
                        "placement_x_ratio": 0.0,
                        "placement_y_ratio": 0.0,
                        "placement_scale_ratio": 1.0,
                        "record_mode": "image-only",
                        "text_record_count": 0,
                        "image_record_count": 1,
                        "risk_flags": [],
                    }
                )

            ui_session = build_problem_ui_session(
                [],
                placements,
                root / "out",
                None,
                [source],
                record_mode="image-only",
            )

            groups = ui_session["duplicateProblemNumberGroups"]
            self.assertEqual(1, len(groups))
            self.assertEqual("alternate_section", groups[0]["classification"])
            self.assertFalse(groups[0]["blocking"])
            self.assertEqual([], ui_session["blockingDuplicateProblemNumberGroups"])
            for problem in ui_session["problems"]:
                self.assertNotIn("duplicate_problem_number", problem["riskFlags"])
                self.assertEqual("normal", problem["reviewStatus"])

    def test_problem_ui_session_marks_math_common_and_elective_duplicate_ranges_nonblocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4")
            crop = root / "crop.png"
            Image.new("RGB", (320, 240), "white").save(crop)

            placements = []
            problem_numbers = (
                list(range(1, 31))
                + list(range(23, 31)) * 2
                + list(range(1, 31))
                + list(range(23, 31)) * 2
            )
            for index, number in enumerate(problem_numbers, start=1):
                placements.append(
                    {
                        "problem_id": f"problem-{index}",
                        "title": f"{number}.",
                        "problem_number": number,
                        "subject": Subject.MATH,
                        "source_page_id": f"page-{index:03d}",
                        "source_path": str(source),
                        "crop_path": str(crop),
                        "board_render_path": str(crop),
                        "bbox": {"left": 0, "top": 0, "width": 320, "height": 240},
                        "actual_content_height_pages": 0.8,
                        "overflow_allowed": False,
                        "start_y_pages": float(index),
                        "snapped_next_start_y_pages": float(index + 1),
                        "overflow_amount_pages": 0.0,
                        "overflow_violation": False,
                        "slot_span_count": 1,
                        "placement_x_ratio": 0.0,
                        "placement_y_ratio": 0.0,
                        "placement_scale_ratio": 1.0,
                        "record_mode": "image-only",
                        "text_record_count": 0,
                        "image_record_count": 1,
                        "risk_flags": [],
                    }
                )

            ui_session = build_problem_ui_session(
                [],
                placements,
                root / "out",
                None,
                [source],
                record_mode="image-only",
            )

            groups = ui_session["duplicateProblemNumberGroups"]
            self.assertEqual(1, len(groups))
            self.assertEqual(1, groups[0]["numberStart"])
            self.assertEqual(30, groups[0]["numberEnd"])
            self.assertEqual("alternate_section", groups[0]["classification"])
            self.assertFalse(groups[0]["blocking"])
            self.assertEqual([], ui_session["blockingDuplicateProblemNumberGroups"])
            for problem in ui_session["problems"]:
                self.assertNotIn("duplicate_problem_number", problem["riskFlags"])
                self.assertEqual("normal", problem["reviewStatus"])

            preflight, duplicate_groups = _session_publish_blocking_preflight(ui_session["problems"], session=ui_session)
            self.assertTrue(preflight["passed"])
            self.assertEqual("passed", preflight["status"])
            self.assertEqual([], duplicate_groups)

            legacy_flagged_problems = [
                {
                    **problem,
                    "riskFlags": ["duplicate_problem_number"],
                    "reviewStatus": "check_needed",
                }
                if problem["problemNumber"] == 24
                else problem
                for problem in ui_session["problems"]
            ]
            preflight, duplicate_groups = _session_publish_blocking_preflight(
                legacy_flagged_problems,
                session={**ui_session, "problems": legacy_flagged_problems},
            )
            self.assertTrue(preflight["passed"])
            self.assertEqual("passed", preflight["status"])
            self.assertEqual([], duplicate_groups)

    def test_publish_preflight_blocks_any_s3_page_chrome_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "s3-artifact.png"
            image = Image.new("RGB", (320, 240), "white")
            draw = ImageDraw.Draw(image)
            draw.text((64, 48), "1. reconstructed", fill="black")
            draw.line((0, 0, 0, 239), fill="black", width=3)
            image.save(artifact)
            problem = {
                "id": "problem-1",
                "title": "1.",
                "imagePath": artifact.resolve().as_uri(),
                "processingStep": "s3",
                "riskFlags": [],
            }

            preflight, duplicate_groups = _session_publish_blocking_preflight(
                [problem],
                session={"problems": [problem], "pages": []},
            )

            self.assertFalse(preflight["passed"])
            self.assertEqual("blocked", preflight["status"])
            self.assertEqual([], duplicate_groups)
            self.assertEqual(["step3_page_chrome_artifact"], [issue["type"] for issue in preflight["issues"]])
            self.assertEqual(["edge_vertical_guide"], preflight["issues"][0]["artifactTypes"])

    def test_page_chrome_detector_keeps_paired_inset_content_frame(self):
        image = Image.new("RGB", (500, 420), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((24, 40, 475, 390), outline="black", width=2)
        draw.text((64, 80), "legitimate framed passage", fill="black")

        stats = problem_board._problem_image_page_chrome_artifact_stats(image)

        self.assertTrue(stats["pairedContentFrame"])
        self.assertFalse(stats["hasArtifact"])
        self.assertEqual(0, stats["edgeGuideColumnCount"])

    def test_publish_preflight_allows_page_chrome_for_page_as_is_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "full-page.png"
            image = Image.new("RGB", (320, 240), "white")
            draw = ImageDraw.Draw(image)
            draw.text((64, 48), "full page", fill="black")
            draw.line((0, 0, 0, 239), fill="black", width=3)
            image.save(artifact)
            problem = {
                "id": "page-1",
                "title": "페이지 1",
                "imagePath": artifact.resolve().as_uri(),
                "processingStep": "s3",
                "recordMode": "image-only",
                "riskFlags": [],
            }

            preflight, duplicate_groups = _session_publish_blocking_preflight(
                [problem],
                session={
                    "problems": [problem],
                    "pages": [],
                    "input_intent": "page-as-is",
                    "template": {"metadata": {"placement_mode": "continuous-page-as-is"}},
                },
            )

            self.assertTrue(preflight["passed"])
            self.assertEqual("passed", preflight["status"])
            self.assertEqual([], duplicate_groups)
            self.assertEqual([], preflight["issues"])

    def test_publish_preflight_allows_one_s2_page_chrome_artifact_per_ten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clean = root / "clean.png"
            clean_image = Image.new("RGB", (320, 240), "white")
            ImageDraw.Draw(clean_image).text((64, 48), "clean problem", fill="black")
            clean_image.save(clean)
            artifact = root / "s2-artifact.png"
            artifact_image = clean_image.copy()
            ImageDraw.Draw(artifact_image).line((0, 0, 0, 239), fill="black", width=3)
            artifact_image.save(artifact)

            def make_problem(index: int, *, has_artifact: bool) -> dict[str, object]:
                path = artifact if has_artifact else clean
                return {
                    "id": f"problem-{index}",
                    "title": f"{index}.",
                    "imagePath": path.resolve().as_uri(),
                    "processingStep": "s2",
                    "riskFlags": [],
                }

            one_bad = [
                make_problem(index, has_artifact=index == 1)
                for index in range(1, 11)
            ]
            preflight, _duplicate_groups = _session_publish_blocking_preflight(
                one_bad,
                session={"problems": one_bad, "pages": []},
            )
            self.assertTrue(preflight["passed"])

            two_bad = [
                make_problem(index, has_artifact=index in {1, 2})
                for index in range(1, 11)
            ]
            preflight, _duplicate_groups = _session_publish_blocking_preflight(
                two_bad,
                session={"problems": two_bad, "pages": []},
            )
            self.assertFalse(preflight["passed"])
            self.assertEqual(["step2_page_chrome_artifact_rate"], [issue["type"] for issue in preflight["issues"]])
            self.assertEqual(2, preflight["issues"][0]["artifactProblemCount"])
            self.assertEqual(10, preflight["issues"][0]["checkedProblemCount"])
            self.assertGreater(preflight["issues"][0]["artifactRatio"], 0.10)

    def test_publish_preflight_keeps_intentional_s2_passage_box(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            passage = root / "passage.png"
            image = Image.new("RGB", (320, 240), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 20, 319, 239), outline="black", width=3)
            draw.text((32, 48), "shared passage", fill="black")
            image.save(passage)
            problem = {
                "id": "passage-1-3",
                "title": "지문 1~3",
                "imagePath": passage.resolve().as_uri(),
                "processingStep": "s2",
                "passageRole": "passage_fragment",
                "riskFlags": [],
            }

            preflight, duplicate_groups = _session_publish_blocking_preflight(
                [problem],
                session={"problems": [problem], "pages": []},
            )

            self.assertTrue(preflight["passed"])
            self.assertEqual("passed", preflight["status"])
            self.assertEqual([], duplicate_groups)
            self.assertEqual([], preflight["issues"])

    def test_problem_ui_session_keeps_duplicate_problem_numbers_nonblocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            crop = root / "crop.png"
            Image.new("RGB", (320, 240), "white").save(crop)

            placements = []
            for index, number in enumerate([24, 25, 24], start=1):
                placements.append(
                    {
                        "problem_id": f"problem-{index}",
                        "title": f"{number}.",
                        "problem_number": number,
                        "subject": Subject.KOREAN,
                        "source_page_id": f"page-{index}",
                        "source_path": str(source),
                        "crop_path": str(crop),
                        "board_render_path": str(crop),
                        "bbox": {"left": 0, "top": 0, "width": 320, "height": 240},
                        "actual_content_height_pages": 0.8,
                        "overflow_allowed": False,
                        "start_y_pages": float(index),
                        "snapped_next_start_y_pages": float(index + 1),
                        "overflow_amount_pages": 0.0,
                        "overflow_violation": False,
                        "slot_span_count": 1,
                        "placement_x_ratio": 0.0,
                        "placement_y_ratio": 0.0,
                        "placement_scale_ratio": 1.0,
                        "record_mode": "image-only",
                        "text_record_count": 0,
                        "image_record_count": 1,
                        "risk_flags": [],
                    }
                )

            ui_session = build_problem_ui_session(
                [],
                placements,
                root / "out",
                None,
                [source],
                record_mode="image-only",
            )

            duplicate_groups = ui_session["duplicateProblemNumberGroups"]
            self.assertEqual(1, len(duplicate_groups))
            self.assertFalse(duplicate_groups[0]["blocking"])
            self.assertEqual([], ui_session["blockingDuplicateProblemNumberGroups"])
            for problem in ui_session["problems"]:
                self.assertNotIn("duplicate_problem_number", problem["riskFlags"])
                self.assertEqual("normal", problem["reviewStatus"])

            preflight, duplicate_groups = _session_publish_blocking_preflight(ui_session["problems"], session=ui_session)
            self.assertTrue(preflight["passed"])
            self.assertEqual("passed", preflight["status"])
            self.assertEqual([], duplicate_groups)

    def test_problem_ui_session_flags_source_bbox_overlap_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            crop = root / "crop.png"
            Image.new("RGB", (640, 260), "white").save(crop)

            placements = []
            for index, (page_id, bbox) in enumerate(
                [
                    ("page-001", {"left": 40, "top": 100, "width": 520, "height": 320}),
                    ("page-001", {"left": 60, "top": 125, "width": 500, "height": 300}),
                    ("page-002", {"left": 60, "top": 125, "width": 500, "height": 300}),
                ],
                start=1,
            ):
                placements.append(
                    {
                        "problem_id": f"problem-{index}",
                        "title": f"{20 + index}.",
                        "problem_number": 20 + index,
                        "subject": Subject.KOREAN,
                        "source_page_id": page_id,
                        "source_path": str(source),
                        "crop_path": str(crop),
                        "board_render_path": str(crop),
                        "bbox": bbox,
                        "actual_content_height_pages": 0.8,
                        "overflow_allowed": False,
                        "start_y_pages": float(index),
                        "snapped_next_start_y_pages": float(index + 1),
                        "overflow_amount_pages": 0.0,
                        "overflow_violation": False,
                        "slot_span_count": 1,
                        "placement_x_ratio": 0.0,
                        "placement_y_ratio": 0.0,
                        "placement_scale_ratio": 1.0,
                        "record_mode": "image-only",
                        "text_record_count": 0,
                        "image_record_count": 1,
                        "risk_flags": [],
                    }
                )

            ui_session = build_problem_ui_session(
                [],
                placements,
                root / "out",
                None,
                [source],
                record_mode="image-only",
            )

            groups = ui_session["sourceProblemOverlapGroups"]
            self.assertEqual(groups, ui_session["source_problem_overlap_groups"])
            self.assertEqual(1, ui_session["sourceProblemOverlapGroupCount"])
            self.assertEqual(1, len(groups))
            self.assertEqual("page-001", groups[0]["sourcePageId"])
            self.assertEqual(["problem-1", "problem-2"], groups[0]["problemIds"])
            self.assertGreaterEqual(groups[0]["overlapAreaRatio"], 0.8)

            flagged = {
                problem["id"]: problem
                for problem in ui_session["problems"]
                if "source_problem_bbox_overlap" in problem["riskFlags"]
            }
            self.assertEqual({"problem-1", "problem-2"}, set(flagged))
            for problem in flagged.values():
                self.assertEqual("check_needed", problem["reviewStatus"])

            safe_problem = next(problem for problem in ui_session["problems"] if problem["id"] == "problem-3")
            self.assertNotIn("source_problem_bbox_overlap", safe_problem["riskFlags"])
            self.assertEqual("normal", safe_problem["reviewStatus"])

    def test_problem_ui_session_flags_passage_group_source_reuse_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            crop = root / "crop.png"
            Image.new("RGB", (640, 320), "white").save(crop)

            page = PageModel(
                page_id="page-004",
                width_px=900,
                height_px=1200,
                subject=Subject.ENGLISH,
                source_path=str(source),
                problems=[
                    ProblemUnit(
                        unit_id="p31",
                        subject=Subject.ENGLISH,
                        title="31.",
                        metadata={
                            "problem_number": 31,
                            "passage_group_id": "hwp-text-passage-31-34",
                            "passage_range": {"start": 31, "end": 34},
                            "passage_role": "child_question",
                            "passage_child_problem_numbers": [31, 32, 33, 34],
                        },
                    ),
                    ProblemUnit(
                        unit_id="p32",
                        subject=Subject.ENGLISH,
                        title="32.",
                        metadata={
                            "problem_number": 32,
                            "passage_group_id": "hwp-text-passage-31-34",
                            "passage_range": {"start": 31, "end": 34},
                            "passage_role": "child_question",
                            "passage_child_problem_numbers": [31, 32, 33, 34],
                        },
                    ),
                ],
            )
            placements = []
            for problem_id, number, bbox in (
                ("p31", 31, {"left": 42, "top": 120, "width": 520, "height": 430}),
                ("p32", 32, {"left": 48, "top": 132, "width": 510, "height": 410}),
            ):
                placements.append(
                    {
                        "problem_id": problem_id,
                        "title": f"{number}.",
                        "problem_number": number,
                        "subject": Subject.ENGLISH,
                        "source_page_id": "page-004",
                        "source_path": str(source),
                        "crop_path": str(crop),
                        "board_render_path": str(crop),
                        "bbox": bbox,
                        "actual_content_height_pages": 0.8,
                        "overflow_allowed": True,
                        "start_y_pages": float(number - 31),
                        "snapped_next_start_y_pages": float(number - 30),
                        "overflow_amount_pages": 0.0,
                        "overflow_violation": False,
                        "slot_span_count": 1,
                        "placement_x_ratio": 0.0,
                        "placement_y_ratio": 0.0,
                        "placement_scale_ratio": 1.0,
                        "record_mode": "image-only",
                        "text_record_count": 0,
                        "image_record_count": 1,
                        "risk_flags": [],
                    }
                )

            ui_session = build_problem_ui_session(
                [],
                placements,
                root / "out",
                None,
                [source],
                record_mode="image-only",
                pages=[page],
            )

            groups = ui_session["passageGroupSourceReuseGroups"]
            self.assertEqual(groups, ui_session["passage_group_source_reuse_groups"])
            self.assertEqual(1, ui_session["passageGroupSourceReuseGroupCount"])
            self.assertEqual("hwp-text-passage-31-34", groups[0]["passageGroupId"])
            self.assertEqual(["p31", "p32"], groups[0]["problemIds"])
            self.assertGreaterEqual(groups[0]["overlapAreaRatio"], 0.8)

            flagged = {
                problem["id"]: problem
                for problem in ui_session["problems"]
                if "passage_group_source_reuse" in problem["riskFlags"]
            }
            self.assertEqual({"p31", "p32"}, set(flagged))
            for problem in flagged.values():
                self.assertEqual("check_needed", problem["reviewStatus"])

            self.assertEqual(1, ui_session["passageReviewItemCount"])
            self.assertEqual([33, 34], ui_session["passageReviewItems"][0]["missingChildProblemNumbers"])
            self.assertEqual(2, ui_session["passageReviewItems"][0]["missingChildProblemCount"])
            self.assertEqual(
                ["passage_missing_child_questions", "passage_group_source_reuse"],
                ui_session["passageReviewItems"][0]["reviewReasonCodes"],
            )

    def test_problem_ui_session_flags_missing_passage_child_questions_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            crop = root / "crop.png"
            Image.new("RGB", (760, 420), "white").save(crop)

            page = PageModel(
                page_id="page-005",
                width_px=900,
                height_px=1200,
                subject=Subject.ENGLISH,
                source_path=str(source),
                problems=[
                    ProblemUnit(
                        unit_id=f"p{number}",
                        subject=Subject.ENGLISH,
                        title=f"{number}.",
                        metadata={
                            "problem_number": number,
                            "passage_group_id": "hwp-text-passage-31-34",
                            "passage_range": {"start": 31, "end": 34},
                            "passage_role": "child_question",
                            "passage_child_problem_numbers": [31, 32, 33, 34],
                        },
                    )
                    for number in (31, 32, 34)
                ],
            )
            placements = []
            for index, number in enumerate((31, 32, 34), start=1):
                placements.append(
                    {
                        "problem_id": f"p{number}",
                        "title": f"{number}.",
                        "problem_number": number,
                        "subject": Subject.ENGLISH,
                        "source_page_id": "page-005",
                        "source_path": str(source),
                        "crop_path": str(crop),
                        "board_render_path": str(crop),
                        "bbox": {"left": 40, "top": 80 + index * 180, "width": 560, "height": 120},
                        "actual_content_height_pages": 0.8,
                        "overflow_allowed": True,
                        "start_y_pages": float(index),
                        "snapped_next_start_y_pages": float(index + 1),
                        "overflow_amount_pages": 0.0,
                        "overflow_violation": False,
                        "slot_span_count": 1,
                        "placement_x_ratio": 0.0,
                        "placement_y_ratio": 0.0,
                        "placement_scale_ratio": 1.0,
                        "record_mode": "image-only",
                        "text_record_count": 0,
                        "image_record_count": 1,
                        "risk_flags": [],
                    }
                )

            ui_session = build_problem_ui_session(
                [],
                placements,
                root / "out",
                None,
                [source],
                record_mode="image-only",
                pages=[page],
            )

            flagged = {
                problem["id"]: problem
                for problem in ui_session["problems"]
                if "passage_missing_child_questions" in problem["riskFlags"]
            }
            self.assertEqual({"p31", "p32", "p34"}, set(flagged))
            for problem in flagged.values():
                self.assertEqual("check_needed", problem["reviewStatus"])

            self.assertEqual(1, ui_session["passageReviewItemCount"])
            self.assertEqual([33], ui_session["passageReviewItems"][0]["missingChildProblemNumbers"])
            self.assertEqual(
                ["passage_missing_child_questions"],
                ui_session["passageReviewItems"][0]["reviewReasonCodes"],
            )
            self.assertEqual(
                ["passage_missing_child_questions"],
                ui_session["passageReviewItems"][0]["riskFlags"],
            )

    def test_ui_session_exposes_shared_passage_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_image = root / "page.png"
            crop_path = root / "crop.png"
            Image.new("RGB", (900, 1200), "white").save(source_image)
            Image.new("RGB", (600, 420), "white").save(crop_path)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source_image),
                page_number=1,
                image=Image.new("RGB", (900, 1200), "white"),
                original_size=(900, 1200),
            )
            page = PageModel(
                page_id="page-1",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(source_image),
                blocks=[
                    ContentBlock(
                        block_id="range-header",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=40, width=500, height=40),
                        reading_order=0,
                        text="[13~14] 다음 글을 읽고 물음에 답하시오.",
                    ),
                    ContentBlock(
                        block_id="shared-passage",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=90, width=500, height=360),
                        reading_order=1,
                        text="shared passage",
                    ),
                    ContentBlock(
                        block_id="q13",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=480, width=500, height=120),
                        reading_order=2,
                        text="13. question",
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-problem-1",
                        subject=Subject.KOREAN,
                        title="13.",
                        stem_block_ids=["range-header", "shared-passage", "q13"],
                        metadata={
                            "problem_number": 13,
                            "passage_group_id": "page-1-passage-13-14",
                            "passage_range": {"start": 13, "end": 14},
                            "passage_role": "child_question",
                            "shared_passage_block_ids": ["range-header", "shared-passage"],
                            "passage_child_problem_numbers": [13, 14],
                            "passage_pre_question_continuation_block_ids": ["continued-line-1"],
                        },
                    )
                ],
            )
            placement = {
                "problem_id": "page-1-problem-1",
                "title": "13.",
                "problem_number": 13,
                "subject": "국어",
                "source_page_id": "page-1",
                "source_path": str(source_image),
                "crop_path": str(crop_path),
                "board_render_path": str(crop_path),
                "actual_content_height_pages": 0.75,
                "overflow_allowed": True,
                "overflow_violation": False,
                "overflow_amount_pages": 0.0,
                "slot_span_count": 1,
                "start_y_pages": 0.0,
                "snapped_next_start_y_pages": 1.2,
                "placement_x_ratio": 0.0,
                "placement_y_ratio": 0.0,
                "placement_scale_ratio": 1.0,
                "record_mode": "image-only",
                "processing_step": "raw",
                "text_record_count": 0,
                "image_record_count": 1,
                "bbox": {"left": 40, "top": 40, "width": 500, "height": 560},
                "risk_flags": [],
            }

            ui_session = build_problem_ui_session(
                [prepared],
                [placement],
                root / "out",
                None,
                [source_image],
                record_mode="image-only",
                pages=[page],
            )

            problem = ui_session["problems"][0]
            self.assertEqual("page-1-passage-13-14", problem["passageGroupId"])
            self.assertEqual({"start": 13, "end": 14}, problem["passageRange"])
            self.assertEqual("child_question", problem["passageRole"])
            self.assertEqual(["range-header", "shared-passage"], problem["sharedPassageBlockIds"])
            self.assertEqual([13, 14], problem["passageChildProblemNumbers"])
            self.assertEqual(["continued-line-1"], problem["passagePreQuestionContinuationBlockIds"])
            self.assertEqual(["continued-line-1"], problem["passage_pre_question_continuation_block_ids"])

    def test_ui_session_keeps_ordered_passage_boxes_on_every_source_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_paths = [root / "page-1.png", root / "page-2.png"]
            crop_path = root / "passage.png"
            for source_path in source_paths:
                Image.new("RGB", (900, 1200), "white").save(source_path)
            Image.new("RGB", (600, 900), "white").save(crop_path)
            prepared_pages = [
                PreparedPage(
                    page_id=f"page-{index}",
                    source_path=str(source_path),
                    page_number=index,
                    image=Image.open(source_path).convert("RGB"),
                    original_size=(900, 1200),
                )
                for index, source_path in enumerate(source_paths, start=1)
            ]
            pages = [
                PageModel(
                    page_id=f"page-{index}",
                    width_px=900,
                    height_px=1200,
                    subject=Subject.KOREAN,
                    source_path=str(source_path),
                )
                for index, source_path in enumerate(source_paths, start=1)
            ]
            placement = {
                "problem_id": "passage-1",
                "title": "지문 1~3",
                "problem_number": None,
                "subject": "korean",
                "source_page_id": "page-1",
                "source_path": str(source_paths[0]),
                "crop_path": str(crop_path),
                "board_render_path": str(crop_path),
                "actual_content_height_pages": 1.0,
                "overflow_allowed": True,
                "overflow_violation": False,
                "overflow_amount_pages": 0.0,
                "slot_span_count": 1,
                "start_y_pages": 0.0,
                "snapped_next_start_y_pages": 1.2,
                "placement_x_ratio": 0.0,
                "placement_y_ratio": 0.0,
                "placement_scale_ratio": 1.0,
                "record_mode": "image-only",
                "processing_step": "raw",
                "text_record_count": 0,
                "image_record_count": 1,
                "bbox": {"left": 40, "top": 60, "width": 820, "height": 1080},
                "source_segments": [
                    {
                        "source_page_id": "page-1",
                        "column_index": 1,
                        "fragment_index": 1,
                        "bbox": {"left": 40, "top": 500, "width": 390, "height": 620},
                    },
                    {
                        "source_page_id": "page-1",
                        "column_index": 2,
                        "fragment_index": 2,
                        "bbox": {"left": 470, "top": 60, "width": 390, "height": 760},
                    },
                    {
                        "source_page_id": "page-2",
                        "column_index": 1,
                        "fragment_index": 3,
                        "bbox": {"left": 40, "top": 60, "width": 390, "height": 420},
                    },
                ],
                "risk_flags": [],
            }

            ui_session = build_problem_ui_session(
                prepared_pages,
                [placement],
                root / "out",
                None,
                source_paths,
                record_mode="image-only",
                pages=pages,
            )

            problem = ui_session["problems"][0]
            self.assertEqual(
                ["page-1", "page-1", "page-2"],
                [segment["sourcePageId"] for segment in problem["sourceSegments"]],
            )
            self.assertEqual([["passage-1"], ["passage-1"]], [page["problemIds"] for page in ui_session["pages"]])

    def test_ui_session_links_cross_page_passage_child_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page_1_image = root / "page-1.png"
            page_2_image = root / "page-2.png"
            crop_13 = root / "crop-13.png"
            crop_15 = root / "crop-15.png"
            for path in (page_1_image, page_2_image):
                Image.new("RGB", (900, 1200), "white").save(path)
            for path in (crop_13, crop_15):
                Image.new("RGB", (600, 420), "white").save(path)

            prepared_pages = [
                PreparedPage(
                    page_id="page-1",
                    source_path=str(page_1_image),
                    page_number=1,
                    image=Image.new("RGB", (900, 1200), "white"),
                    original_size=(900, 1200),
                ),
                PreparedPage(
                    page_id="page-2",
                    source_path=str(page_2_image),
                    page_number=2,
                    image=Image.new("RGB", (900, 1200), "white"),
                    original_size=(900, 1200),
                ),
            ]
            pages = [
                PageModel(
                    page_id="page-1",
                    width_px=900,
                    height_px=1200,
                    subject=Subject.KOREAN,
                    source_path=str(page_1_image),
                    problems=[
                        ProblemUnit(
                            unit_id="page-1-problem-13",
                            subject=Subject.KOREAN,
                            title="13.",
                            metadata={
                                "problem_number": 13,
                                "passage_group_id": "page-1-passage-13-16",
                                "passage_range": {"start": 13, "end": 16},
                                "passage_role": "child_question",
                                "shared_passage_block_ids": ["range-header", "shared-passage-a"],
                                "passage_child_problem_numbers": [13, 14, 15, 16],
                            },
                        ),
                    ],
                ),
                PageModel(
                    page_id="page-2",
                    width_px=900,
                    height_px=1200,
                    subject=Subject.KOREAN,
                    source_path=str(page_2_image),
                    problems=[
                        ProblemUnit(
                            unit_id="page-2-problem-15",
                            subject=Subject.KOREAN,
                            title="15.",
                            metadata={"problem_number": 15},
                        ),
                    ],
                ),
            ]
            placements = [
                {
                    "problem_id": "page-1-problem-13",
                    "title": "13.",
                    "problem_number": 13,
                    "subject": "국어",
                    "source_page_id": "page-1",
                    "source_path": str(page_1_image),
                    "crop_path": str(crop_13),
                    "board_render_path": str(crop_13),
                    "actual_content_height_pages": 0.75,
                    "overflow_allowed": True,
                    "overflow_violation": False,
                    "overflow_amount_pages": 0.0,
                    "slot_span_count": 1,
                    "start_y_pages": 0.0,
                    "snapped_next_start_y_pages": 1.2,
                    "placement_x_ratio": 0.0,
                    "placement_y_ratio": 0.0,
                    "placement_scale_ratio": 1.0,
                    "record_mode": "image-only",
                    "processing_step": "raw",
                    "text_record_count": 0,
                    "image_record_count": 1,
                    "bbox": {"left": 40, "top": 40, "width": 500, "height": 560},
                    "risk_flags": [],
                },
                {
                    "problem_id": "page-2-problem-15",
                    "title": "15.",
                    "problem_number": 15,
                    "subject": "국어",
                    "source_page_id": "page-2",
                    "source_path": str(page_2_image),
                    "crop_path": str(crop_15),
                    "board_render_path": str(crop_15),
                    "actual_content_height_pages": 0.75,
                    "overflow_allowed": True,
                    "overflow_violation": False,
                    "overflow_amount_pages": 0.0,
                    "slot_span_count": 1,
                    "start_y_pages": 1.2,
                    "snapped_next_start_y_pages": 2.4,
                    "placement_x_ratio": 0.0,
                    "placement_y_ratio": 0.0,
                    "placement_scale_ratio": 1.0,
                    "record_mode": "image-only",
                    "processing_step": "raw",
                    "text_record_count": 0,
                    "image_record_count": 1,
                    "bbox": {"left": 40, "top": 40, "width": 500, "height": 560},
                    "risk_flags": [],
                },
            ]

            ui_session = build_problem_ui_session(
                prepared_pages,
                placements,
                root / "out",
                None,
                [page_1_image, page_2_image],
                record_mode="image-only",
                pages=pages,
            )

            problems_by_id = {problem["id"]: problem for problem in ui_session["problems"]}
            linked_problem = problems_by_id["page-2-problem-15"]
            self.assertEqual("page-1-passage-13-16", linked_problem["passageGroupId"])
            self.assertEqual({"start": 13, "end": 16}, linked_problem["passageRange"])
            self.assertEqual("child_question", linked_problem["passageRole"])
            self.assertEqual([13, 14, 15, 16], linked_problem["passageChildProblemNumbers"])
            self.assertEqual(["page-1", "page-2"], linked_problem["passageSourcePageIds"])
            self.assertTrue(linked_problem["passageContinuesAcrossPages"])
            self.assertIn("passage_cross_page_merge_check", linked_problem["riskFlags"])
            self.assertEqual("check_needed", linked_problem["reviewStatus"])

    def test_mvp_export_links_cross_page_passage_child_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            page_1_image = root / "page-1.png"
            page_2_image = root / "page-2.png"
            for path in (page_1_image, page_2_image):
                Image.new("RGB", (900, 1200), "white").save(path)
            prepared_pages = [
                PreparedPage(
                    page_id="page-1",
                    source_path=str(page_1_image),
                    page_number=1,
                    image=Image.open(page_1_image).convert("RGB"),
                    original_size=(900, 1200),
                ),
                PreparedPage(
                    page_id="page-2",
                    source_path=str(page_2_image),
                    page_number=2,
                    image=Image.open(page_2_image).convert("RGB"),
                    original_size=(900, 1200),
                ),
            ]
            page_1 = PageModel(
                page_id="page-1",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(page_1_image),
                blocks=[
                    ContentBlock(
                        block_id="q13",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=80, width=500, height=120),
                        reading_order=0,
                        text="13. first child",
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-problem-13",
                        subject=Subject.KOREAN,
                        title="13.",
                        stem_block_ids=["q13"],
                        metadata={
                            "problem_number": 13,
                            "passage_group_id": "page-1-passage-13-16",
                            "passage_range": {"start": 13, "end": 16},
                            "passage_role": "child_question",
                            "shared_passage_block_ids": ["range-header", "shared-passage"],
                            "passage_child_problem_numbers": [13, 14, 15, 16],
                        },
                    )
                ],
            )
            page_2 = PageModel(
                page_id="page-2",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(page_2_image),
                blocks=[
                    ContentBlock(
                        block_id="q15",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=120, width=500, height=160),
                        reading_order=0,
                        text="15. next page child",
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-2-problem-15",
                        subject=Subject.KOREAN,
                        title="15.",
                        stem_block_ids=["q15"],
                        metadata={"problem_number": 15},
                    ),
                ],
            )

            with (
                mock.patch("build_mvp_export.prepare_source_pages", return_value=prepared_pages),
                mock.patch("build_mvp_export.build_page_model", side_effect=[page_1, page_2]),
            ):
                result = run_mvp_export(
                    source,
                    output_dir=root / "out",
                    subject_name="korean",
                    ocr="none",
                    sync_ui=False,
                )

            problems_by_id = {problem["id"]: problem for problem in result["ui_session"]["problems"]}
            linked_problem = problems_by_id["page-2-problem-15"]
            self.assertEqual(15, linked_problem["problemNumber"])
            self.assertEqual("page-1-passage-13-16", linked_problem["passageGroupId"])
            self.assertEqual({"start": 13, "end": 16}, linked_problem["passageRange"])
            self.assertEqual([13, 14, 15, 16], linked_problem["passageChildProblemNumbers"])
            self.assertEqual(["page-1", "page-2"], linked_problem["passageSourcePageIds"])
            self.assertTrue(linked_problem["passageContinuesAcrossPages"])
            self.assertIn("passage_cross_page_merge_check", linked_problem["riskFlags"])
            self.assertEqual("check_needed", linked_problem["reviewStatus"])
            self.assertEqual(1, result["ui_session"]["passageGroupCount"])
            self.assertEqual(1, result["ui_session"]["crossPagePassageGroupCount"])

    def test_mvp_export_infers_passage_groups_from_hwp_preview_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            page_1_image = root / "page-1.png"
            page_2_image = root / "page-2.png"
            for path in (page_1_image, page_2_image):
                Image.new("RGB", (900, 1200), "white").save(path)
            prepared_pages = [
                PreparedPage(
                    page_id="page-1",
                    source_path=str(page_1_image),
                    page_number=1,
                    image=Image.open(page_1_image).convert("RGB"),
                    original_size=(900, 1200),
                ),
                PreparedPage(
                    page_id="page-2",
                    source_path=str(page_2_image),
                    page_number=2,
                    image=Image.open(page_2_image).convert("RGB"),
                    original_size=(900, 1200),
                ),
            ]
            hwp_preview_text = (
                "[1～3] 다음 글을 읽고 물음에 답하시오.\n"
                "1. first\n2. second\n3. third\n"
                "[4~5] 다음 자료를 보고 물음에 답하시오.\n"
                "4. fourth\n5. fifth"
            )
            page_1 = PageModel(
                page_id="page-1",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(page_1_image),
                blocks=[
                    ContentBlock(
                        block_id="q1",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=80, width=500, height=120),
                        reading_order=0,
                        text="1. first",
                    ),
                    ContentBlock(
                        block_id="q2",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=260, width=500, height=120),
                        reading_order=1,
                        text="2. second",
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-problem-1",
                        subject=Subject.KOREAN,
                        title="1.",
                        stem_block_ids=["q1"],
                        metadata={"problem_number": 1},
                    ),
                    ProblemUnit(
                        unit_id="page-1-problem-2",
                        subject=Subject.KOREAN,
                        title="2.",
                        stem_block_ids=["q2"],
                        metadata={"problem_number": 2},
                    ),
                ],
                metadata={
                    "source_type": "hwp",
                    "hwp_preview_text": hwp_preview_text,
                },
            )
            page_2 = PageModel(
                page_id="page-2",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(page_2_image),
                blocks=[
                    ContentBlock(
                        block_id="q3",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=80, width=500, height=120),
                        reading_order=0,
                        text="3. third",
                    ),
                    ContentBlock(
                        block_id="q4",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=260, width=500, height=120),
                        reading_order=1,
                        text="4. fourth",
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-2-problem-3",
                        subject=Subject.KOREAN,
                        title="3.",
                        stem_block_ids=["q3"],
                        metadata={"problem_number": 3},
                    ),
                    ProblemUnit(
                        unit_id="page-2-problem-4",
                        subject=Subject.KOREAN,
                        title="4.",
                        stem_block_ids=["q4"],
                        metadata={"problem_number": 4},
                    ),
                ],
                metadata={
                    "source_type": "hwp",
                    "hwp_preview_text": hwp_preview_text,
                },
            )

            with (
                mock.patch("build_mvp_export.prepare_source_pages", return_value=prepared_pages),
                mock.patch("build_mvp_export.build_page_model", side_effect=[page_1, page_2]),
            ):
                result = run_mvp_export(
                    source,
                    output_dir=root / "out",
                    subject_name="korean",
                    ocr="none",
                    sync_ui=False,
                )

            problems_by_id = {problem["id"]: problem for problem in result["ui_session"]["problems"]}
            grouped_ids = ["page-1-problem-1", "page-1-problem-2", "page-2-problem-3"]
            for problem_id in grouped_ids:
                problem = problems_by_id[problem_id]
                self.assertEqual("hwp-preview-passage-1-3", problem["passageGroupId"])
                self.assertEqual({"start": 1, "end": 3}, problem["passageRange"])
                self.assertEqual([1, 2, 3], problem["passageChildProblemNumbers"])
            self.assertEqual(["page-1", "page-2"], problems_by_id["page-2-problem-3"]["passageSourcePageIds"])
            self.assertTrue(problems_by_id["page-2-problem-3"]["passageContinuesAcrossPages"])
            self.assertIn("passage_cross_page_merge_check", problems_by_id["page-2-problem-3"]["riskFlags"])
            self.assertEqual(2, result["ui_session"]["passageGroupCount"])
            self.assertEqual(1, result["ui_session"]["crossPagePassageGroupCount"])

    def test_problem_ui_session_infers_tamgu_passage_groups_from_hwp_text_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            page_1_image = root / "page-1.png"
            page_2_image = root / "page-2.png"
            crop = root / "crop.png"
            for path in (page_1_image, page_2_image, crop):
                Image.new("RGB", (900, 1200), "white").save(path)

            prepared_pages = [
                PreparedPage(
                    page_id="page-1",
                    source_path=str(page_1_image),
                    page_number=1,
                    image=Image.open(page_1_image).convert("RGB"),
                    original_size=(900, 1200),
                ),
                PreparedPage(
                    page_id="page-2",
                    source_path=str(page_2_image),
                    page_number=2,
                    image=Image.open(page_2_image).convert("RGB"),
                    original_size=(900, 1200),
                ),
            ]
            quality = {
                "hwp_text_passage_ranges": [
                    {"start": 5, "end": 7, "text": "[5~7] 다음 자료를 보고 물음에 답하시오."}
                ]
            }
            page_1 = PageModel(
                page_id="page-1",
                width_px=900,
                height_px=1200,
                subject=Subject.SOCIAL,
                source_path=str(page_1_image),
                blocks=[],
                problems=[
                    ProblemUnit(
                        unit_id="p5",
                        subject=Subject.SOCIAL,
                        title="5.",
                        metadata={"problem_number": 5},
                    ),
                    ProblemUnit(
                        unit_id="p6",
                        subject=Subject.SOCIAL,
                        title="6.",
                        metadata={"problem_number": 6},
                    ),
                ],
                metadata={"source_type": "hwp", "hwp_conversion_quality": quality},
            )
            page_2 = PageModel(
                page_id="page-2",
                width_px=900,
                height_px=1200,
                subject=Subject.SOCIAL,
                source_path=str(page_2_image),
                blocks=[],
                problems=[
                    ProblemUnit(
                        unit_id="p7",
                        subject=Subject.SOCIAL,
                        title="7.",
                        metadata={"problem_number": 7},
                    ),
                ],
                metadata={"source_type": "hwp"},
            )
            placements = []
            for index, (problem_id, number, page_id, source_image) in enumerate(
                [
                    ("p5", 5, "page-1", page_1_image),
                    ("p6", 6, "page-1", page_1_image),
                    ("p7", 7, "page-2", page_2_image),
                ],
                start=1,
            ):
                placements.append(
                    {
                        "problem_id": problem_id,
                        "title": f"{number}.",
                        "problem_number": number,
                        "subject": Subject.SOCIAL,
                        "source_page_id": page_id,
                        "source_path": str(source_image),
                        "crop_path": str(crop),
                        "board_render_path": str(crop),
                        "bbox": {"left": 40, "top": 80 + index * 160, "width": 520, "height": 120},
                        "actual_content_height_pages": 0.8,
                        "overflow_allowed": False,
                        "start_y_pages": float(index),
                        "snapped_next_start_y_pages": float(index + 1),
                        "overflow_amount_pages": 0.0,
                        "overflow_violation": False,
                        "slot_span_count": 1,
                        "placement_x_ratio": 0.0,
                        "placement_y_ratio": 0.0,
                        "placement_scale_ratio": 1.0,
                        "record_mode": "image-only",
                        "text_record_count": 0,
                        "image_record_count": 1,
                        "risk_flags": [],
                    }
                )

            ui_session = build_problem_ui_session(
                prepared_pages,
                placements,
                root / "out",
                None,
                [source],
                record_mode="image-only",
                pages=[page_1, page_2],
            )

            problems_by_id = {problem["id"]: problem for problem in ui_session["problems"]}
            for problem_id in ("p5", "p6", "p7"):
                problem = problems_by_id[problem_id]
                self.assertEqual("hwp-text-passage-5-7", problem["passageGroupId"])
                self.assertEqual({"start": 5, "end": 7}, problem["passageRange"])
                self.assertEqual([5, 6, 7], problem["passageChildProblemNumbers"])
            self.assertEqual(["page-1", "page-2"], problems_by_id["p7"]["passageSourcePageIds"])
            self.assertTrue(problems_by_id["p7"]["passageContinuesAcrossPages"])
            self.assertIn("passage_cross_page_merge_check", problems_by_id["p7"]["riskFlags"])
            self.assertEqual(1, ui_session["passageGroupCount"])
            self.assertEqual(1, ui_session["crossPagePassageGroupCount"])

    def test_problem_ui_session_does_not_apply_global_hwp_text_range_to_duplicate_problem_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            page_images = [root / "page-1.png", root / "page-2.png"]
            crop = root / "crop.png"
            for path in (*page_images, crop):
                Image.new("RGB", (900, 1200), "white").save(path)

            prepared_pages = [
                PreparedPage(
                    page_id=f"page-{index}",
                    source_path=str(path),
                    page_number=index,
                    image=Image.open(path).convert("RGB"),
                    original_size=(900, 1200),
                )
                for index, path in enumerate(page_images, start=1)
            ]
            quality = {
                "hwp_text_passage_ranges": [
                    {"start": 35, "end": 36, "text": "[35~36] 다음 글을 읽고 물음에 답하시오."}
                ]
            }
            pages = []
            placements = []
            for page_index, page_image in enumerate(page_images, start=1):
                page_id = f"page-{page_index}"
                problem_ids = [f"{page_id}-p35", f"{page_id}-p36"]
                problems = [
                    ProblemUnit(
                        unit_id=problem_ids[0],
                        subject=Subject.KOREAN,
                        title="35.",
                        metadata={"problem_number": 35},
                    ),
                    ProblemUnit(
                        unit_id=problem_ids[1],
                        subject=Subject.KOREAN,
                        title="36.",
                        metadata={"problem_number": 36},
                    ),
                ]
                pages.append(
                    PageModel(
                        page_id=page_id,
                        width_px=900,
                        height_px=1200,
                        subject=Subject.KOREAN,
                        source_path=str(page_image),
                        blocks=[],
                        problems=problems,
                        metadata={"source_type": "hwp", "hwp_conversion_quality": quality if page_index == 1 else {}},
                    )
                )
                for problem_index, (problem_id, number) in enumerate(zip(problem_ids, [35, 36]), start=1):
                    placements.append(
                        {
                            "problem_id": problem_id,
                            "title": f"{number}.",
                            "problem_number": number,
                            "subject": Subject.KOREAN,
                            "source_page_id": page_id,
                            "source_path": str(page_image),
                            "crop_path": str(crop),
                            "board_render_path": str(crop),
                            "bbox": {"left": 40, "top": 80 + problem_index * 180, "width": 520, "height": 140},
                            "actual_content_height_pages": 0.8,
                            "overflow_allowed": False,
                            "start_y_pages": float(len(placements) + 1),
                            "snapped_next_start_y_pages": float(len(placements) + 2),
                            "overflow_amount_pages": 0.0,
                            "overflow_violation": False,
                            "slot_span_count": 1,
                            "placement_x_ratio": 0.0,
                            "placement_y_ratio": 0.0,
                            "placement_scale_ratio": 1.0,
                            "record_mode": "image-only",
                            "text_record_count": 0,
                            "image_record_count": 1,
                            "risk_flags": [],
                        }
                    )

            ui_session = build_problem_ui_session(
                prepared_pages,
                placements,
                root / "out",
                None,
                [source],
                record_mode="image-only",
                pages=pages,
            )

            self.assertEqual(0, ui_session["passageGroupCount"])
            for problem in ui_session["problems"]:
                self.assertNotIn("passageGroupId", problem)
                self.assertNotIn("passage_cross_page_merge_check", problem["riskFlags"])

    def test_mvp_export_links_marker_continuation_page_to_following_passage_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            image_paths = [root / f"page-{number}.png" for number in range(1, 4)]
            for path in image_paths:
                Image.new("RGB", (900, 1200), "white").save(path)
            prepared_pages = [
                PreparedPage(
                    page_id=f"page-{number}",
                    source_path=str(image_paths[number - 1]),
                    page_number=number,
                    image=Image.open(image_paths[number - 1]).convert("RGB"),
                    original_size=(900, 1200),
                )
                for number in range(1, 4)
            ]
            page_1 = PageModel(
                page_id="page-1",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(image_paths[0]),
                blocks=[
                    ContentBlock(
                        block_id="q18",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=80, width=500, height=120),
                        reading_order=0,
                        text="18. previous passage child",
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-problem-18",
                        subject=Subject.KOREAN,
                        title="18.",
                        stem_block_ids=["q18"],
                        metadata={"problem_number": 18},
                    ),
                ],
            )
            page_2 = PageModel(
                page_id="page-2",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(image_paths[1]),
                blocks=[
                    ContentBlock(
                        block_id="continued-passage",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=60, width=760, height=1040),
                        reading_order=0,
                        text=None,
                        metadata={
                            "segmenter": "document-bands",
                            "column_index": 0,
                            "question_band_index": 0,
                            "source_band_index": 0,
                        },
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-2-problem-1",
                        subject=Subject.KOREAN,
                        title=None,
                        stem_block_ids=["continued-passage"],
                        metadata={
                            "fallback_grouping": True,
                            "grouping_source": "fallback_grouping",
                            "question_band_index": 0,
                            "column_index": 0,
                        },
                    ),
                ],
                metadata={
                    "source_type": "hwp",
                    "document_like": True,
                    "segmenter": "document-bands",
                    "pdf_problem_markers": [],
                    "hwp_conversion_quality": {
                        "has_pdf_text_markers": True,
                        "pdf_text_marker_count": 45,
                    },
                },
            )
            page_3 = PageModel(
                page_id="page-3",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(image_paths[2]),
                blocks=[
                    ContentBlock(
                        block_id="q22",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=80, width=500, height=120),
                        reading_order=0,
                        text="22. following passage child",
                    ),
                    ContentBlock(
                        block_id="q23",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=260, width=500, height=120),
                        reading_order=1,
                        text="23. following passage child",
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-3-problem-22",
                        subject=Subject.KOREAN,
                        title="22.",
                        stem_block_ids=["q22"],
                        metadata={"problem_number": 22},
                    ),
                    ProblemUnit(
                        unit_id="page-3-problem-23",
                        subject=Subject.KOREAN,
                        title="23.",
                        stem_block_ids=["q23"],
                        metadata={"problem_number": 23},
                    ),
                ],
            )

            with (
                mock.patch("build_mvp_export.prepare_source_pages", return_value=prepared_pages),
                mock.patch("build_mvp_export.build_page_model", side_effect=[page_1, page_2, page_3]),
            ):
                result = run_mvp_export(
                    source,
                    output_dir=root / "out",
                    subject_name="korean",
                    ocr="none",
                    sync_ui=False,
                )

            problems_by_id = {problem["id"]: problem for problem in result["ui_session"]["problems"]}
            continuation = problems_by_id["page-2-continuation"]
            problem_22 = problems_by_id["page-3-problem-22"]
            problem_23 = problems_by_id["page-3-problem-23"]
            for problem in (continuation, problem_22, problem_23):
                self.assertEqual("hwp-continuation-passage-22-23", problem["passageGroupId"])
                self.assertEqual({"start": 22, "end": 23}, problem["passageRange"])
                self.assertEqual([22, 23], problem["passageChildProblemNumbers"])
                self.assertEqual(["page-2", "page-3"], problem["passageSourcePageIds"])
                self.assertTrue(problem["passageContinuesAcrossPages"])
            self.assertEqual("passage_fragment", continuation["passageRole"])
            self.assertIn("marker_document_continuation", continuation["riskFlags"])
            self.assertIn("passage_cross_page_merge_check", problem_22["riskFlags"])
            self.assertEqual(1, result["ui_session"]["supplemental_item_count"])
            self.assertEqual(1, result["ui_session"]["passageGroupCount"])
            self.assertEqual(1, result["ui_session"]["crossPagePassageGroupCount"])

    def test_mvp_export_links_marker_continuation_page_to_single_english_passage_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            image_paths = [root / f"page-{number}.png" for number in range(1, 4)]
            for path in image_paths:
                Image.new("RGB", (900, 1200), "white").save(path)
            prepared_pages = [
                PreparedPage(
                    page_id=f"page-{number}",
                    source_path=str(image_paths[number - 1]),
                    page_number=number,
                    image=Image.open(image_paths[number - 1]).convert("RGB"),
                    original_size=(900, 1200),
                )
                for number in range(1, 4)
            ]
            page_1 = PageModel(
                page_id="page-1",
                width_px=900,
                height_px=1200,
                subject=Subject.ENGLISH,
                source_path=str(image_paths[0]),
                blocks=[
                    ContentBlock(
                        block_id="q30",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=80, width=500, height=120),
                        reading_order=0,
                        text="30. previous question",
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-problem-30",
                        subject=Subject.ENGLISH,
                        title="30.",
                        stem_block_ids=["q30"],
                        metadata={"problem_number": 30},
                    ),
                ],
            )
            page_2 = PageModel(
                page_id="page-2",
                width_px=900,
                height_px=1200,
                subject=Subject.ENGLISH,
                source_path=str(image_paths[1]),
                blocks=[
                    ContentBlock(
                        block_id="continued-english-passage",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=60, width=760, height=1040),
                        reading_order=0,
                        text=None,
                        metadata={
                            "segmenter": "document-bands",
                            "column_index": 0,
                            "question_band_index": 0,
                            "source_band_index": 0,
                        },
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-2-problem-1",
                        subject=Subject.ENGLISH,
                        title=None,
                        stem_block_ids=["continued-english-passage"],
                        metadata={
                            "fallback_grouping": True,
                            "grouping_source": "fallback_grouping",
                            "question_band_index": 0,
                            "column_index": 0,
                        },
                    ),
                ],
                metadata={
                    "source_type": "hwp",
                    "document_like": True,
                    "segmenter": "document-bands",
                    "pdf_problem_markers": [],
                    "hwp_conversion_quality": {
                        "has_pdf_text_markers": True,
                        "pdf_text_marker_count": 45,
                    },
                },
            )
            page_3 = PageModel(
                page_id="page-3",
                width_px=900,
                height_px=1200,
                subject=Subject.ENGLISH,
                source_path=str(image_paths[2]),
                blocks=[
                    ContentBlock(
                        block_id="q31",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=80, width=500, height=420),
                        reading_order=0,
                        text="31. single long passage question",
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-3-problem-31",
                        subject=Subject.ENGLISH,
                        title="31.",
                        stem_block_ids=["q31"],
                        metadata={"problem_number": 31},
                    ),
                ],
            )

            with (
                mock.patch("build_mvp_export.prepare_source_pages", return_value=prepared_pages),
                mock.patch("build_mvp_export.build_page_model", side_effect=[page_1, page_2, page_3]),
            ):
                result = run_mvp_export(
                    source,
                    output_dir=root / "out",
                    subject_name="english",
                    ocr="none",
                    sync_ui=False,
                )

            problems_by_id = {problem["id"]: problem for problem in result["ui_session"]["problems"]}
            continuation = problems_by_id["page-2-continuation"]
            problem_31 = problems_by_id["page-3-problem-31"]
            for problem in (continuation, problem_31):
                self.assertEqual("hwp-continuation-passage-31", problem["passageGroupId"])
                self.assertEqual({"start": 31, "end": 31}, problem["passageRange"])
                self.assertEqual([31], problem["passageChildProblemNumbers"])
                self.assertEqual(["page-2", "page-3"], problem["passageSourcePageIds"])
                self.assertTrue(problem["passageContinuesAcrossPages"])
            self.assertEqual("passage_fragment", continuation["passageRole"])
            self.assertEqual("child_question", problem_31["passageRole"])
            self.assertIn("marker_document_continuation", continuation["riskFlags"])
            self.assertIn("passage_cross_page_merge_check", problem_31["riskFlags"])
            self.assertEqual(1, result["ui_session"]["supplemental_item_count"])
            self.assertEqual(1, result["ui_session"]["passageGroupCount"])
            self.assertEqual(1, result["ui_session"]["crossPagePassageGroupCount"])

    def test_later_child_page_prefix_is_not_promoted_to_passage_fragment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page_1_image = root / "page-1.png"
            page_2_image = root / "page-2.png"
            for path in (page_1_image, page_2_image):
                Image.new("RGB", (900, 1200), "white").save(path)
            prepared_pages = [
                PreparedPage(
                    page_id="page-1",
                    source_path=str(page_1_image),
                    page_number=1,
                    image=Image.open(page_1_image).convert("RGB"),
                    original_size=(900, 1200),
                ),
                PreparedPage(
                    page_id="page-2",
                    source_path=str(page_2_image),
                    page_number=2,
                    image=Image.open(page_2_image).convert("RGB"),
                    original_size=(900, 1200),
                ),
            ]
            page_1 = PageModel(
                page_id="page-1",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(page_1_image),
                blocks=[
                    ContentBlock(
                        block_id="q13",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=80, width=500, height=120),
                        reading_order=0,
                        text="13. first child",
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-problem-13",
                        subject=Subject.KOREAN,
                        title="13.",
                        stem_block_ids=["q13"],
                        metadata={
                            "problem_number": 13,
                            "passage_group_id": "page-1-passage-13-16",
                            "passage_range": {"start": 13, "end": 16},
                            "passage_role": "child_question",
                            "shared_passage_block_ids": ["range-header", "shared-passage-a"],
                            "passage_child_problem_numbers": [13, 14, 15, 16],
                        },
                    )
                ],
            )
            page_2 = PageModel(
                page_id="page-2",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(page_2_image),
                blocks=[
                    ContentBlock(
                        block_id="passage-continuation",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=40, width=500, height=180),
                        reading_order=0,
                        text="앞 페이지에서 이어지는 긴 지문 내용이다.",
                    ),
                    ContentBlock(
                        block_id="q15",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=260, width=500, height=520),
                        reading_order=1,
                        text="15. 위 글에 대한 설명으로 적절한 것은?",
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-2-continuation",
                        subject=Subject.KOREAN,
                        title="지문 이어짐",
                        stem_block_ids=["passage-continuation"],
                    ),
                    ProblemUnit(
                        unit_id="page-2-problem-15",
                        subject=Subject.KOREAN,
                        title="15.",
                        stem_block_ids=["q15"],
                        metadata={"problem_number": 15},
                    ),
                ],
            )

            entries = build_problem_entries(
                prepared_pages,
                [page_1, page_2],
                root / "out",
                LayoutTemplate(name="academy-default"),
            )

            prefix_entry = next(entry for entry in entries if entry.problem_id == "page-2-continuation")
            problem_15 = next(entry for entry in entries if entry.problem_id == "page-2-problem-15")
            self.assertEqual("지문 이어짐", prefix_entry.title)
            self.assertIsNone(page_2.problems[0].metadata.get("passage_role"))
            self.assertFalse(page_2.problems[0].metadata.get("supplemental_item"))
            self.assertIsNone(page_2.problems[0].metadata.get("passage_group_id"))
            self.assertIn("passage_cross_page_merge_check", problem_15.risk_flags)

    def test_problem_entries_stitch_cross_page_passage_fragments_into_one_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page_1_image = root / "page-1.png"
            page_2_image = root / "page-2.png"
            first_image = Image.new("RGB", (900, 1200), "white")
            second_image = Image.new("RGB", (900, 1200), "white")
            ImageDraw.Draw(first_image).rectangle((80, 100, 520, 260), fill=(220, 20, 20))
            ImageDraw.Draw(second_image).rectangle((80, 80, 520, 240), fill=(20, 40, 220))
            first_image.save(page_1_image)
            second_image.save(page_2_image)

            prepared_pages = [
                PreparedPage(
                    page_id="page-1",
                    source_path=str(page_1_image),
                    page_number=1,
                    image=first_image,
                    original_size=first_image.size,
                ),
                PreparedPage(
                    page_id="page-2",
                    source_path=str(page_2_image),
                    page_number=2,
                    image=second_image,
                    original_size=second_image.size,
                ),
            ]
            passage_metadata = {
                "passage_group_id": "page-1-passage-13-14",
                "passage_range": {"start": 13, "end": 14},
                "passage_child_problem_numbers": [13, 14],
            }
            page_1 = PageModel(
                page_id="page-1",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(page_1_image),
                blocks=[
                    ContentBlock(
                        block_id="passage-part-1",
                        block_type=BlockType.STEM,
                        bbox=Box(left=60, top=80, width=500, height=220),
                        reading_order=0,
                        text="첫 페이지 지문",
                    ),
                    ContentBlock(
                        block_id="q13",
                        block_type=BlockType.STEM,
                        bbox=Box(left=60, top=360, width=500, height=180),
                        reading_order=1,
                        text="13. 첫 번째 문항",
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-passage-fragment",
                        subject=Subject.KOREAN,
                        title="지문 13~14",
                        stem_block_ids=["passage-part-1"],
                        metadata={
                            **passage_metadata,
                            "passage_role": "passage_fragment",
                            "supplemental_item": True,
                        },
                    ),
                    ProblemUnit(
                        unit_id="page-1-problem-13",
                        subject=Subject.KOREAN,
                        title="13.",
                        stem_block_ids=["q13"],
                        metadata={
                            **passage_metadata,
                            "problem_number": 13,
                            "passage_role": "child_question",
                        },
                    ),
                ],
            )
            page_2 = PageModel(
                page_id="page-2",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(page_2_image),
                blocks=[
                    ContentBlock(
                        block_id="passage-part-2",
                        block_type=BlockType.STEM,
                        bbox=Box(left=60, top=60, width=500, height=220),
                        reading_order=0,
                        text="둘째 페이지에서 이어지는 지문",
                    ),
                    ContentBlock(
                        block_id="q14",
                        block_type=BlockType.STEM,
                        bbox=Box(left=60, top=340, width=500, height=180),
                        reading_order=1,
                        text="14. 두 번째 문항",
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-2-passage-fragment",
                        subject=Subject.KOREAN,
                        title="지문 이어짐",
                        stem_block_ids=["passage-part-2"],
                        metadata={
                            **passage_metadata,
                            "passage_role": "passage_fragment",
                            "supplemental_item": True,
                        },
                    ),
                    ProblemUnit(
                        unit_id="page-2-problem-14",
                        subject=Subject.KOREAN,
                        title="14.",
                        stem_block_ids=["q14"],
                        metadata={
                            **passage_metadata,
                            "problem_number": 14,
                            "passage_role": "child_question",
                        },
                    ),
                ],
            )

            entries = build_problem_entries(
                prepared_pages,
                [page_1, page_2],
                root / "out",
                LayoutTemplate(name="academy-default"),
            )

            passage_entries = [entry for entry in entries if entry.problem_number is None]
            self.assertEqual(["page-1-passage-fragment"], [entry.problem_id for entry in passage_entries])
            self.assertEqual(3, len(entries))
            passage_entry = passage_entries[0]
            with Image.open(passage_entry.crop_path).convert("RGB") as stitched:
                pixels = list(stitched.get_flattened_data())
            self.assertGreater(sum(1 for red, green, blue in pixels if red > 180 and green < 80 and blue < 80), 1000)
            self.assertGreater(sum(1 for red, green, blue in pixels if blue > 180 and red < 80 and green < 100), 1000)
            self.assertEqual([], passage_entry.blocks)

            by_id = {entry.problem_id: entry for entry in entries}
            self.assertEqual(["q13"], [block.block_id for block in by_id["page-1-problem-13"].blocks])
            self.assertEqual(["q14"], [block.block_id for block in by_id["page-2-problem-14"].blocks])
            for entry in entries:
                self.assertNotIn("passage_cross_page_merge_check", entry.risk_flags)

            records, placements, _header_flag = problem_board.build_records(
                entries,
                LayoutTemplate(name="academy-default"),
                record_mode="mixed",
                output_dir=root / "out",
                text_confidence_threshold=0.78,
                dark_board=False,
            )
            self.assertEqual(3, len(records))
            self.assertEqual(3, len(placements))
            self.assertEqual("page-1-passage-fragment", placements[0]["problem_id"])
            self.assertEqual(1, placements[0]["image_record_count"])
            self.assertEqual(0, placements[0]["text_record_count"])

            ui_session = build_problem_ui_session(
                prepared_pages,
                placements,
                root / "out",
                None,
                [page_1_image, page_2_image],
                record_mode="mixed",
                pages=[page_1, page_2],
                template=LayoutTemplate(name="academy-default"),
            )
            session_by_id = {problem["id"]: problem for problem in ui_session["problems"]}
            self.assertNotIn("page-2-passage-fragment", session_by_id)
            stitched_payload = session_by_id["page-1-passage-fragment"]
            self.assertTrue(stitched_payload["passageFragmentsMerged"])
            self.assertEqual(
                ["page-1", "page-2"],
                stitched_payload["passageMergedSourcePageIds"],
            )
            self.assertNotIn("passage_cross_page_merge_check", stitched_payload["riskFlags"])
            self.assertEqual(1, ui_session["crossPagePassageGroupCount"])

            primary_metadata = page_1.problems[0].metadata
            self.assertTrue(primary_metadata.get("passage_fragments_merged"))
            self.assertEqual(
                ["page-1-passage-fragment", "page-2-passage-fragment"],
                primary_metadata.get("passage_merged_fragment_ids"),
            )
            self.assertEqual(
                "page-1-passage-fragment",
                page_2.problems[0].metadata.get("passage_merged_into_problem_id"),
            )

    def test_pdf_text_before_first_cross_page_child_is_stitched_into_passage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page_1_image = root / "page-1.png"
            page_2_image = root / "page-2.png"
            first_image = Image.new("RGB", (900, 1200), "white")
            second_image = Image.new("RGB", (900, 1200), "white")
            ImageDraw.Draw(first_image).rectangle((60, 760, 760, 1080), fill=(220, 20, 20))
            ImageDraw.Draw(second_image).rectangle((60, 80, 760, 220), fill=(20, 40, 220))
            first_image.save(page_1_image)
            second_image.save(page_2_image)

            prepared_pages = [
                PreparedPage(
                    page_id="page-1",
                    source_path=str(page_1_image),
                    page_number=1,
                    image=first_image,
                    original_size=first_image.size,
                ),
                PreparedPage(
                    page_id="page-2",
                    source_path=str(page_2_image),
                    page_number=2,
                    image=second_image,
                    original_size=second_image.size,
                    metadata={
                        "pdf_text_lines": [
                            {
                                "text": "앞 페이지에서 이어지는 공통 지문의 첫 번째 줄입니다.",
                                "bbox": {"left": 60, "top": 80, "right": 760, "bottom": 115},
                            },
                            {
                                "text": "문항이 시작되기 전까지 이어지는 두 번째 줄입니다.",
                                "bbox": {"left": 60, "top": 140, "right": 730, "bottom": 175},
                            },
                        ]
                    },
                ),
            ]
            passage_metadata = {
                "passage_group_id": "page-1-passage-24-27",
                "passage_range": {"start": 24, "end": 27},
                "passage_child_problem_numbers": [24, 25, 26, 27],
            }
            page_1 = PageModel(
                page_id="page-1",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(page_1_image),
                blocks=[
                    ContentBlock(
                        block_id="passage-part-1",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=740, width=760, height=380),
                        reading_order=0,
                        text="첫 페이지에서 시작한 공통 지문",
                    )
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-passage-fragment",
                        subject=Subject.KOREAN,
                        title="지문 24~27",
                        stem_block_ids=["passage-part-1"],
                        metadata={
                            **passage_metadata,
                            "passage_role": "passage_fragment",
                            "supplemental_item": True,
                            "passage_detection_confidence": 0.9,
                            "passage_text_line_count": 1,
                            "passage_text_character_count": 10,
                            "passage_text_bounds_score": 1.0,
                        },
                    )
                ],
            )
            page_2 = PageModel(
                page_id="page-2",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(page_2_image),
                blocks=[
                    ContentBlock(
                        block_id="q24",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=260, width=760, height=440),
                        reading_order=0,
                        text="24. 첫 번째 문항",
                        metadata={"column_index": 1},
                    )
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-2-problem-24",
                        subject=Subject.KOREAN,
                        title="24.",
                        stem_block_ids=["q24"],
                        metadata={"problem_number": 24},
                    )
                ],
            )

            entries = build_problem_entries(
                prepared_pages,
                [page_1, page_2],
                root / "out",
                LayoutTemplate(name="academy-default"),
                content_target="shared-passages",
            )

            self.assertEqual(1, len(entries))
            passage_entry = entries[0]
            with Image.open(passage_entry.crop_path).convert("RGB") as stitched:
                pixels = list(stitched.get_flattened_data())
            self.assertGreater(sum(1 for red, green, blue in pixels if red > 180 and green < 80 and blue < 80), 1000)
            self.assertGreater(sum(1 for red, green, blue in pixels if blue > 180 and red < 80 and green < 100), 1000)
            self.assertEqual([], passage_entry.blocks)
            self.assertNotIn("passage_cross_page_merge_check", passage_entry.risk_flags)
            continuation = next(
                problem
                for problem in page_2.problems
                if problem.metadata.get("passage_pre_question_continuation")
            )
            self.assertEqual("passage_fragment", continuation.metadata.get("passage_role"))
            self.assertTrue(continuation.metadata.get("passage_fragments_merged"))
            primary = page_1.problems[0]
            self.assertEqual(3, primary.metadata.get("passage_text_line_count"))
            self.assertGreater(primary.metadata.get("passage_text_character_count", 0), 40)
            self.assertTrue(passage_entry.board_render_path.is_file())

    def test_nested_question_material_is_not_exported_as_a_passage(self):
        parent_group = "page-1-passage-11-14"
        page_1 = PageModel(
            page_id="page-1",
            width_px=900,
            height_px=1200,
            subject=Subject.KOREAN,
            blocks=[
                ContentBlock(
                    block_id="passage-11-14",
                    block_type=BlockType.STEM,
                    bbox=Box(left=40, top=120, width=380, height=600),
                    reading_order=0,
                    text="[11~14] 다음 글을 읽고 물음에 답하시오.",
                ),
                ContentBlock(
                    block_id="q11",
                    block_type=BlockType.STEM,
                    bbox=Box(left=460, top=120, width=380, height=240),
                    reading_order=1,
                    text="11. 첫 문항",
                ),
            ],
            problems=[
                ProblemUnit(
                    unit_id=parent_group,
                    subject=Subject.KOREAN,
                    title="지문 11~14",
                    stem_block_ids=["passage-11-14"],
                    metadata={
                        "passage_group_id": parent_group,
                        "passage_range": {"start": 11, "end": 14},
                        "passage_role": "passage_fragment",
                        "passage_child_problem_numbers": [11, 12, 13, 14],
                        "shared_passage_block_ids": ["passage-11-14"],
                    },
                ),
                ProblemUnit(
                    unit_id="q11",
                    subject=Subject.KOREAN,
                    title="11.",
                    stem_block_ids=["q11"],
                    metadata={
                        "problem_number": 11,
                        "passage_group_id": parent_group,
                        "passage_range": {"start": 11, "end": 14},
                        "passage_role": "child_question",
                        "passage_child_problem_numbers": [11, 12, 13, 14],
                    },
                ),
            ],
        )
        nested_group = "page-2-passage-13-14"
        page_2 = PageModel(
            page_id="page-2",
            width_px=900,
            height_px=1200,
            subject=Subject.KOREAN,
            blocks=[
                ContentBlock(
                    block_id="material-13-14",
                    block_type=BlockType.IMAGE,
                    bbox=Box(left=40, top=120, width=380, height=300),
                    reading_order=0,
                    text="[13~14] <보기>를 참고하시오.",
                    metadata={"shared_passage": True},
                ),
                ContentBlock(
                    block_id="q13",
                    block_type=BlockType.STEM,
                    bbox=Box(left=40, top=460, width=380, height=260),
                    reading_order=1,
                    text="13. 자료 문항",
                ),
            ],
            problems=[
                ProblemUnit(
                    unit_id=nested_group,
                    subject=Subject.KOREAN,
                    title="지문 13~14",
                    figure_block_ids=["material-13-14"],
                    metadata={
                        "passage_group_id": nested_group,
                        "passage_range": {"start": 13, "end": 14},
                        "passage_role": "passage_fragment",
                        "passage_child_problem_numbers": [13, 14],
                        "shared_passage_block_ids": ["material-13-14"],
                        "supplemental_item": True,
                    },
                ),
                ProblemUnit(
                    unit_id="q13",
                    subject=Subject.KOREAN,
                    title="13.",
                    stem_block_ids=["q13"],
                    metadata={
                        "problem_number": 13,
                        "passage_group_id": nested_group,
                        "passage_range": {"start": 13, "end": 14},
                        "passage_role": "child_question",
                        "passage_child_problem_numbers": [13, 14],
                    },
                ),
            ],
        )

        problem_board._annotate_cross_page_passage_groups([page_1, page_2])

        material = next(problem for problem in page_2.problems if problem.unit_id == nested_group)
        child = next(problem for problem in page_2.problems if problem.unit_id == "q13")
        self.assertEqual("question_material_fragment", material.metadata.get("passage_role"))
        self.assertTrue(material.metadata.get("nested_passage_suppressed"))
        self.assertFalse(problem_board._problem_matches_content_target(material, "shared-passages"))
        self.assertEqual(parent_group, child.metadata.get("passage_group_id"))
        self.assertEqual({"start": 11, "end": 14}, child.metadata.get("passage_range"))
        self.assertFalse(page_2.blocks[0].metadata.get("shared_passage"))

    def test_later_child_page_content_is_not_materialized_as_passage_continuation(self):
        page_1 = PageModel(
            page_id="page-1",
            width_px=900,
            height_px=1200,
            subject=Subject.KOREAN,
            blocks=[
                ContentBlock(
                    block_id="q24",
                    block_type=BlockType.STEM,
                    bbox=Box(left=40, top=300, width=760, height=400),
                    reading_order=0,
                    text="24. first child",
                    metadata={"column_index": 1},
                )
            ],
            problems=[
                ProblemUnit(
                    unit_id="page-1-problem-24",
                    subject=Subject.KOREAN,
                    title="24.",
                    stem_block_ids=["q24"],
                    metadata={
                        "problem_number": 24,
                        "passage_group_id": "passage-24-27",
                        "passage_range": {"start": 24, "end": 27},
                        "passage_role": "child_question",
                        "passage_child_problem_numbers": [24, 25, 26, 27],
                    },
                )
            ],
        )
        page_2 = PageModel(
            page_id="page-2",
            width_px=900,
            height_px=1200,
            subject=Subject.KOREAN,
            blocks=[
                ContentBlock(
                    block_id="q25",
                    block_type=BlockType.STEM,
                    bbox=Box(left=40, top=300, width=760, height=400),
                    reading_order=0,
                    text="25. later child",
                    metadata={"column_index": 1},
                )
            ],
            problems=[
                ProblemUnit(
                    unit_id="page-2-problem-25",
                    subject=Subject.KOREAN,
                    title="25.",
                    stem_block_ids=["q25"],
                    metadata={"problem_number": 25},
                )
            ],
        )
        prepared = PreparedPage(
            page_id="page-2",
            source_path="page-2.pdf",
            page_number=2,
            image=Image.new("RGB", (900, 1200), "white"),
            original_size=(900, 1200),
            metadata={
                "pdf_text_lines": [
                    {
                        "text": "앞 문항의 선택지 내용이 다음 페이지 위쪽에 남아 있습니다.",
                        "bbox": {"left": 60, "top": 80, "right": 760, "bottom": 115},
                    },
                    {
                        "text": "이 내용은 공통 지문으로 다시 붙으면 안 됩니다.",
                        "bbox": {"left": 60, "top": 140, "right": 730, "bottom": 175},
                    },
                ]
            },
        )

        problem_board._annotate_cross_page_passage_groups([page_1, page_2])
        problem_board._materialize_pdf_pre_question_passage_continuations(
            [page_1, page_2],
            {"page-2": prepared},
        )

        self.assertFalse(any(
            problem.metadata.get("passage_pre_question_continuation")
            for problem in page_2.problems
        ))

    def test_completed_passage_group_is_not_revived_by_next_page_numbered_material(self):
        group_id = "page-1-passage-1-2"
        page_1 = PageModel(
            page_id="page-1",
            width_px=900,
            height_px=1200,
            subject=Subject.KOREAN,
            metadata={"source_type": "pdf", "segmenter": "pdf-text-markers"},
            blocks=[
                ContentBlock(
                    block_id="passage-1-2",
                    block_type=BlockType.IMAGE,
                    bbox=Box(left=40, top=720, width=820, height=420),
                    reading_order=0,
                    metadata={"shared_passage": True},
                ),
                ContentBlock(
                    block_id="q1",
                    block_type=BlockType.STEM,
                    bbox=Box(left=40, top=100, width=380, height=240),
                    reading_order=1,
                    text="1. 첫 문항",
                ),
                ContentBlock(
                    block_id="q2",
                    block_type=BlockType.STEM,
                    bbox=Box(left=460, top=100, width=380, height=240),
                    reading_order=2,
                    text="2. 둘째 문항",
                ),
            ],
            problems=[
                ProblemUnit(
                    unit_id=group_id,
                    subject=Subject.KOREAN,
                    title="지문 1~2",
                    figure_block_ids=["passage-1-2"],
                    metadata={
                        "passage_group_id": group_id,
                        "passage_range": {"start": 1, "end": 2},
                        "passage_role": "passage_fragment",
                        "passage_child_problem_numbers": [1, 2],
                        "shared_passage_block_ids": ["passage-1-2"],
                    },
                ),
                *[
                    ProblemUnit(
                        unit_id=f"page-1-problem-{number}",
                        subject=Subject.KOREAN,
                        title=f"{number}.",
                        stem_block_ids=[f"q{number}"],
                        metadata={
                            "problem_number": number,
                            "passage_group_id": group_id,
                            "passage_range": {"start": 1, "end": 2},
                            "passage_role": "child_question",
                            "passage_child_problem_numbers": [1, 2],
                        },
                    )
                    for number in (1, 2)
                ],
            ],
        )
        page_2 = PageModel(
            page_id="page-2",
            width_px=900,
            height_px=1200,
            subject=Subject.KOREAN,
            metadata={
                "source_type": "pdf",
                "segmenter": "pdf-text-markers",
                "pdf_pre_question_text_regions": [
                    {
                        "before_problem_number": 1,
                        "column_index": 1,
                        "bbox": {"left": 40, "top": 80, "right": 420, "bottom": 500},
                    }
                ],
            },
            blocks=[
                ContentBlock(
                    block_id="page-2-material-1",
                    block_type=BlockType.STEM,
                    bbox=Box(left=40, top=520, width=380, height=120),
                    reading_order=0,
                    text="1. 도표 안의 항목 번호",
                )
            ],
            problems=[
                ProblemUnit(
                    unit_id="page-2-material-1",
                    subject=Subject.KOREAN,
                    title="1.",
                    stem_block_ids=["page-2-material-1"],
                    metadata={"problem_number": 1},
                )
            ],
        )

        problem_board._annotate_cross_page_passage_groups([page_1, page_2])

        self.assertFalse(any(
            problem.metadata.get("cross_page_passage_inferred")
            for problem in page_2.problems
        ))
        self.assertIsNone(page_2.problems[0].metadata.get("passage_group_id"))
        self.assertEqual(["page-1"], page_1.problems[0].metadata.get("passage_source_page_ids"))

    def test_numbered_question_before_first_passage_child_is_not_swallowed(self):
        page = PageModel(
            page_id="page-2",
            width_px=900,
            height_px=1200,
            subject=Subject.KOREAN,
            blocks=[
                ContentBlock(
                    block_id="q42",
                    block_type=BlockType.STEM,
                    bbox=Box(left=460, top=80, width=400, height=440),
                    reading_order=0,
                    text="42. 앞에 있는 독립 문항",
                    metadata={"column_index": 1},
                ),
                ContentBlock(
                    block_id="q43",
                    block_type=BlockType.STEM,
                    bbox=Box(left=460, top=650, width=400, height=400),
                    reading_order=1,
                    text="43. 지문의 첫 번째 문항",
                    metadata={"column_index": 1},
                ),
            ],
            problems=[
                ProblemUnit(
                    unit_id="page-2-problem-42",
                    subject=Subject.KOREAN,
                    title="42.",
                    stem_block_ids=["q42"],
                    metadata={"problem_number": 42},
                ),
                ProblemUnit(
                    unit_id="page-2-problem-43",
                    subject=Subject.KOREAN,
                    title="43.",
                    stem_block_ids=["q43"],
                    metadata={
                        "problem_number": 43,
                        "passage_group_id": "passage-43-45",
                        "passage_range": {"start": 43, "end": 45},
                        "passage_role": "child_question",
                        "passage_child_problem_numbers": [43, 44, 45],
                        "passage_source_page_ids": ["page-1", "page-2"],
                    },
                ),
            ],
        )
        prepared = PreparedPage(
            page_id="page-2",
            source_path="page-2.pdf",
            page_number=2,
            image=Image.new("RGB", (900, 1200), "white"),
            original_size=(900, 1200),
            metadata={
                "pdf_text_lines": [
                    {
                        "text": "42번 문항의 본문과 보기 카드가 이 영역을 차지합니다.",
                        "bbox": {"left": 470, "top": 100, "right": 840, "bottom": 135},
                    },
                    {
                        "text": "이 텍스트는 43~45번 공통 지문으로 합쳐지면 안 됩니다.",
                        "bbox": {"left": 470, "top": 180, "right": 840, "bottom": 215},
                    },
                ]
            },
        )

        problem_board._materialize_pdf_pre_question_passage_continuations(
            [page],
            {"page-2": prepared},
        )

        self.assertFalse(any(
            problem.metadata.get("passage_pre_question_continuation")
            for problem in page.problems
        ))
        block_by_id = {block.block_id: block for block in page.blocks}
        next_by_id = problem_board._build_crop_next_problem_map(
            page.problems,
            block_by_id,
        )
        self.assertEqual(
            "page-2-problem-43",
            next_by_id["page-2-problem-42"].unit_id,
        )

    def test_classin_handoff_manifest_explains_duplicate_problem_number_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            edb_path = root / "lesson.edb"
            source.write_bytes(b"hwp")
            edb_path.write_bytes(b"edb")
            duplicate_groups = [
                {
                    "numberStart": 35,
                    "numberEnd": 45,
                    "numberLabel": "35-45",
                    "occurrencesPerNumber": 2,
                    "duplicateRecordCount": 11,
                    "totalRecordCount": 22,
                    "sourcePageIds": ["page-13", "page-17"],
                    "problemIds": ["p35-a", "p35-b"],
                    "message": "문항 번호 35-45가 각 2회 등장합니다.",
                }
            ]

            json_path, md_path = problem_board.write_classin_handoff_manifest(
                root,
                source_paths=[source],
                edb_path=edb_path,
                ui_session={
                    "core_problem_count": 56,
                    "supplemental_item_count": 0,
                    "detected_problem_count": 56,
                    "source_page_count": 20,
                    "duplicate_problem_number_groups": duplicate_groups,
                    "reviewSummary": {},
                },
                summary={"record_count": 56, "record_mode": "image-only", "placements": []},
                template=LayoutTemplate(name="academy-default", board_page_count=112),
            )

            handoff = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = md_path.read_text(encoding="utf-8")
            self.assertEqual("ready_for_classin_review", handoff["status"])
            self.assertTrue(handoff["readyForClassIn"])
            self.assertFalse(handoff["duplicateProblemNumberGroups"][0]["blocking"])
            self.assertTrue(handoff["duplicateProblemNumberGroups"][0]["pageOrderPreserved"])
            self.assertEqual("edb_page_order", handoff["duplicateProblemNumberGroups"][0]["orderBasis"])
            self.assertEqual([], handoff["blockingDuplicateProblemNumberGroups"])
            self.assertIn("35-45 x2", handoff["duplicateProblemNumberNote"])
            self.assertIn("Duplicate problem numbers preserved in page order: 35-45 x2", markdown)

    def test_ui_session_exposes_long_image_layout_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            crop = root / "p13.png"
            source.write_bytes(b"source")
            crop.write_bytes(b"crop")

            session = build_problem_ui_session(
                prepared_pages=[],
                placements=[
                    {
                        "problem_id": "p13",
                        "title": "13. long passage",
                        "problem_number": 13,
                        "subject": "korean",
                        "crop_path": str(crop),
                        "board_render_path": str(crop),
                        "source_page_id": "page-1",
                        "source_path": str(source),
                        "start_y_pages": 0.0,
                        "actual_content_height_pages": 1.1,
                        "snapped_next_start_y_pages": 2.4,
                        "overflow_allowed": True,
                        "overflow_amount_pages": 0.34,
                        "overflow_violation": False,
                        "slot_span_count": 2,
                        "placement_x_ratio": 0.0,
                        "placement_y_ratio": 0.0,
                        "placement_scale_ratio": 1.4,
                        "bbox": {"left": 0, "top": 0, "width": 100, "height": 100},
                        "risk_flags": [],
                    }
                ],
                output_dir=root,
                edb_path=None,
                source_paths=[source],
                record_mode="image-only",
                template=LayoutTemplate(name="academy-default", board_page_count=50),
            )

            diagnostics = session["layoutDiagnostics"]
            problem_diag = session["problems"][0]["layoutDiagnostics"]
            self.assertEqual(1, diagnostics["autoExtendedCount"])
            self.assertEqual(1, diagnostics["longImageCount"])
            self.assertEqual(0, diagnostics["overlapRiskCount"])
            self.assertEqual("긴 이미지 자동 확장 1 · 최대 1.54p", diagnostics["label"])
            self.assertEqual(1.54, problem_diag["renderedHeightPages"])
            self.assertEqual(2.4, problem_diag["reservedSpanPages"])
            self.assertEqual(1.4, problem_diag["placementScaleRatio"])

    def test_classin_handoff_manifest_includes_layout_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            edb_path = root / "lesson.edb"
            source.write_bytes(b"pdf")
            edb_path.write_bytes(b"edb")
            layout_diagnostics = {
                "autoExtendedCount": 1,
                "auto_extended_count": 1,
                "overlapRiskCount": 0,
                "overlap_risk_count": 0,
                "maxRenderedHeightPages": 1.54,
                "max_rendered_height_pages": 1.54,
                "label": "긴 이미지 자동 확장 1 · 최대 1.54p",
                "items": [
                    {
                        "problemId": "p13",
                        "title": "13. long passage",
                        "renderedHeightPages": 1.54,
                        "reservedSpanPages": 2.4,
                        "placementScaleRatio": 1.4,
                    }
                ],
            }

            json_path, md_path = problem_board.write_classin_handoff_manifest(
                root,
                source_paths=[source],
                edb_path=edb_path,
                ui_session={
                    "core_problem_count": 1,
                    "supplemental_item_count": 0,
                    "detected_problem_count": 1,
                    "source_page_count": 1,
                    "reviewSummary": {},
                    "layoutDiagnostics": layout_diagnostics,
                    "problems": [],
                },
                summary={"record_count": 1, "record_mode": "image-only", "placements": []},
                template=LayoutTemplate(name="academy-default", board_page_count=50),
            )

            handoff = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = md_path.read_text(encoding="utf-8")
            self.assertEqual(layout_diagnostics, handoff["layoutDiagnostics"])
            self.assertIn("Layout Diagnostics", markdown)
            self.assertIn("긴 이미지 자동 확장 1 · 최대 1.54p", markdown)
            self.assertIn("`p13`", markdown)

    def test_classin_handoff_manifest_summarizes_passage_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            edb_path = root / "lesson.edb"
            source.write_bytes(b"hwp")
            edb_path.write_bytes(b"edb")

            json_path, md_path = problem_board.write_classin_handoff_manifest(
                root,
                source_paths=[source],
                edb_path=edb_path,
                ui_session={
                    "core_problem_count": 4,
                    "supplemental_item_count": 0,
                    "detected_problem_count": 4,
                    "source_page_count": 2,
                    "reviewSummary": {},
                    "problems": [
                        {
                            "id": "p13",
                            "title": "13.",
                            "problemNumber": 13,
                            "sourcePageId": "page-1",
                            "passageGroupId": "page-1-passage-13-16",
                            "passageRange": {"start": 13, "end": 16},
                            "passageRole": "child_question",
                            "passageChildProblemNumbers": [13, 14, 15, 16],
                            "passageSourcePageIds": ["page-1", "page-2"],
                            "passageContinuesAcrossPages": True,
                        },
                        {
                            "id": "p15",
                            "title": "15.",
                            "problemNumber": 15,
                            "sourcePageId": "page-2",
                            "passageGroupId": "page-1-passage-13-16",
                            "passageRange": {"start": 13, "end": 16},
                            "passageRole": "child_question",
                            "passageChildProblemNumbers": [13, 14, 15, 16],
                            "passageSourcePageIds": ["page-1", "page-2"],
                            "passageContinuesAcrossPages": True,
                        },
                        {
                            "id": "p16",
                            "title": "16.",
                            "problemNumber": 16,
                            "sourcePageId": "page-2",
                            "passageGroupId": "",
                            "metadata": {
                                "passage_group_id": "page-1-passage-13-16",
                                "passage_range": {"start": 13, "end": 16},
                                "passage_role": "child_question",
                                "passage_child_problem_numbers": [13, 14, 15, 16],
                                "passage_source_page_ids": ["page-1", "page-2"],
                                "passage_continues_across_pages": True,
                            },
                        },
                        {
                            "id": "p13-fragment",
                            "title": "이어지는 자료",
                            "sourcePageId": "page-1",
                            "passageGroupId": "page-1-passage-13-16",
                            "passageRange": {"start": 13, "end": 16},
                            "passageRole": "passage_fragment",
                            "passageChildProblemNumbers": [13, 14, 15, 16],
                            "passageSourcePageIds": ["page-1", "page-2"],
                            "passageContinuesAcrossPages": True,
                            "riskFlags": ["marker_document_continuation"],
                        },
                    ],
                },
                summary={"record_count": 4, "record_mode": "image-only", "placements": []},
                template=LayoutTemplate(name="academy-default", board_page_count=8),
            )

            handoff = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = md_path.read_text(encoding="utf-8")
            self.assertEqual(1, handoff["passageGroupCount"])
            self.assertEqual(1, handoff["crossPagePassageGroupCount"])
            self.assertEqual(3, handoff["passageProblemCount"])
            self.assertEqual(
                [
                    {
                        "groupId": "page-1-passage-13-16",
                        "numberStart": 13,
                        "numberEnd": 16,
                        "numberLabel": "13-16",
                        "problemNumbers": [13, 15, 16],
                        "childProblemNumbers": [13, 14, 15, 16],
                        "missingChildProblemNumbers": [14],
                        "missingChildProblemCount": 1,
                        "problemIds": ["p13", "p15", "p16", "p13-fragment"],
                        "coreProblemIds": ["p13", "p15", "p16"],
                        "fragmentProblemIds": ["p13-fragment"],
                        "sourcePageIds": ["page-1", "page-2"],
                        "sourcePageCount": 2,
                        "problemCount": 3,
                        "detectedProblemCount": 4,
                        "fragmentProblemCount": 1,
                        "continuesAcrossPages": True,
                        "roles": ["child_question", "passage_fragment"],
                        "message": "지문 묶음 13-16이 2개 원본 페이지와 3개 하위 문항, 자료 1개에 걸쳐 있습니다.",
                    }
                ],
                handoff["passageGroups"],
            )
            self.assertIn("## Passage Groups", markdown)
            self.assertIn("page-1-passage-13-16", markdown)
            self.assertIn("13-16", markdown)
            self.assertIn("cross-page", markdown)

    def test_classin_handoff_manifest_summarizes_passage_review_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            edb_path = root / "lesson.edb"
            source.write_bytes(b"hwp")
            edb_path.write_bytes(b"edb")

            json_path, md_path = problem_board.write_classin_handoff_manifest(
                root,
                source_paths=[source],
                edb_path=edb_path,
                ui_session={
                    "core_problem_count": 3,
                    "supplemental_item_count": 1,
                    "detected_problem_count": 4,
                    "source_page_count": 2,
                    "reviewSummary": {},
                    "problems": [
                        {
                            "id": "p31",
                            "title": "31.",
                            "problemNumber": 31,
                            "sourcePageId": "page-5",
                            "passageGroupId": "hwp-text-passage-31-34",
                            "passageRange": {"start": 31, "end": 34},
                            "passageRole": "child_question",
                            "passageChildProblemNumbers": [31, 32, 33, 34],
                            "passageSourcePageIds": ["page-5", "page-6"],
                            "passageContinuesAcrossPages": True,
                            "riskFlags": ["passage_cross_page_merge_check"],
                            "reviewStatus": "check_needed",
                        },
                        {
                            "id": "p32",
                            "title": "32.",
                            "problemNumber": 32,
                            "sourcePageId": "page-5",
                            "passageGroupId": "hwp-text-passage-31-34",
                            "passageRange": {"start": 31, "end": 34},
                            "passageRole": "child_question",
                            "passageChildProblemNumbers": [31, 32, 33, 34],
                            "passageSourcePageIds": ["page-5", "page-6"],
                            "passageContinuesAcrossPages": True,
                            "riskFlags": ["passage_cross_page_merge_check"],
                            "reviewStatus": "check_needed",
                        },
                        {
                            "id": "p34",
                            "title": "34.",
                            "problemNumber": 34,
                            "sourcePageId": "page-6",
                            "passageGroupId": "hwp-text-passage-31-34",
                            "passageRange": {"start": 31, "end": 34},
                            "passageRole": "child_question",
                            "passageChildProblemNumbers": [31, 32, 33, 34],
                            "passageSourcePageIds": ["page-5", "page-6"],
                            "passageContinuesAcrossPages": True,
                            "riskFlags": ["passage_cross_page_merge_check"],
                            "reviewStatus": "check_needed",
                        },
                        {
                            "id": "p31-fragment",
                            "title": "이어지는 지문",
                            "sourcePageId": "page-5",
                            "passageGroupId": "hwp-text-passage-31-34",
                            "passageRange": {"start": 31, "end": 34},
                            "passageRole": "passage_fragment",
                            "passageChildProblemNumbers": [31, 32, 33, 34],
                            "passageSourcePageIds": ["page-5", "page-6"],
                            "passageContinuesAcrossPages": True,
                            "riskFlags": ["marker_document_continuation"],
                            "reviewStatus": "check_needed",
                        },
                    ],
                },
                summary={"record_count": 4, "record_mode": "image-only", "placements": []},
                template=LayoutTemplate(name="academy-default", board_page_count=8),
            )

            handoff = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = md_path.read_text(encoding="utf-8")
            self.assertEqual(1, handoff["passageReviewItemCount"])
            self.assertEqual(1, handoff["crossPagePassageReviewItemCount"])
            self.assertEqual(
                {
                    "groupId": "hwp-text-passage-31-34",
                    "numberLabel": "31-34",
                    "problemIds": ["p31", "p32", "p34"],
                    "fragmentProblemIds": ["p31-fragment"],
                    "sourcePageIds": ["page-5", "page-6"],
                    "problemCount": 3,
                    "missingChildProblemNumbers": [33],
                    "missingChildProblemCount": 1,
                    "fragmentProblemCount": 1,
                    "continuesAcrossPages": True,
                    "reviewReasonCodes": [
                        "cross_page_passage_group",
                        "passage_fragment",
                        "passage_missing_child_questions",
                        "passage_cross_page_merge_check",
                    ],
                    "riskFlags": ["marker_document_continuation", "passage_cross_page_merge_check"],
                    "message": "31-34 지문 묶음은 2개 페이지와 3개 하위 문항, 지문 본문 1개, 누락 문항 33번을 확인해야 합니다.",
                },
                handoff["passageReviewItems"][0],
            )
            self.assertIn("## Passage Review Queue", markdown)
            self.assertIn("hwp-text-passage-31-34", markdown)
            self.assertIn("problems: p31, p32, p34", markdown)
            self.assertIn("fragments: p31-fragment", markdown)
            self.assertIn("pages: page-5, page-6", markdown)
            self.assertIn("missing: 33", markdown)
            self.assertIn("cross_page_passage_group", markdown)
            self.assertIn(
                "reasons: 페이지 이어짐 (`cross_page_passage_group`), "
                "지문 본문 (`passage_fragment`), "
                "문항 누락 (`passage_missing_child_questions`), "
                "병합 확인 (`passage_cross_page_merge_check`)",
                markdown,
            )

    def test_classin_preflight_flags_missing_passage_child_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            edb_path = root / "lesson.edb"
            source.write_bytes(b"hwp")
            edb_path.write_bytes(b"edb")
            crop_paths = {}
            for number in (31, 32, 34):
                path = root / f"p{number}.png"
                image = Image.new("RGB", (760, 420), "white")
                draw = ImageDraw.Draw(image)
                for line in range(12):
                    draw.text((40, 30 + line * 28), f"{number}. question line {line}", fill=(20, 20, 20))
                image.save(path)
                crop_paths[number] = path

            problems = []
            for index, number in enumerate((31, 32, 34), start=1):
                problems.append(
                    {
                        "id": f"p{number}",
                        "title": f"{number}.",
                        "problemNumber": number,
                        "imagePath": crop_paths[number].resolve().as_uri(),
                        "sourcePageId": f"page-{index}",
                        "bbox": {"left": 40, "top": 80, "width": 560, "height": 160},
                        "passageGroupId": "hwp-text-passage-31-34",
                        "passageRange": {"start": 31, "end": 34},
                        "passageRole": "child_question",
                        "passageChildProblemNumbers": [31, 32, 33, 34],
                        "passageSourcePageIds": ["page-1", "page-2", "page-3"],
                        "riskFlags": [],
                        "reviewStatus": "normal",
                    }
                )

            json_path, md_path = problem_board.write_classin_handoff_manifest(
                root,
                source_paths=[source],
                edb_path=edb_path,
                ui_session={
                    "core_problem_count": 3,
                    "supplemental_item_count": 0,
                    "detected_problem_count": 3,
                    "source_page_count": 3,
                    "reviewSummary": {},
                    "problems": problems,
                },
                summary={"record_count": 3, "record_mode": "image-only", "placements": []},
                template=LayoutTemplate(name="academy-default", board_page_count=8),
            )

            handoff = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = md_path.read_text(encoding="utf-8")
            preflight = handoff["classinPreflight"]
            missing_issues = [
                issue for issue in preflight["issues"] if issue["type"] == "passage_missing_child_questions"
            ]
            self.assertFalse(handoff["readyForClassIn"])
            self.assertEqual("needs_attention", preflight["status"])
            self.assertEqual(1, len(missing_issues))
            self.assertEqual("hwp-text-passage-31-34", missing_issues[0]["passageGroupId"])
            self.assertEqual([33], missing_issues[0]["missingChildProblemNumbers"])
            self.assertEqual([33], missing_issues[0]["missing_child_problem_numbers"])
            self.assertEqual(["p31", "p32", "p34"], missing_issues[0]["problemIds"])
            self.assertIn("passage_missing_child_questions", markdown)
            self.assertIn("문항 누락", markdown)

    def test_classin_preflight_allows_intentional_shared_passage_only_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            edb_path = root / "passages.edb"
            passage_crop = root / "passage-31-34.png"
            source.write_bytes(b"pdf")
            edb_path.write_bytes(b"edb")

            image = Image.new("RGB", (760, 420), "white")
            draw = ImageDraw.Draw(image)
            for line in range(14):
                y = 24 + line * 27
                draw.line((40, y, 710, y), fill=(20, 20, 20), width=3)
            image.save(passage_crop)

            problem = {
                "id": "passage-31-34",
                "title": "지문 31~34",
                "imagePath": passage_crop.resolve().as_uri(),
                "sourcePageId": "page-1",
                "bbox": {"left": 40, "top": 80, "width": 670, "height": 378},
                "passageGroupId": "passage-31-34",
                "passageRange": {"start": 31, "end": 34},
                "passageRole": "passage_fragment",
                "passageChildProblemNumbers": [31, 32, 33, 34],
                "supplementalItem": True,
                "riskFlags": [],
                "reviewStatus": "normal",
            }

            json_path, md_path = problem_board.write_classin_handoff_manifest(
                root,
                source_paths=[source],
                edb_path=edb_path,
                ui_session={
                    "contentTarget": "shared-passages",
                    "content_target": "shared-passages",
                    "core_problem_count": 0,
                    "supplemental_item_count": 1,
                    "detected_problem_count": 1,
                    "source_page_count": 1,
                    "reviewSummary": {},
                    "problems": [problem],
                },
                summary={"record_count": 1, "record_mode": "image-only", "placements": []},
                template=LayoutTemplate(name="academy-default", board_page_count=8),
            )

            handoff = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = md_path.read_text(encoding="utf-8")
            preflight = handoff["classinPreflight"]
            self.assertTrue(handoff["readyForClassIn"])
            self.assertTrue(preflight["passed"])
            self.assertEqual("passed", preflight["status"])
            self.assertEqual("shared-passages", handoff["contentTarget"])
            self.assertEqual([], handoff["passageGroups"][0]["missingChildProblemNumbers"])
            self.assertFalse(
                any(issue["type"] == "passage_missing_child_questions" for issue in preflight["issues"])
            )
            self.assertNotIn("passage_missing_child_questions", markdown)

    def test_classin_handoff_manifest_includes_asset_preflight_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            edb_path = root / "lesson.edb"
            tiny_crop = root / "tiny.png"
            blank_crop = root / "blank.png"
            missing_crop = root / "missing.png"
            source.write_bytes(b"hwp")
            edb_path.write_bytes(b"edb")
            Image.new("RGB", (90, 50), "white").save(tiny_crop)
            blank_image = Image.new("RGB", (800, 300), "white")
            ImageDraw.Draw(blank_image).line((780, 0, 780, 3), fill=(20, 20, 20), width=1)
            blank_image.save(blank_crop)

            json_path, md_path = problem_board.write_classin_handoff_manifest(
                root,
                source_paths=[source],
                edb_path=edb_path,
                ui_session={
                    "core_problem_count": 2,
                    "supplemental_item_count": 0,
                    "detected_problem_count": 2,
                    "source_page_count": 1,
                    "reviewSummary": {"riskFlagCounts": {"manual_check": 1}},
                    "problems": [
                        {
                            "id": "p-small",
                            "title": "1.",
                            "imagePath": tiny_crop.resolve().as_uri(),
                            "riskFlags": ["manual_check"],
                            "reviewStatus": "check_needed",
                        },
                        {
                            "id": "p-missing",
                            "title": "2.",
                            "imagePath": missing_crop.resolve().as_uri(),
                            "riskFlags": [],
                            "reviewStatus": "normal",
                        },
                        {
                            "id": "p-blank",
                            "title": "3.",
                            "imagePath": blank_crop.resolve().as_uri(),
                            "riskFlags": [],
                            "reviewStatus": "normal",
                        },
                    ],
                },
                summary={"record_count": 3, "record_mode": "image-only", "placements": []},
                template=LayoutTemplate(name="academy-default", board_page_count=50),
            )

            handoff = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = md_path.read_text(encoding="utf-8")
            preflight = handoff["classinPreflight"]
            issue_types = {issue["type"] for issue in preflight["issues"]}
            self.assertEqual("needs_attention", preflight["status"])
            self.assertFalse(preflight["passed"])
            self.assertIn("small_problem_image", issue_types)
            self.assertIn("missing_problem_image", issue_types)
            self.assertIn("low_ink_problem_image", issue_types)
            self.assertIn("review_flags_remaining", issue_types)
            self.assertEqual(3, preflight["checkedProblemCount"])
            self.assertIn("ClassIn Preflight", markdown)
            self.assertIn("small_problem_image", markdown)
            self.assertIn("missing_problem_image", markdown)
            self.assertIn("low_ink_problem_image", markdown)
            self.assertIn("문항 이미지 작음", markdown)
            self.assertIn("문항 이미지 없음", markdown)
            self.assertIn("이미지 내용 부족", markdown)

    def test_classin_preflight_ignores_nonactionable_continuation_review_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            edb_path = root / "lesson.edb"
            crop = root / "continuation.png"
            source.write_bytes(b"hwp")
            edb_path.write_bytes(b"edb")
            image = Image.new("RGB", (800, 300), "white")
            ImageDraw.Draw(image).rectangle((40, 40, 220, 90), fill=(20, 20, 20))
            image.save(crop)

            json_path, _md_path = problem_board.write_classin_handoff_manifest(
                root,
                source_paths=[source],
                edb_path=edb_path,
                ui_session={
                    "core_problem_count": 45,
                    "supplemental_item_count": 1,
                    "detected_problem_count": 46,
                    "source_page_count": 16,
                    "reviewSummary": {"riskFlagCounts": {"marker_document_continuation": 1}},
                    "problems": [
                        {
                            "id": "page-008-continuation",
                            "title": "이어지는 자료",
                            "imagePath": crop.resolve().as_uri(),
                            "riskFlags": ["fallback_grouping", "marker_document_continuation"],
                            "reviewStatus": "check_needed",
                        }
                    ],
                },
                summary={"record_count": 46, "record_mode": "image-only", "placements": []},
                template=LayoutTemplate(name="academy-default", board_page_count=92),
            )

            handoff = json.loads(json_path.read_text(encoding="utf-8"))

            self.assertEqual("passed", handoff["classinPreflight"]["status"])
            self.assertTrue(handoff["classinPreflight"]["passed"])
            self.assertEqual([], handoff["classinPreflight"]["issues"])

    def test_classin_preflight_keeps_fallback_review_flags_without_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            edb_path = root / "lesson.edb"
            crop = root / "fallback.png"
            source.write_bytes(b"hwp")
            edb_path.write_bytes(b"edb")
            image = Image.new("RGB", (800, 300), "white")
            ImageDraw.Draw(image).rectangle((40, 40, 220, 90), fill=(20, 20, 20))
            image.save(crop)

            json_path, _md_path = problem_board.write_classin_handoff_manifest(
                root,
                source_paths=[source],
                edb_path=edb_path,
                ui_session={
                    "core_problem_count": 1,
                    "detected_problem_count": 1,
                    "source_page_count": 1,
                    "problems": [
                        {
                            "id": "page-001-fallback",
                            "title": "1.",
                            "imagePath": crop.resolve().as_uri(),
                            "riskFlags": ["fallback_grouping"],
                            "reviewStatus": "check_needed",
                        }
                    ],
                },
                summary={"record_count": 1, "record_mode": "image-only", "placements": []},
                template=LayoutTemplate(name="academy-default", board_page_count=50),
            )

            handoff = json.loads(json_path.read_text(encoding="utf-8"))

            self.assertEqual("needs_attention", handoff["classinPreflight"]["status"])
            self.assertFalse(handoff["classinPreflight"]["passed"])
            self.assertEqual(["review_flags_remaining"], [issue["type"] for issue in handoff["classinPreflight"]["issues"]])

    def test_classin_preflight_flags_board_placement_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            edb_path = root / "lesson.edb"
            first_crop = root / "first.png"
            second_crop = root / "second.png"
            source.write_bytes(b"hwp")
            edb_path.write_bytes(b"edb")
            for path, label in ((first_crop, "13. long passage"), (second_crop, "14. child question")):
                image = Image.new("RGB", (640, 280), "white")
                draw = ImageDraw.Draw(image)
                for line in range(8):
                    draw.text((40, 32 + line * 26), f"{label} text line {line}", fill=(20, 20, 20))
                image.save(path)

            json_path, md_path = problem_board.write_classin_handoff_manifest(
                root,
                source_paths=[source],
                edb_path=edb_path,
                ui_session={
                    "core_problem_count": 2,
                    "supplemental_item_count": 0,
                    "detected_problem_count": 2,
                    "source_page_count": 1,
                    "reviewSummary": {},
                    "problems": [
                        {
                            "id": "p13",
                            "title": "13.",
                            "imagePath": first_crop.resolve().as_uri(),
                            "riskFlags": [],
                            "reviewStatus": "normal",
                            "startYPages": 0.0,
                            "actualHeightPages": 1.1,
                            "placementScaleRatio": 1.4,
                            "snappedNextStartYPages": 1.2,
                        },
                        {
                            "id": "p14",
                            "title": "14.",
                            "imagePath": second_crop.resolve().as_uri(),
                            "riskFlags": [],
                            "reviewStatus": "normal",
                            "startYPages": 1.2,
                            "actualHeightPages": 0.8,
                            "placementScaleRatio": 1.0,
                            "snappedNextStartYPages": 2.4,
                        },
                    ],
                },
                summary={"record_count": 2, "record_mode": "image-only", "placements": []},
                template=LayoutTemplate(name="academy-default", board_page_count=50),
            )

            handoff = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = md_path.read_text(encoding="utf-8")
            preflight = handoff["classinPreflight"]
            overlap_issues = [
                issue for issue in preflight["issues"] if issue["type"] == "board_placement_overlap"
            ]
            self.assertEqual("needs_attention_before_classin", handoff["status"])
            self.assertFalse(handoff["readyForClassIn"])
            self.assertEqual(1, len(overlap_issues))
            self.assertEqual("p13", overlap_issues[0]["problemId"])
            self.assertEqual("p14", overlap_issues[0]["nextProblemId"])
            self.assertGreater(overlap_issues[0]["renderedBottomYPages"], overlap_issues[0]["nextStartYPages"])
            self.assertIn("Handoff status: `needs_attention_before_classin`", markdown)
            self.assertIn("board_placement_overlap", markdown)

    def test_classin_preflight_flags_source_problem_bbox_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            edb_path = root / "lesson.edb"
            first_crop = root / "first.png"
            second_crop = root / "second.png"
            source.write_bytes(b"hwp")
            edb_path.write_bytes(b"edb")
            for path, label in ((first_crop, "21. first crop"), (second_crop, "22. overlapping crop")):
                image = Image.new("RGB", (640, 260), "white")
                draw = ImageDraw.Draw(image)
                for line in range(10):
                    draw.text((36, 28 + line * 22), f"{label} text line {line}", fill=(20, 20, 20))
                image.save(path)

            json_path, md_path = problem_board.write_classin_handoff_manifest(
                root,
                source_paths=[source],
                edb_path=edb_path,
                ui_session={
                    "core_problem_count": 2,
                    "supplemental_item_count": 0,
                    "detected_problem_count": 2,
                    "source_page_count": 1,
                    "reviewSummary": {},
                    "problems": [
                        {
                            "id": "p21",
                            "title": "21.",
                            "imagePath": first_crop.resolve().as_uri(),
                            "sourcePageId": "page-001",
                            "bbox": {"left": 40, "top": 100, "width": 520, "height": 320},
                            "riskFlags": [],
                            "reviewStatus": "normal",
                        },
                        {
                            "id": "p22",
                            "title": "22.",
                            "imagePath": second_crop.resolve().as_uri(),
                            "sourcePageId": "page-001",
                            "bbox": {"left": 60, "top": 125, "width": 500, "height": 300},
                            "riskFlags": [],
                            "reviewStatus": "normal",
                        },
                    ],
                },
                summary={"record_count": 2, "record_mode": "image-only", "placements": []},
                template=LayoutTemplate(name="academy-default", board_page_count=50),
            )

            handoff = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = md_path.read_text(encoding="utf-8")
            preflight = handoff["classinPreflight"]
            overlap_issues = [
                issue for issue in preflight["issues"] if issue["type"] == "source_problem_bbox_overlap"
            ]
            self.assertEqual("needs_attention_before_classin", handoff["status"])
            self.assertFalse(handoff["readyForClassIn"])
            self.assertEqual(1, len(overlap_issues))
            self.assertEqual("p21", overlap_issues[0]["problemId"])
            self.assertEqual("p22", overlap_issues[0]["nextProblemId"])
            self.assertEqual(["p21", "p22"], overlap_issues[0]["problemIds"])
            self.assertEqual(["p21", "p22"], overlap_issues[0]["problem_ids"])
            self.assertEqual("page-001", overlap_issues[0]["sourcePageId"])
            self.assertGreaterEqual(overlap_issues[0]["overlapAreaRatio"], 0.8)
            self.assertIn("source_problem_bbox_overlap", markdown)

    def test_classin_preflight_ignores_supplemental_passage_enclosing_bbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crop = root / "crop.png"
            Image.new("RGB", (640, 320), "white").save(crop)
            problems = [
                {
                    "id": "q10",
                    "title": "10.",
                    "imagePath": crop.resolve().as_uri(),
                    "sourcePageId": "page-004",
                    "bbox": {"left": 0, "top": 100, "width": 500, "height": 300},
                    "riskFlags": [],
                },
                {
                    "id": "passage-11-15",
                    "title": "지문 11~15",
                    "imagePath": crop.resolve().as_uri(),
                    "sourcePageId": "page-004",
                    "bbox": {"left": 0, "top": 0, "width": 1000, "height": 1200},
                    "passageRole": "passage_fragment",
                    "supplementalItem": True,
                    "riskFlags": [],
                },
            ]

            preflight, duplicate_groups = _session_publish_blocking_preflight(
                problems,
                session={"problems": problems, "pages": []},
            )

            self.assertTrue(preflight["passed"])
            self.assertEqual([], duplicate_groups)
            self.assertNotIn(
                "source_problem_bbox_overlap",
                [issue["type"] for issue in preflight["issues"]],
            )

    def test_classin_preflight_flags_passage_group_source_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            edb_path = root / "lesson.edb"
            first_crop = root / "p22.png"
            second_crop = root / "p23.png"
            source.write_bytes(b"hwp")
            edb_path.write_bytes(b"edb")
            for path, label in ((first_crop, "22. child question"), (second_crop, "23. child question")):
                image = Image.new("RGB", (640, 320), "white")
                draw = ImageDraw.Draw(image)
                for line in range(10):
                    draw.text((36, 28 + line * 24), f"{label} shared passage line {line}", fill=(20, 20, 20))
                image.save(path)

            json_path, md_path = problem_board.write_classin_handoff_manifest(
                root,
                source_paths=[source],
                edb_path=edb_path,
                ui_session={
                    "core_problem_count": 2,
                    "supplemental_item_count": 0,
                    "detected_problem_count": 2,
                    "source_page_count": 1,
                    "reviewSummary": {},
                    "problems": [
                        {
                            "id": "p22",
                            "title": "22.",
                            "imagePath": first_crop.resolve().as_uri(),
                            "sourcePageId": "page-004",
                            "bbox": {"left": 42, "top": 120, "width": 520, "height": 430},
                            "passageGroupId": "hwp-continuation-passage-22-26",
                            "passageRange": {"start": 22, "end": 26},
                            "passageRole": "child_question",
                            "passageChildProblemNumbers": [22, 23, 24, 25, 26],
                            "riskFlags": [],
                            "reviewStatus": "normal",
                        },
                        {
                            "id": "p23",
                            "title": "23.",
                            "imagePath": second_crop.resolve().as_uri(),
                            "sourcePageId": "page-004",
                            "bbox": {"left": 48, "top": 132, "width": 510, "height": 410},
                            "passageGroupId": "hwp-continuation-passage-22-26",
                            "passageRange": {"start": 22, "end": 26},
                            "passageRole": "child_question",
                            "passageChildProblemNumbers": [22, 23, 24, 25, 26],
                            "riskFlags": [],
                            "reviewStatus": "normal",
                        },
                    ],
                },
                summary={"record_count": 2, "record_mode": "image-only", "placements": []},
                template=LayoutTemplate(name="academy-default", board_page_count=50),
            )

            handoff = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = md_path.read_text(encoding="utf-8")
            preflight = handoff["classinPreflight"]
            reuse_issues = [
                issue for issue in preflight["issues"] if issue["type"] == "passage_group_source_reuse"
            ]
            source_overlap_issues = [
                issue for issue in preflight["issues"] if issue["type"] == "source_problem_bbox_overlap"
            ]
            self.assertEqual("needs_attention_before_classin", handoff["status"])
            self.assertFalse(handoff["readyForClassIn"])
            self.assertEqual(1, len(reuse_issues))
            self.assertEqual([], source_overlap_issues)
            self.assertEqual("p22", reuse_issues[0]["problemId"])
            self.assertEqual("p23", reuse_issues[0]["nextProblemId"])
            self.assertEqual(["p22", "p23"], reuse_issues[0]["problemIds"])
            self.assertEqual(["p22", "p23"], reuse_issues[0]["problem_ids"])
            self.assertEqual("hwp-continuation-passage-22-26", reuse_issues[0]["passageGroupId"])
            self.assertEqual({"start": 22, "end": 26}, reuse_issues[0]["passageRange"])
            self.assertEqual({"start": 22, "end": 26}, reuse_issues[0]["passage_range"])
            self.assertEqual([22, 23, 24, 25, 26], reuse_issues[0]["passageChildProblemNumbers"])
            self.assertEqual([22, 23, 24, 25, 26], reuse_issues[0]["passage_child_problem_numbers"])
            self.assertGreaterEqual(reuse_issues[0]["overlapAreaRatio"], 0.8)
            self.assertEqual(
                {"start": 22, "end": 26},
                handoff["passageGroupSourceReuseGroups"][0]["passageRange"],
            )
            self.assertEqual(
                [22, 23, 24, 25, 26],
                handoff["passageGroupSourceReuseGroups"][0]["passageChildProblemNumbers"],
            )
            self.assertIn("passage_group_source_reuse", markdown)

    def test_classin_preflight_ignores_hwp_text_fallback_passage_bbox_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            edb_path = root / "lesson.edb"
            fallback_crop = root / "p32.png"
            child_crop = root / "p33.png"
            source.write_bytes(b"hwp")
            edb_path.write_bytes(b"edb")
            for path, label in ((fallback_crop, "32. text fallback"), (child_crop, "33. child question")):
                image = Image.new("RGB", (640, 320), "white")
                draw = ImageDraw.Draw(image)
                for line in range(10):
                    draw.text((36, 28 + line * 24), f"{label} line {line}", fill=(20, 20, 20))
                image.save(path)

            json_path, _md_path = problem_board.write_classin_handoff_manifest(
                root,
                source_paths=[source],
                edb_path=edb_path,
                ui_session={
                    "core_problem_count": 2,
                    "supplemental_item_count": 0,
                    "detected_problem_count": 2,
                    "source_page_count": 1,
                    "reviewSummary": {},
                    "problems": [
                        {
                            "id": "p32",
                            "title": "32.",
                            "imagePath": fallback_crop.resolve().as_uri(),
                            "sourcePageId": "page-012",
                            "bbox": {"left": 0, "top": 0, "width": 2493, "height": 3412},
                            "passageGroupId": "hwp-text-passage-32-34",
                            "passageRole": "child_question",
                            "riskFlags": ["hwp_text_fallback_problem"],
                            "reviewStatus": "check_needed",
                        },
                        {
                            "id": "p33",
                            "title": "33.",
                            "imagePath": child_crop.resolve().as_uri(),
                            "sourcePageId": "page-012",
                            "bbox": {"left": 1228, "top": 0, "width": 1264, "height": 1004},
                            "passageGroupId": "hwp-text-passage-32-34",
                            "passageRole": "child_question",
                            "riskFlags": [],
                            "reviewStatus": "normal",
                        },
                    ],
                },
                summary={"record_count": 2, "record_mode": "image-only", "placements": []},
                template=LayoutTemplate(name="academy-default", board_page_count=50),
            )

            handoff = json.loads(json_path.read_text(encoding="utf-8"))
            reuse_issues = [
                issue
                for issue in handoff["classinPreflight"]["issues"]
                if issue["type"] == "passage_group_source_reuse"
            ]
            self.assertEqual([], reuse_issues)

    def test_korean_edb_filename_download_header_is_http_safe(self):
        header = content_disposition_attachment(
            "20260610_223707_1781098627053740000_고1_샘플_7f796ebe63.edb"
        )
        header.encode("latin-1")
        self.assertIn('filename="20260610_223707_1781098627053740000__1____7f796ebe63.edb"', header)
        self.assertIn("filename*=UTF-8''", header)
        self.assertIn("%EA%B3%A01_%EC%83%98%ED%94%8C", header)

    def test_publish_summary_exposes_validated_edb_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edb_path = root / "lesson.edb"
            edb_path.write_bytes(b"placeholder")

            summary = _session_publish_summary(
                edb_path=edb_path,
                output_dir=root,
                edb_validation={
                    "outerSize": 1234,
                    "innerSize": 987,
                    "pageCountHint": 23,
                    "recordCountHint": 45,
                    "recordCountActual": 45,
                },
                record_count=45,
                published_at="2026-06-13T12:00:00+09:00",
            )

            self.assertTrue(summary["validated"])
            self.assertEqual(summary["statusLabel"], "검증 완료")
            self.assertEqual(summary["edbFileName"], "lesson.edb")
            self.assertEqual(summary["edbPath"], str(edb_path.resolve()))
            self.assertEqual(summary["outputDir"], str(root.resolve()))
            self.assertEqual(summary["recordCount"], 45)
            self.assertEqual(summary["recordCountActual"], 45)
            self.assertEqual(summary["pageCountHint"], 23)
            self.assertIn("/api/file?path=", summary["edbFileUri"])
            self.assertEqual(summary["publishedAt"], "2026-06-13T12:00:00+09:00")

    def test_publish_summary_exposes_classin_handoff_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edb_path = root / "lesson.edb"
            handoff_path = root / "classin_handoff.json"
            handoff_md_path = root / "classin_handoff.md"
            edb_path.write_bytes(b"placeholder")
            handoff_path.write_text("{}", encoding="utf-8")
            handoff_md_path.write_text("# check", encoding="utf-8")

            summary = _session_publish_summary(
                edb_path=edb_path,
                output_dir=root,
                edb_validation={
                    "outerSize": 1234,
                    "innerSize": 987,
                    "pageCountHint": 23,
                    "recordCountHint": 45,
                    "recordCountActual": 45,
                },
                record_count=45,
                classin_handoff_path=handoff_path,
                classin_handoff_markdown_path=handoff_md_path,
                published_at="2026-06-13T12:00:00+09:00",
            )

            self.assertEqual(str(handoff_path.resolve()), summary["classinHandoffPath"])
            self.assertEqual(str(handoff_md_path.resolve()), summary["classinHandoffMarkdownPath"])
            self.assertIn("/api/file?path=", summary["classinHandoffUri"])
            self.assertIn("/api/file?path=", summary["classinHandoffMarkdownUri"])

    def test_publish_summary_exposes_classin_handoff_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edb_path = root / "lesson.edb"
            handoff_path = root / "classin_handoff.json"
            edb_path.write_bytes(b"placeholder")
            handoff_path.write_text(
                json.dumps(
                    {
                        "status": "needs_attention_before_classin",
                        "readyForClassIn": False,
                    }
                ),
                encoding="utf-8",
            )

            summary = _session_publish_summary(
                edb_path=edb_path,
                output_dir=root,
                edb_validation={
                    "outerSize": 1234,
                    "innerSize": 987,
                    "pageCountHint": 23,
                    "recordCountHint": 45,
                    "recordCountActual": 45,
                },
                record_count=45,
                classin_handoff_path=handoff_path,
                published_at="2026-06-13T12:00:00+09:00",
            )

            self.assertEqual("needs_attention_before_classin", summary["classinHandoffStatus"])
            self.assertFalse(summary["readyForClassIn"])
            self.assertEqual("needs_attention_before_classin", summary["classin_handoff_status"])
            self.assertFalse(summary["ready_for_classin"])

    def test_publish_summary_exposes_classin_preflight_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edb_path = root / "lesson.edb"
            edb_path.write_bytes(b"placeholder")
            preflight = {
                "status": "needs_attention",
                "passed": False,
                "issueCount": 2,
                "checkedProblemCount": 3,
                "issues": [{"type": "small_problem_image"}],
            }

            summary = _session_publish_summary(
                edb_path=edb_path,
                output_dir=root,
                edb_validation={
                    "outerSize": 1234,
                    "innerSize": 987,
                    "pageCountHint": 50,
                    "recordCountHint": 3,
                    "recordCountActual": 3,
                },
                record_count=3,
                classin_preflight=preflight,
                published_at="2026-06-13T12:00:00+09:00",
            )

            self.assertEqual(preflight, summary["classinPreflight"])
            self.assertEqual("needs_attention", summary["classinPreflightStatus"])
            self.assertEqual(2, summary["classinPreflightIssueCount"])
            self.assertFalse(summary["classinPreflightPassed"])

    def test_publish_summary_exposes_layout_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edb_path = root / "lesson.edb"
            edb_path.write_bytes(b"placeholder")
            diagnostics = {
                "autoExtendedCount": 1,
                "overlapRiskCount": 0,
                "maxRenderedHeightPages": 1.54,
                "label": "긴 이미지 자동 확장 1 · 최대 1.54p",
            }

            summary = _session_publish_summary(
                edb_path=edb_path,
                output_dir=root,
                edb_validation={
                    "outerSize": 1234,
                    "innerSize": 987,
                    "pageCountHint": 50,
                    "recordCountHint": 1,
                    "recordCountActual": 1,
                },
                record_count=1,
                layout_diagnostics=diagnostics,
                published_at="2026-06-13T12:00:00+09:00",
            )

            self.assertEqual(diagnostics, summary["layoutDiagnostics"])
            self.assertEqual("긴 이미지 자동 확장 1 · 최대 1.54p", summary["layoutDiagnosticsLabel"])
            self.assertEqual(diagnostics, summary["layout_diagnostics"])
            self.assertEqual("긴 이미지 자동 확장 1 · 최대 1.54p", summary["layout_diagnostics_label"])

    def test_publish_summary_exposes_passage_group_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edb_path = root / "lesson.edb"
            edb_path.write_bytes(b"placeholder")
            passage_groups = [
                {
                    "id": "page-1-passage-13-16",
                    "problemCount": 4,
                    "problemNumbers": [13, 14, 15, 16],
                    "continuesAcrossPages": True,
                    "sourcePageIds": ["page-1", "page-2"],
                }
            ]

            summary = _session_publish_summary(
                edb_path=edb_path,
                output_dir=root,
                edb_validation={
                    "outerSize": 1234,
                    "innerSize": 987,
                    "pageCountHint": 50,
                    "recordCountHint": 4,
                    "recordCountActual": 4,
                },
                record_count=4,
                passage_groups=passage_groups,
                published_at="2026-06-13T12:00:00+09:00",
            )

            self.assertEqual(passage_groups, summary["passageGroups"])
            self.assertEqual(passage_groups, summary["passage_groups"])
            self.assertEqual(1, summary["passageGroupCount"])
            self.assertEqual(1, summary["passage_group_count"])
            self.assertEqual(4, summary["passageProblemCount"])
            self.assertEqual(4, summary["passage_problem_count"])
            self.assertEqual(1, summary["crossPagePassageGroupCount"])
            self.assertEqual(1, summary["cross_page_passage_group_count"])

    def test_publish_summary_counts_passage_children_without_fragments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edb_path = root / "lesson.edb"
            edb_path.write_bytes(b"placeholder")
            passage_groups = [
                {
                    "id": "hwp-continuation-passage-22-26",
                    "problemCount": 6,
                    "detectedProblemCount": 6,
                    "fragmentProblemCount": 1,
                    "problemNumbers": [22, 23, 24, 25, 26],
                    "fragmentProblemIds": ["page-008-continuation"],
                    "continuesAcrossPages": True,
                }
            ]

            summary = _session_publish_summary(
                edb_path=edb_path,
                output_dir=root,
                edb_validation={
                    "outerSize": 1234,
                    "innerSize": 987,
                    "pageCountHint": 50,
                    "recordCountHint": 5,
                    "recordCountActual": 5,
                },
                record_count=5,
                passage_groups=passage_groups,
                published_at="2026-06-13T12:00:00+09:00",
            )

            self.assertEqual(5, summary["passageProblemCount"])
            self.assertEqual(5, summary["passage_problem_count"])

    def test_publish_summary_exposes_passage_group_source_reuse_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edb_path = root / "lesson.edb"
            edb_path.write_bytes(b"placeholder")
            source_reuse_groups = [
                {
                    "passageGroupId": "hwp-text-passage-31-34",
                    "sourcePageId": "page-004",
                    "problemIds": ["p31", "p32"],
                    "overlapAreaRatio": 0.92,
                }
            ]

            summary = _session_publish_summary(
                edb_path=edb_path,
                output_dir=root,
                edb_validation={
                    "outerSize": 1234,
                    "innerSize": 987,
                    "pageCountHint": 50,
                    "recordCountHint": 2,
                    "recordCountActual": 2,
                },
                record_count=2,
                passage_group_source_reuse_groups=source_reuse_groups,
                published_at="2026-06-13T12:00:00+09:00",
            )

            self.assertEqual(source_reuse_groups, summary["passageGroupSourceReuseGroups"])
            self.assertEqual(source_reuse_groups, summary["passage_group_source_reuse_groups"])
            self.assertEqual(1, summary["passageGroupSourceReuseGroupCount"])
            self.assertEqual(1, summary["passage_group_source_reuse_group_count"])

    def test_publish_summary_preserves_core_and_supplemental_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edb_path = root / "lesson.edb"
            edb_path.write_bytes(b"placeholder")

            summary = _session_publish_summary(
                edb_path=edb_path,
                output_dir=root,
                edb_validation={
                    "outerSize": 1234,
                    "innerSize": 987,
                    "pageCountHint": 92,
                    "recordCountHint": 46,
                    "recordCountActual": 46,
                },
                record_count=46,
                core_problem_count=45,
                supplemental_item_count=1,
                published_at="2026-06-13T12:00:00+09:00",
            )

            self.assertEqual(46, summary["recordCount"])
            self.assertEqual(45, summary["coreProblemCount"])
            self.assertEqual(1, summary["supplementalItemCount"])
            self.assertEqual("45문항 + 자료 1", summary["recordCountLabel"])
            self.assertEqual(45, summary["core_problem_count"])
            self.assertEqual(1, summary["supplemental_item_count"])
            self.assertEqual("45문항 + 자료 1", summary["record_count_label"])

    def test_publish_history_keeps_latest_first_and_limits_entries(self):
        current = {
            "edbFileName": "latest.edb",
            "edbPath": "/tmp/latest.edb",
            "publishedAt": "2026-06-13T12:05:00+09:00",
        }
        prior = [
            {"edbFileName": f"old-{index}.edb", "edbPath": f"/tmp/old-{index}.edb"}
            for index in range(1, 7)
        ]
        session = {"publish_history": prior}

        history = _session_publish_history(session, current, limit=5)

        self.assertEqual(
            ["latest.edb", "old-1.edb", "old-2.edb", "old-3.edb", "old-4.edb"],
            [item["edbFileName"] for item in history],
        )
        self.assertEqual("/tmp/latest.edb", history[0]["edbPath"])
        self.assertEqual(5, len(history))

    def test_publish_history_preserves_existing_summary_when_history_missing(self):
        current = {"edbFileName": "latest.edb", "edbPath": "/tmp/latest.edb"}
        previous = {"edbFileName": "previous.edb", "edbPath": "/tmp/previous.edb"}
        session = {"publishSummary": previous}

        history = _session_publish_history(session, current, limit=5)

        self.assertEqual(["latest.edb", "previous.edb"], [item["edbFileName"] for item in history])

    def test_v1_multi_problem_export_uses_one_problem_per_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [
                self._make_problem_entry(root, "problem-1", Box(0, 40, 380, 300)),
                self._make_problem_entry(root, "problem-2", Box(410, 40, 380, 300)),
            ]
            template = LayoutTemplate(
                name="academy-default",
                board_page_count=2,
                base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
            )

            _records, placements = build_image_only_records(
                entries,
                template,
                crop_format=CROP_FORMAT_V1,
            )

            self.assertEqual([0.0, 1.2], [item["start_y_pages"] for item in placements])
            self.assertEqual([1.2, 2.4], [item["snapped_next_start_y_pages"] for item in placements])
            self.assertEqual([1.0, 1.0], [item["placement_scale_ratio"] for item in placements])
            self.assertEqual(
                [V1_DEFAULT_DISPLAY_WIDTH_PX, V1_DEFAULT_DISPLAY_WIDTH_PX],
                [item["rendered_width_px"] for item in placements],
            )
            self.assertAlmostEqual(
                placements[0]["rendered_height_px"],
                V1_DEFAULT_DISPLAY_WIDTH_PX * (300 / 380),
                places=6,
            )

    def test_v1_image_only_layout_reserves_actual_rendered_height(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [
                self._make_problem_entry(root, "long-problem", Box(0, 40, 380, 1600)),
                self._make_problem_entry(root, "next-problem", Box(0, 40, 380, 300)),
            ]
            template = LayoutTemplate(
                name="academy-default",
                base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
            )

            _records, placements = build_image_only_records(
                entries,
                template,
                crop_format=CROP_FORMAT_V1,
            )

            expected_height_pages = V1_DEFAULT_DISPLAY_WIDTH_PX * (1600 / 380) / 590
            self.assertAlmostEqual(
                placements[0]["actual_content_height_pages"],
                expected_height_pages,
                places=6,
            )
            self.assertAlmostEqual(
                placements[0]["rendered_height_px"],
                V1_DEFAULT_DISPLAY_WIDTH_PX * (1600 / 380),
                places=6,
            )
            self.assertGreater(placements[0]["snapped_next_start_y_pages"], 1.2)
            self.assertEqual(
                placements[0]["snapped_next_start_y_pages"],
                placements[1]["start_y_pages"],
            )
            self.assertGreaterEqual(
                template.board_page_count,
                placements[-1]["snapped_next_start_y_pages"],
            )

    def test_classin_v1_part_rerender_reserves_final_record_height(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = LayoutTemplate(
                name="academy-default",
                board_page_count=58,
                base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
            )
            entries = [
                self._make_problem_entry(root, f"problem-{index}", Box(0, 0, 380, 850))
                for index in range(1, 3)
            ]
            for entry in entries:
                entry.actual_height_pages = problem_board.estimate_height_pages(
                    (380, 850),
                    template,
                )

            parts = write_classin_limited_edb_files(
                entries,
                template,
                root,
                "long-v1.edb",
                record_mode="image-only",
                text_confidence_threshold=0.78,
                dark_board=False,
                board_theme=problem_board.DEFAULT_BOARD_THEME,
                crop_format=CROP_FORMAT_V1,
            )

            self.assertEqual(1, len(parts))
            placements = parts[0]["placements"]
            self.assertEqual([0.0, 2.4], [item["start_y_pages"] for item in placements])
            self.assertEqual([2.4, 4.8], [item["snapped_next_start_y_pages"] for item in placements])
            self.assertLessEqual(
                placements[0]["record_bottom_y_pages"],
                placements[1]["record_top_y_pages"],
            )
            self.assertLessEqual(parts[0]["flowEndPages"], problem_board.CLASSIN_MAX_BOARD_PAGE_COUNT)

            parsed = parse_edb(Path(parts[0]["edbPath"]))
            image_records = [record for record in parsed.records if record.embedded_images]
            self.assertEqual(2, len(image_records))
            first_bottom_pages = (
                float(image_records[0].pos_y) + float(image_records[0].height_hint)
            ) * parsed.page_count_hint
            second_top_pages = float(image_records[1].pos_y) * parsed.page_count_hint
            self.assertLessEqual(first_bottom_pages, second_top_pages)

    def test_classin_v2_part_rerender_remains_non_overlapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = LayoutTemplate(
                name="academy-default",
                board_page_count=58,
                base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
            )
            entries = [
                self._make_problem_entry(root, f"problem-{index}", Box(0, 0, 380, 850))
                for index in range(1, 3)
            ]
            for entry in entries:
                entry.actual_height_pages = problem_board.estimate_height_pages(
                    (380, 850),
                    template,
                )

            parts = write_classin_limited_edb_files(
                entries,
                template,
                root,
                "long-v2.edb",
                record_mode="image-only",
                text_confidence_threshold=0.78,
                dark_board=False,
                board_theme=problem_board.DEFAULT_BOARD_THEME,
                crop_format=edb_builder.CROP_FORMAT_V2,
            )

            placements = parts[0]["placements"]
            self.assertEqual([0.0, 1.2], [item["start_y_pages"] for item in placements])
            self.assertEqual([1.2, 2.4], [item["snapped_next_start_y_pages"] for item in placements])
            self.assertLessEqual(
                placements[0]["record_bottom_y_pages"],
                placements[1]["record_top_y_pages"],
            )
            self.assertLessEqual(parts[0]["flowEndPages"], problem_board.CLASSIN_MAX_BOARD_PAGE_COUNT)

            parsed = parse_edb(Path(parts[0]["edbPath"]))
            image_records = [record for record in parsed.records if record.embedded_images]
            self.assertEqual(2, len(image_records))
            first_bottom_pages = (
                float(image_records[0].pos_y) + float(image_records[0].height_hint)
            ) * parsed.page_count_hint
            second_top_pages = float(image_records[1].pos_y) * parsed.page_count_hint
            self.assertLessEqual(first_bottom_pages, second_top_pages)

    def test_build_image_only_records_parallelizes_encoding_without_reordering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [
                self._make_problem_entry(root, f"problem-{index}", Box(0, 40, 380, 300))
                for index in range(1, 5)
            ]
            template = LayoutTemplate(
                name="academy-default",
                base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
            )
            lock = threading.Lock()
            active_count = 0
            max_active_count = 0
            completion_order: list[str] = []
            delays = {
                "problem-1": 0.04,
                "problem-2": 0.03,
                "problem-3": 0.02,
                "problem-4": 0.01,
            }

            def fake_build_image(placement, entry, **_kwargs):
                nonlocal active_count, max_active_count
                with lock:
                    active_count += 1
                    max_active_count = max(max_active_count, active_count)
                time.sleep(delays[entry.problem_id])
                with lock:
                    active_count -= 1
                    completion_order.append(entry.problem_id)
                return problem_board._ImageOnlyRecordImage(
                    crop_path=entry.crop_path,
                    board_render_path=entry.board_render_path,
                    image_bytes=f"primary-{entry.problem_id}".encode("ascii"),
                    secondary_bytes=f"secondary-{entry.problem_id}".encode("ascii"),
                    width_px=380,
                    height_px=300,
                    scale_ratio=1.0,
                )

            with (
                mock.patch.object(problem_board, "_resolve_image_record_worker_count", return_value=4),
                mock.patch.object(problem_board, "_build_image_only_record_image", side_effect=fake_build_image),
            ):
                records, placements = build_image_only_records(
                    entries,
                    template,
                    crop_format=CROP_FORMAT_V1,
                )

            self.assertGreater(max_active_count, 1)
            self.assertCountEqual([entry.problem_id for entry in entries], completion_order)
            self.assertEqual([entry.problem_id for entry in entries], [item["problem_id"] for item in placements])
            self.assertEqual(len(entries), len(records))

    def test_v2_image_only_record_keeps_logical_width_and_oversamples_bitmap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = self._make_problem_entry(root, "problem-1", Box(0, 40, 380, 300))
            template = LayoutTemplate(
                name="academy-default",
                base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
            )

            records, placements = build_image_only_records(
                [entry],
                template,
                crop_format=problem_board.CROP_FORMAT_V2,
            )

            self.assertEqual(1, len(records))
            self.assertEqual(problem_board.CROP_FORMAT_V2, placements[0]["crop_format"])
            self.assertGreaterEqual(
                placements[0]["image_pixel_width"],
                problem_board.V2_ENCODED_IMAGE_MIN_WIDTH_PX,
            )
            self.assertEqual(
                float(problem_board.V2_TARGET_IMAGE_WIDTH_PX),
                placements[0]["rendered_width_px"],
            )
            self.assertEqual(1.0, placements[0]["placement_scale_ratio"])

    def test_v2_fit_width_page_keeps_high_res_pixels_without_changing_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = self._make_problem_entry(root, "page-1", Box(0, 0, 1697, 2400))
            full_page = Image.new("RGB", (1697, 2400), "white")
            ImageDraw.Draw(full_page).text((120, 180), "국어 통이미지 글자 보존", fill="black")
            full_page.save(entry.crop_path)
            full_page.save(entry.board_render_path)
            entry.input_intent = "page-as-is"
            entry.placement_scale_ratio = 2.1148148148

            _records, placements = build_image_only_records(
                [entry],
                LayoutTemplate(name="academy-default"),
                crop_format=problem_board.CROP_FORMAT_V2,
                dark_board=False,
            )

            placement = placements[0]
            expected_display_width = problem_board.V2_TARGET_IMAGE_WIDTH_PX * entry.placement_scale_ratio
            self.assertAlmostEqual(expected_display_width, placement["rendered_width_px"], places=3)
            self.assertEqual(1697, placement["image_pixel_width"])
            self.assertEqual(2400, placement["image_pixel_height"])
            self.assertEqual("source-preserving", placement["image_resolution_policy"])
            self.assertLessEqual(
                placement["image_pixel_width"],
                problem_board.V2_ENCODED_IMAGE_MAX_WIDTH_PX,
            )
            self.assertGreater(placement["image_pixel_width"], placement["rendered_width_px"] * 2.6)
            self.assertAlmostEqual(
                2400 / 1697,
                placement["image_pixel_height"] / placement["image_pixel_width"],
                places=3,
            )

    def test_v2_legacy_regular_scale_preserves_ratio_and_reserves_grid_height(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = LayoutTemplate(
                name="academy-default",
                base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
            )
            unmarked = self._make_problem_entry(root, "unmarked", Box(0, 0, 380, 300))
            unmarked.placement_scale_ratio = 2.4
            following = self._make_problem_entry(root, "following", Box(0, 0, 380, 300))

            _records, unmarked_placements = build_image_only_records(
                [unmarked, following],
                template,
                crop_format=problem_board.CROP_FORMAT_V2,
            )

            self.assertEqual(1.6, unmarked_placements[0]["placement_scale_ratio"])
            self.assertEqual(1.2, unmarked_placements[1]["start_y_pages"])
            self.assertFalse(unmarked_placements[0]["preserve_legacy_placement_scale"])

            marked = self._make_problem_entry(root, "marked", Box(0, 0, 380, 300))
            marked.placement_scale_ratio = 2.4
            marked.preserve_legacy_placement_scale = True
            marked_following = self._make_problem_entry(root, "marked-following", Box(0, 0, 380, 300))

            inputs = problem_board.placement_inputs([marked, marked_following])
            self.assertEqual(2.4, inputs[0].metadata["placement_scale_ratio"])
            self.assertTrue(inputs[0].metadata["reserve_scaled_height"])

            _records, marked_placements = build_image_only_records(
                [marked, marked_following],
                template,
                crop_format=problem_board.CROP_FORMAT_V2,
            )

            self.assertEqual(2.4, marked_placements[0]["placement_scale_ratio"])
            self.assertEqual(2.4, marked_placements[0]["snapped_next_start_y_pages"])
            self.assertEqual(2.4, marked_placements[1]["start_y_pages"])
            self.assertTrue(marked_placements[0]["preserve_legacy_placement_scale"])
            self.assertLessEqual(
                marked_placements[0]["record_bottom_y_pages"],
                marked_placements[1]["record_top_y_pages"],
            )

    def test_problem_entry_conversion_accepts_legacy_scale_only_with_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crop_path = root / "problem.png"
            Image.new("RGB", (380, 300), "white").save(crop_path)
            base_problem = {
                "id": "problem-1",
                "title": "1.",
                "subject": "math",
                "imagePath": crop_path.resolve().as_uri(),
                "bbox": {"left": 0, "top": 0, "width": 380, "height": 300},
                "placementScaleRatio": 2.4,
            }

            unmarked = _problems_to_entries([base_problem])[0]
            marked = _problems_to_entries([
                {**base_problem, "preserveLegacyPlacementScale": True}
            ])[0]

            self.assertFalse(unmarked.preserve_legacy_placement_scale)
            self.assertTrue(marked.preserve_legacy_placement_scale)

    def test_v2_preserves_safe_300_dpi_page_and_bounds_oversized_scan(self):
        self.assertEqual(
            (2480, 3508),
            problem_board._v2_encoded_image_size((2480, 3508), (760.0, 1075.0)),
        )
        self.assertEqual(
            (3508, 2480),
            problem_board._v2_encoded_image_size((3508, 2480), (1075.0, 760.0)),
        )

        encoded_width, encoded_height = problem_board._v2_encoded_image_size(
            (5000, 7000),
            (900.0, 1260.0),
        )

        self.assertLessEqual(encoded_width, problem_board.V2_ENCODED_IMAGE_MAX_WIDTH_PX)
        self.assertLessEqual(
            max(encoded_width, encoded_height),
            problem_board.V2_ENCODED_IMAGE_MAX_EDGE_PX,
        )
        self.assertLessEqual(
            encoded_width * encoded_height,
            problem_board.V2_ENCODED_IMAGE_MAX_PIXELS,
        )

    def test_page_as_is_and_formula_subjects_use_high_fidelity_jpeg_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = self._make_problem_entry(root, "page-1", Box(0, 0, 1697, 2400))
            entry.input_intent = "page-as-is"
            math_entry = self._make_problem_entry(root, "math-1", Box(0, 0, 800, 600))
            math_entry.subject = Subject.MATH
            captured_qualities: list[int] = []

            def fake_encode(_image, quality=92):
                captured_qualities.append(quality)
                return b"jpeg", "JPEG"

            with (
                mock.patch.object(problem_board, "_encode_image_bytes", side_effect=fake_encode),
                mock.patch.object(problem_board, "build_v1_secondary_image_bytes", return_value=b"preview"),
            ):
                build_image_only_records(
                    [entry],
                    LayoutTemplate(name="academy-default"),
                    crop_format=problem_board.CROP_FORMAT_V1,
                    dark_board=False,
                )
                build_image_only_records(
                    [math_entry],
                    LayoutTemplate(name="academy-default"),
                    crop_format=problem_board.CROP_FORMAT_V1,
                    dark_board=False,
                )

            self.assertEqual(
                [
                    problem_board.TEXT_PRIORITY_IMAGE_RECORD_JPEG_QUALITY,
                    problem_board.TEXT_PRIORITY_IMAGE_RECORD_JPEG_QUALITY,
                ],
                captured_qualities,
            )

    def test_v1_page_as_is_export_uses_fit_width_continuous_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [
                self._make_problem_entry(root, "page-1", Box(0, 0, 400, 620)),
                self._make_problem_entry(root, "page-2", Box(0, 0, 400, 620)),
            ]
            for entry in entries:
                image = Image.open(entry.crop_path).convert("RGB")
                draw = ImageDraw.Draw(image)
                draw.rectangle((32, 36, 368, 584), outline="black", width=4)
                draw.text((58, 70), entry.title, fill="black")
                image.save(entry.crop_path)
                entry.processing_step = PROCESSING_STEP_RECONSTRUCT
                entry.placement_scale_ratio = problem_board.PLACEMENT_FIT_WIDTH_SCALE_MAX
                entry.input_intent = "page-as-is"
                entry.force_full_page_bounds = True
            template = LayoutTemplate(
                name="academy-default",
                base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
                metadata={"placement_mode": "continuous-page-as-is"},
            )
            for entry in entries:
                entry.actual_height_pages = problem_board.estimate_page_as_is_height_pages(
                    Image.open(entry.crop_path).size,
                    template,
                )

            _records, placements = build_image_only_records(
                entries,
                template,
                crop_format=CROP_FORMAT_V1,
            )

            applied_scale = placements[0]["placement_scale_ratio"]
            self.assertGreater(applied_scale, problem_board.PLACEMENT_SCALE_MAX)
            self.assertLessEqual(applied_scale, problem_board.PLACEMENT_FIT_WIDTH_SCALE_MAX)
            expected_span = round(
                placements[0]["actual_content_height_pages"] * applied_scale
                + problem_board.CONTINUOUS_RECORD_GAP_PX / problem_board.CANVAS_WIDTH,
                6,
            )
            self.assertEqual(0.0, placements[0]["start_y_pages"])
            self.assertAlmostEqual(expected_span, placements[0]["snapped_next_start_y_pages"], places=5)
            self.assertAlmostEqual(expected_span, placements[1]["start_y_pages"], places=5)
            self.assertEqual(
                [applied_scale, applied_scale],
                [item["placement_scale_ratio"] for item in placements],
            )

    def test_classin_part_keeps_image_hints_on_header_page_scale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = self._make_problem_entry(root, "page-1", Box(0, 0, 1697, 2400))
            entry.processing_step = PROCESSING_STEP_RECONSTRUCT
            entry.placement_scale_ratio = problem_board.PLACEMENT_FIT_WIDTH_SCALE_MAX
            entry.input_intent = "page-as-is"
            entry.force_full_page_bounds = True
            template = LayoutTemplate(
                name="academy-default",
                board_page_count=63,
                base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
                metadata={"placement_mode": "continuous-page-as-is"},
            )

            parts = problem_board.write_classin_limited_edb_files(
                [entry],
                template,
                root,
                "korean-page.edb",
                record_mode="image-only",
                text_confidence_threshold=0.78,
                dark_board=True,
                board_theme=problem_board.DEFAULT_BOARD_THEME,
                crop_format=CROP_FORMAT_V1,
            )

            parsed = parse_edb(Path(parts[0]["edbPath"]))
            record = parsed.records[0]
            image = record.embedded_images[0]
            rendered_width = float(record.width_hint) * parsed.canvas_height
            rendered_height = float(record.height_hint) * parsed.canvas_width * parsed.page_count_hint

            self.assertEqual(problem_board.CLASSIN_MAX_BOARD_PAGE_COUNT, parsed.page_count_hint)
            self.assertEqual(
                {problem_board.CLASSIN_MAX_BOARD_PAGE_COUNT},
                {item["record_page_count_hint"] for item in parts[0]["placements"]},
            )
            self.assertAlmostEqual(
                image.height / image.width,
                rendered_height / rendered_width,
                places=5,
            )

    def test_v1_single_problem_full_page_bounds_stays_slot_based(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [
                self._make_problem_entry(root, "single-1", Box(0, 0, 400, 620)),
                self._make_problem_entry(root, "single-2", Box(0, 0, 400, 620)),
            ]
            for entry in entries:
                entry.input_intent = "single-problem"
                entry.force_full_page_bounds = True
            template = LayoutTemplate(
                name="academy-default",
                base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
                metadata={"placement_mode": "continuous-page-as-is"},
            )

            _records, placements = build_image_only_records(
                entries,
                template,
                crop_format=CROP_FORMAT_V1,
            )

            self.assertEqual([0.0, 2.4], [item["start_y_pages"] for item in placements])
            self.assertEqual([2.4, 4.8], [item["snapped_next_start_y_pages"] for item in placements])
            self.assertEqual([1.0, 1.0], [item["placement_scale_ratio"] for item in placements])
            self.assertEqual(
                [problem_board.PROCESSING_STEP_RAW, problem_board.PROCESSING_STEP_RAW],
                [item["processing_step"] for item in placements],
            )

    def test_layout_input_intent_overrides_stale_continuous_mode(self):
        template = LayoutTemplate(
            name="academy-default",
            base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
            metadata={"placement_mode": "continuous-page-as-is"},
        )
        problems = [
            ProblemLayoutInput(
                problem_id="single-1",
                actual_content_height_pages=0.72,
                metadata={
                    "input_intent": "single-problem",
                    "placement_mode": "continuous-page-as-is",
                    "force_full_page_bounds": True,
                },
            ),
            ProblemLayoutInput(
                problem_id="single-2",
                actual_content_height_pages=0.72,
                metadata={
                    "input_intent": "single-problem",
                    "placement_mode": "continuous-page-as-is",
                    "force_full_page_bounds": True,
                },
            ),
        ]

        placements = place_problems(problems, template=template)

        self.assertEqual([0.0, 1.2], [placement.start_y_pages for placement in placements])
        self.assertEqual([1.2, 2.4], [placement.snapped_next_start_y_pages for placement in placements])

    def test_v1_reconstruct_step_exports_transparent_high_res_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = self._make_problem_entry(root, "problem-1", Box(0, 40, 380, 300))
            entry.processing_step = PROCESSING_STEP_RECONSTRUCT
            image = Image.open(entry.crop_path).convert("RGB")
            draw = ImageDraw.Draw(image)
            draw.text((24, 40), "1. Transparent export", fill="black")
            image.save(entry.crop_path)
            template = LayoutTemplate(
                name="academy-default",
                base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
            )

            records, placements = build_image_only_records(
                [entry],
                template,
                crop_format=CROP_FORMAT_V1,
            )

            self.assertIn(b"\x89PNG\r\n\x1a\n", records[0])
            self.assertEqual(placements[0]["processing_step"], PROCESSING_STEP_RECONSTRUCT)
            self.assertEqual(placements[0]["image_pixel_width"], 1330)
            self.assertGreater(placements[0]["image_pixel_width"], int(entry.bounds.width))
            self.assertEqual(placements[0]["rendered_width_px"], V1_DEFAULT_DISPLAY_WIDTH_PX)

    def test_v1_page_as_is_secondary_preserves_primary_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = self._make_problem_entry(root, "page-1", Box(0, 0, 1697, 2400))
            entry.input_intent = "page-as-is"
            entry.processing_step = problem_board.PROCESSING_STEP_CHALK

            records, _placements = build_image_only_records(
                [entry],
                LayoutTemplate(name="academy-default"),
                crop_format=CROP_FORMAT_V1,
                dark_board=False,
            )

            images = parse_embedded_images(records[0])
            self.assertEqual([(1697, 2400), (1697, 2400)], [
                (image.width, image.height) for image in images
            ])
            primary = records[0][images[0].offset : images[0].offset + images[0].length]
            secondary = records[0][images[1].offset : images[1].offset + images[1].length]
            self.assertEqual(primary, secondary)

    def test_raw_page_as_is_reuses_exact_source_bytes_on_dark_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = self._make_problem_entry(root, "page-1", Box(0, 0, 1697, 2400))
            source = Image.new("RGB", (1697, 2400), "white")
            ImageDraw.Draw(source).text((120, 180), "원본 페이지 픽셀 보존", fill="black")
            source.save(entry.crop_path, format="PNG", compress_level=2)
            entry.board_render_path = entry.crop_path
            entry.input_intent = "page-as-is"
            entry.force_full_page_bounds = True
            entry.processing_step = problem_board.PROCESSING_STEP_RAW
            expected_bytes = entry.crop_path.read_bytes()

            with mock.patch.object(
                problem_board,
                "_load_board_export_image",
                side_effect=AssertionError("raw page-as-is must not remove the paper background"),
            ), mock.patch.object(
                problem_board,
                "_encode_image_bytes",
                side_effect=AssertionError("raw page-as-is must not re-encode the source"),
            ):
                records, placements = build_image_only_records(
                    [entry],
                    LayoutTemplate(name="academy-default"),
                    crop_format=CROP_FORMAT_V1,
                    dark_board=True,
                )

            images = parse_embedded_images(records[0])
            primary = records[0][images[0].offset : images[0].offset + images[0].length]
            secondary = records[0][images[1].offset : images[1].offset + images[1].length]
            self.assertEqual(expected_bytes, primary)
            self.assertEqual(expected_bytes, secondary)
            self.assertEqual("source-preserving", placements[0]["image_resolution_policy"])

    def test_v1_regular_problem_secondary_keeps_legacy_preview_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = self._make_problem_entry(root, "problem-1", Box(0, 0, 1200, 1600))

            records, _placements = build_image_only_records(
                [entry],
                LayoutTemplate(name="academy-default"),
                crop_format=CROP_FORMAT_V1,
                dark_board=False,
            )

            images = parse_embedded_images(records[0])
            self.assertEqual((1200, 1600), (images[0].width, images[0].height))
            self.assertEqual((576, 768), (images[1].width, images[1].height))

    def test_v1_page_as_is_secondary_respects_edge_and_pixel_budgets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = self._make_problem_entry(root, "page-1", Box(0, 0, 600, 900))
            entry.input_intent = "page-as-is"

            with (
                mock.patch.object(edb_builder, "V1_PAGE_AS_IS_SECONDARY_MAX_EDGE_PX", 700),
                mock.patch.object(edb_builder, "V1_PAGE_AS_IS_SECONDARY_MAX_PIXELS", 400_000),
            ):
                records, _placements = build_image_only_records(
                    [entry],
                    LayoutTemplate(name="academy-default"),
                    crop_format=CROP_FORMAT_V1,
                    dark_board=False,
                )

            secondary = parse_embedded_images(records[0])[1]
            self.assertLessEqual(max(secondary.width, secondary.height), 700)
            self.assertLessEqual(secondary.width * secondary.height, 400_000)
            self.assertAlmostEqual(600 / 900, secondary.width / secondary.height, places=2)

    def test_publish_entries_recalculate_actual_height_from_current_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crop_path = root / "long-reconstructed.png"
            Image.new("RGBA", (400, 1200), (0, 0, 0, 0)).save(crop_path)
            template = LayoutTemplate(
                name="academy-default",
                base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
            )

            entries = _problems_to_entries(
                [
                    {
                        "id": "problem-long",
                        "title": "long",
                        "imagePath": crop_path.resolve().as_uri(),
                        "boardRenderPath": crop_path.resolve().as_uri(),
                        "actualHeightPages": 0.72,
                        "bbox": {"left": 0, "top": 0, "width": 400, "height": 1200},
                    }
                ],
                template=template,
            )

            self.assertEqual(1, len(entries))
            self.assertGreater(entries[0].actual_height_pages, ONE_PROBLEM_SLOT_HEIGHT_PAGES)

    def test_source_page_chrome_trim_removes_edge_tabs_and_blue_footer(self):
        image = Image.new("RGB", (900, 1100), "white")
        draw = ImageDraw.Draw(image)
        draw.text((64, 80), "5. problem body", fill="black")
        draw.text((64, 910), "1 2 3 4 5", fill="black")
        draw.line((0, 0, 0, 1030), fill="black", width=3)
        draw.rectangle((0, 1010, 54, 1099), outline="black", width=2)
        draw.text((10, 1038), "32", fill="black")
        draw.rectangle((820, 720, 899, 1000), fill=(180, 180, 180))
        draw.text((840, 820), "지구과학", fill="white")
        draw.text((200, 1060), "이 문제지에 관한 저작권은 한국교육과정평가원에 있습니다.", fill=(30, 70, 250))

        cleaned = _trim_source_page_chrome(image)

        self.assertLess(cleaned.width, 840)
        self.assertLess(cleaned.height, 1080)
        self.assertGreater(cleaned.width, 650)
        self.assertGreater(cleaned.height, 900)
        self.assertEqual(cleaned.getpixel((8, cleaned.height - 8)), (255, 255, 255))

    def test_bottom_blue_watermark_ignores_vertical_guide_column(self):
        # A cyan column divider swallowed by horizontal crop expansion runs the
        # full height of the crop. It must not be mistaken for the blue footer,
        # which previously chopped the answer row off every textbook problem.
        image = Image.new("RGB", (820, 913), "white")
        draw = ImageDraw.Draw(image)
        draw.text((64, 80), "1. problem body", fill="black")
        draw.text((64, 860), "1 2 3 4 5", fill="black")
        draw.rectangle((814, 0, 818, 912), fill=(90, 200, 235))

        cleaned = _trim_bottom_blue_watermark(image)

        self.assertEqual(cleaned.size, image.size)

    def test_bottom_blue_watermark_still_trims_horizontal_footer(self):
        image = Image.new("RGB", (820, 913), "white")
        draw = ImageDraw.Draw(image)
        draw.text((64, 80), "1. problem body", fill="black")
        draw.text((120, 872), "이 문제지에 관한 저작권은 한국교육과정평가원에 있습니다.", fill=(30, 70, 250))

        cleaned = _trim_bottom_blue_watermark(image)

        self.assertLess(cleaned.height, 872)
        self.assertGreater(cleaned.height, int(913 * 0.72))

    def test_trusted_pdf_marker_crop_trims_column_divider_and_footer_badge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            image = Image.new("RGB", (900, 1120), "white")
            draw = ImageDraw.Draw(image)
            draw.line((450, 140, 450, 1042), fill="black", width=3)
            draw.text((500, 180), "3. problem body", fill="black")
            draw.text((500, 340), "choices 1 2 3 4 5", fill="black")
            draw.rectangle((410, 1040, 490, 1094), outline="black", width=2)
            draw.line((410, 1094, 490, 1040), fill="black", width=2)
            draw.text((424, 1056), "1", fill="black")
            draw.text((462, 1068), "20", fill="black")
            draw.text((560, 1090), "copyright footer", fill=(30, 70, 250))
            image.save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=image,
                original_size=(900, 1120),
            )
            block = ContentBlock(
                block_id="p3-stem",
                block_type=BlockType.STEM,
                bbox=Box(486, 160, 350, 920),
                reading_order=0,
                text="3. problem body",
                metadata={
                    "segmenter": "pdf-text-markers",
                    "problem_number_source": "pdf_text_marker",
                    "column_index": 1,
                },
            )
            page = PageModel(
                page_id="page-1",
                width_px=900,
                height_px=1120,
                subject=Subject.MATH,
                source_path=str(source),
                blocks=[block],
                problems=[
                    ProblemUnit(
                        unit_id="problem-3",
                        subject=Subject.MATH,
                        title="3.",
                        stem_block_ids=["p3-stem"],
                        metadata={
                            "problem_number": 3,
                            "problem_number_source": "pdf_text_marker",
                            "column_index": 1,
                        },
                    )
                ],
            )

            entries = build_problem_entries(
                [prepared],
                [page],
                root / "out",
                LayoutTemplate(name="academy-default"),
            )

            self.assertEqual(1, len(entries))
            crop = Image.open(entries[0].crop_path).convert("RGB")
            self.assertGreaterEqual(crop.width, round(entries[0].bounds.width))
            self.assertLessEqual(crop.width, round(entries[0].bounds.width) + 64)
            gray = crop.convert("L")
            left_band_dark = sum(
                1
                for x in range(min(14, crop.width))
                for y in range(crop.height)
                if gray.getpixel((x, y)) < 80
            )
            bottom_left_dark = sum(
                1
                for x in range(min(70, crop.width))
                for y in range(max(0, crop.height - 70), crop.height)
                if gray.getpixel((x, y)) < 80
            )
            bottom_blue = sum(
                1
                for x in range(crop.width)
                for y in range(max(0, crop.height - 34), crop.height)
                if (pixel := crop.getpixel((x, y)))[2] >= pixel[0] + 22 and pixel[2] >= pixel[1] + 8
            )

            self.assertLess(left_band_dark, 20)
            # Preserve the full PDF column instead of deleting its left edge.
            # A few disconnected antialiased badge glyph pixels may remain,
            # but the footer frame itself must be gone.
            self.assertLess(bottom_left_dark, 80)
            self.assertLess(bottom_blue, 10)

    def test_recrop_problem_uses_full_page_chrome_trim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = Image.new("RGB", (640, 760), "white")
            draw = ImageDraw.Draw(image)
            draw.line((320, 40, 320, 690), fill="black", width=3)
            draw.text((360, 80), "12. recropped problem", fill="black")
            draw.rectangle((290, 690, 350, 735), outline="black", width=2)
            draw.line((290, 735, 350, 690), fill="black", width=2)
            draw.text((302, 706), "6", fill="black")
            draw.text((332, 710), "20", fill="black")
            draw.text((390, 732), "copyright footer", fill=(30, 70, 250))
            crop_path = root / "crop.png"

            problem_board.recrop_problem(
                image,
                Box(left=312, top=54, width=280, height=700),
                crop_path,
            )

            crop = Image.open(crop_path).convert("RGB")
            gray = crop.convert("L")
            left_band_dark = sum(
                1
                for x in range(min(14, crop.width))
                for y in range(crop.height)
                if gray.getpixel((x, y)) < 80
            )
            bottom_left_dark = sum(
                1
                for x in range(min(70, crop.width))
                for y in range(max(0, crop.height - 70), crop.height)
                if gray.getpixel((x, y)) < 80
            )
            bottom_blue = sum(
                1
                for x in range(crop.width)
                for y in range(max(0, crop.height - 34), crop.height)
                if (pixel := crop.getpixel((x, y)))[2] >= pixel[0] + 22 and pixel[2] >= pixel[1] + 8
            )

            self.assertLess(left_band_dark, 20)
            self.assertLess(bottom_left_dark, 20)
            self.assertLess(bottom_blue, 10)

    def test_problem_crops_use_same_column_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            Image.new("RGB", (1000, 1400), "white").save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.open(source).convert("RGB"),
                original_size=(1000, 1400),
            )

            blocks: list[ContentBlock] = []
            problems: list[ProblemUnit] = []

            def add_problem(number: int, column: int, top: float, bottom: float, order: int) -> None:
                block_id = f"b-{number}"
                blocks.append(
                    ContentBlock(
                        block_id=block_id,
                        block_type=BlockType.STEM,
                        bbox=Box(80 + column * 460, top, 360, bottom - top),
                        reading_order=order,
                        text=f"{number}. problem",
                        metadata={"column_index": column, "question_band_index": order},
                    )
                )
                problems.append(
                    ProblemUnit(
                        unit_id=f"problem-{number}",
                        subject=Subject.MATH,
                        title=f"{number}.",
                        stem_block_ids=[block_id],
                        metadata={
                            "problem_number": number,
                            "column_index": column,
                            "question_band_index": order,
                        },
                    )
                )

            add_problem(7, 0, 100, 250, 0)
            add_problem(8, 0, 500, 650, 1)
            add_problem(9, 0, 900, 1050, 2)
            add_problem(10, 1, 100, 280, 3)
            add_problem(11, 1, 520, 894, 4)
            add_problem(12, 1, 900, 1080, 5)

            page = PageModel(
                page_id="page-1",
                width_px=1000,
                height_px=1400,
                subject=Subject.MATH,
                source_path=str(source),
                blocks=blocks,
                problems=problems,
            )

            entries = build_problem_entries(
                [prepared],
                [page],
                root / "out",
                LayoutTemplate(name="academy-default"),
            )

            problem_11 = next(entry for entry in entries if entry.problem_number == 11)
            problem_12 = next(entry for entry in entries if entry.problem_number == 12)
            self.assertLessEqual(problem_11.bounds.bottom, 895)
            self.assertLessEqual(problem_12.bounds.top, 886)

    def test_marker_document_continuation_page_preserves_single_review_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            Image.new("RGB", (720, 960), "white").save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.open(source).convert("RGB"),
                original_size=(720, 960),
                metadata={
                    "source_type": "hwp",
                    "document_like": True,
                    "hwp_conversion_quality": {
                        "has_pdf_text_markers": True,
                        "pdf_text_marker_count": 45,
                    },
                },
            )
            blocks = [
                ContentBlock(
                    block_id="tail-1",
                    block_type=BlockType.STEM,
                    bbox=Box(80, 120, 560, 150),
                    reading_order=0,
                    text=None,
                    metadata={
                        "segmenter": "document-bands",
                        "column_index": 0,
                        "question_band_index": 0,
                        "source_band_index": 0,
                    },
                ),
                ContentBlock(
                    block_id="tail-2",
                    block_type=BlockType.CHOICE,
                    bbox=Box(95, 310, 520, 90),
                    reading_order=1,
                    text=None,
                    metadata={
                        "segmenter": "document-bands",
                        "column_index": 0,
                        "question_band_index": 0,
                        "source_band_index": 0,
                    },
                ),
            ]
            page = PageModel(
                page_id="page-1",
                width_px=720,
                height_px=960,
                subject=Subject.KOREAN,
                source_path=str(source),
                blocks=blocks,
                problems=[
                    ProblemUnit(
                        unit_id="page-1-problem-1",
                        subject=Subject.KOREAN,
                        title=None,
                        stem_block_ids=["tail-1"],
                        choice_block_ids=["tail-2"],
                        metadata={
                            "fallback_grouping": True,
                            "grouping_source": "fallback_by_band",
                            "question_band_index": 0,
                            "column_index": 0,
                        },
                    )
                ],
                metadata={
                    "source_type": "hwp",
                    "document_like": True,
                    "segmenter": "document-bands",
                    "pdf_problem_markers": [],
                    "pdf_text_marker_count": 0,
                    "hwp_conversion_quality": {
                        "has_pdf_text_markers": True,
                        "pdf_text_marker_count": 45,
                    },
                },
            )

            entries = build_problem_entries(
                [prepared],
                [page],
                root / "out",
                LayoutTemplate(name="academy-default"),
            )

            self.assertEqual(1, len(entries))
            self.assertEqual("이어지는 자료", entries[0].title)
            self.assertIsNone(entries[0].problem_number)
            self.assertEqual("page-1-continuation", entries[0].problem_id)
            self.assertEqual(0.0, entries[0].bounds.left)
            self.assertEqual(0.0, entries[0].bounds.top)
            self.assertEqual(720.0, entries[0].bounds.width)
            self.assertEqual(960.0, entries[0].bounds.height)
            self.assertIn("marker_document_continuation", entries[0].risk_flags)
            self.assertEqual(["page-1-continuation"], [problem.unit_id for problem in page.problems])

    def test_mvp_export_preserves_marker_document_continuation_crop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            Image.new("RGB", (720, 960), "white").save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.open(source).convert("RGB"),
                original_size=(720, 960),
                metadata={
                    "source_type": "hwp",
                    "document_like": True,
                    "hwp_conversion_quality": {
                        "has_pdf_text_markers": True,
                        "pdf_text_marker_count": 45,
                    },
                },
            )
            page = PageModel(
                page_id="page-1",
                width_px=720,
                height_px=960,
                subject=Subject.KOREAN,
                source_path=str(source),
                blocks=[
                    ContentBlock(
                        block_id="tail-image",
                        block_type=BlockType.IMAGE,
                        bbox=Box(80, 120, 560, 540),
                        reading_order=0,
                        text=None,
                        metadata={
                            "segmenter": "document-bands",
                            "column_index": 1,
                            "question_band_index": 1,
                            "source_band_index": 1,
                        },
                    )
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-problem-1",
                        subject=Subject.KOREAN,
                        title=None,
                        figure_block_ids=["tail-image"],
                        metadata={
                            "fallback_grouping": True,
                            "grouping_source": "fallback_grouping",
                            "question_band_index": 1,
                            "column_index": 1,
                        },
                    )
                ],
                metadata={
                    "source_type": "hwp",
                    "document_like": True,
                    "segmenter": "document-bands",
                    "pdf_problem_markers": [],
                    "hwp_conversion_quality": {
                        "has_pdf_text_markers": True,
                        "pdf_text_marker_count": 45,
                    },
                },
            )

            crop_paths = _render_problem_crops([page], [prepared], root / "problem_crops")

            self.assertEqual(["page-1-continuation"], list(crop_paths))
            self.assertTrue(crop_paths["page-1-continuation"].exists())
            self.assertEqual(["page-1-continuation"], [problem.unit_id for problem in page.problems])
            self.assertEqual("이어지는 자료", page.problems[0].title)
            self.assertIn("marker_document_continuation", page.problems[0].metadata["risk_flags"])

    def test_mvp_export_keeps_fallback_crops_when_pdf_markers_are_sparse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            Image.new("RGB", (720, 960), "white").save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.open(source).convert("RGB"),
                original_size=(720, 960),
                metadata={
                    "source_type": "hwp",
                    "document_like": True,
                    "hwp_conversion_quality": {
                        "has_pdf_text_markers": True,
                        "pdf_text_marker_count": 3,
                        "pdf_text_markers_reliable": False,
                    },
                },
            )
            page = PageModel(
                page_id="page-1",
                width_px=720,
                height_px=960,
                subject=Subject.SOCIAL,
                source_path=str(source),
                blocks=[
                    ContentBlock(
                        block_id="fallback-image",
                        block_type=BlockType.IMAGE,
                        bbox=Box(80, 120, 560, 540),
                        reading_order=0,
                        text=None,
                        metadata={
                            "segmenter": "document-bands",
                            "column_index": 1,
                            "question_band_index": 1,
                            "source_band_index": 1,
                        },
                    )
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-problem-1",
                        subject=Subject.SOCIAL,
                        title=None,
                        figure_block_ids=["fallback-image"],
                        metadata={
                            "fallback_grouping": True,
                            "grouping_source": "fallback_grouping",
                            "question_band_index": 1,
                            "column_index": 1,
                        },
                    )
                ],
                metadata={
                    "source_type": "hwp",
                    "document_like": True,
                    "segmenter": "document-bands",
                    "pdf_problem_markers": [],
                    "hwp_conversion_quality": {
                        "has_pdf_text_markers": True,
                        "pdf_text_marker_count": 3,
                        "pdf_text_markers_reliable": False,
                    },
                },
            )

            crop_paths = _render_problem_crops([page], [prepared], root / "problem_crops")

            self.assertEqual(["page-1-problem-1"], list(crop_paths))
            self.assertTrue(crop_paths["page-1-problem-1"].exists())

    def test_mvp_export_skips_unnumbered_marker_document_continuation_without_fallback_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            Image.new("RGB", (720, 960), "white").save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.open(source).convert("RGB"),
                original_size=(720, 960),
            )
            page = PageModel(
                page_id="page-1",
                width_px=720,
                height_px=960,
                subject=Subject.MATH,
                source_path=str(source),
                blocks=[
                    ContentBlock(
                        block_id="tail-image",
                        block_type=BlockType.IMAGE,
                        bbox=Box(80, 120, 560, 120),
                        reading_order=0,
                        text=None,
                        metadata={
                            "segmenter": "document-bands",
                            "column_index": 0,
                            "question_band_index": 0,
                            "source_band_index": 0,
                        },
                    )
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-problem-1",
                        subject=Subject.MATH,
                        title=None,
                        figure_block_ids=["tail-image"],
                        metadata={"grouping_source": "text_markers_unavailable"},
                    )
                ],
                metadata={
                    "source_type": "hwp",
                    "document_like": True,
                    "segmenter": "document-bands",
                    "pdf_problem_markers": [],
                    "hwp_conversion_quality": {
                        "has_pdf_text_markers": True,
                        "pdf_text_marker_count": 30,
                        "pdf_text_markers_reliable": True,
                    },
                },
            )

            crop_paths = _render_problem_crops([page], [prepared], root / "problem_crops")

            self.assertEqual({}, crop_paths)

    def test_mvp_export_preserves_marker_document_continuation_before_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.new("RGB", (720, 960), "white"),
                original_size=(720, 960),
                metadata={
                    "source_type": "hwp",
                    "document_like": True,
                    "hwp_conversion_quality": {
                        "has_pdf_text_markers": True,
                        "pdf_text_marker_count": 45,
                    },
                },
            )
            page = PageModel(
                page_id="page-1",
                width_px=720,
                height_px=960,
                subject=Subject.KOREAN,
                source_path=str(source),
                blocks=[
                    ContentBlock(
                        block_id="tail-image",
                        block_type=BlockType.IMAGE,
                        bbox=Box(80, 120, 560, 540),
                        reading_order=0,
                        text=None,
                        metadata={
                            "segmenter": "document-bands",
                            "column_index": 1,
                            "question_band_index": 1,
                            "source_band_index": 1,
                        },
                    )
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-problem-1",
                        subject=Subject.KOREAN,
                        title=None,
                        figure_block_ids=["tail-image"],
                        metadata={
                            "fallback_grouping": True,
                            "grouping_source": "fallback_grouping",
                            "question_band_index": 1,
                            "column_index": 1,
                        },
                    )
                ],
                metadata={
                    "source_type": "hwp",
                    "document_like": True,
                    "segmenter": "document-bands",
                    "pdf_problem_markers": [],
                    "hwp_conversion_quality": {
                        "has_pdf_text_markers": True,
                        "pdf_text_marker_count": 45,
                    },
                },
            )

            with (
                mock.patch("build_mvp_export.prepare_source_pages", return_value=[prepared]),
                mock.patch("build_mvp_export.build_page_model", return_value=page),
            ):
                result = run_mvp_export(
                    source,
                    output_dir=root / "out",
                    subject_name="korean",
                    ocr="none",
                    sync_ui=False,
                )

            self.assertEqual(["page-1-continuation"], list(result["problem_crop_paths"]))
            self.assertEqual(1, result["ui_session"]["detected_problem_count"])
            self.assertEqual(0, result["ui_session"]["core_problem_count"])
            self.assertEqual(1, result["ui_session"]["supplemental_item_count"])
            self.assertEqual(1, len(result["ui_session"]["problems"]))
            self.assertEqual("이어지는 자료", result["ui_session"]["problems"][0]["title"])
            self.assertCountEqual(
                ["marker_document_continuation", "fallback_grouping"],
                result["ui_session"]["problems"][0]["riskFlags"],
            )
            pages_payload = json.loads((root / "out" / "pages.json").read_text(encoding="utf-8"))
            self.assertEqual(["page-1-continuation"], [problem["unit_id"] for problem in pages_payload[0]["problems"]])
            self.assertTrue(pages_payload[0]["problems"][0]["metadata"]["marker_document_continuation"])
            self.assertEqual(["page-1-continuation"], result["ui_session"]["pages"][0]["problemIds"])

    def test_mvp_export_skips_hwp_template_instruction_fallback_band(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "school.hwp"
            source.write_bytes(b"hwp")
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.new("RGB", (720, 960), "white"),
                original_size=(720, 960),
                metadata={
                    "source_type": "hwp",
                    "document_like": True,
                    "hwp_preview_text": (
                        "단어의 뜻이 옳게 짝지어진 것은?\n"
                        "개요 번호 모양 서식 적용되어 있습니다.\n"
                        "Ctrl+3 누르면 지시문(1., 2., 3.)\n"
                        "위 네모칸 표는 복사 붙여넣어서 사용하세요."
                    ),
                    "hwp_conversion_quality": {
                        "has_pdf_text_markers": False,
                        "pdf_text_marker_count": 0,
                        "pdf_text_markers_reliable": False,
                        "preferred_segmentation_path": "ocr_fallback",
                    },
                },
            )
            page = PageModel(
                page_id="page-1",
                width_px=720,
                height_px=960,
                subject=Subject.ENGLISH,
                source_path=str(source),
                blocks=[
                    ContentBlock(
                        block_id="question-band",
                        block_type=BlockType.IMAGE,
                        bbox=Box(80, 80, 520, 180),
                        reading_order=0,
                        text=None,
                        metadata={
                            "segmenter": "document-bands",
                            "column_index": 1,
                            "question_band_index": 1,
                            "source_band_index": 1,
                        },
                    ),
                    ContentBlock(
                        block_id="instruction-band",
                        block_type=BlockType.IMAGE,
                        bbox=Box(80, 360, 540, 260),
                        reading_order=1,
                        text=None,
                        metadata={
                            "segmenter": "document-bands",
                            "column_index": 1,
                            "question_band_index": 2,
                            "source_band_index": 2,
                        },
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-problem-1",
                        subject=Subject.ENGLISH,
                        title=None,
                        figure_block_ids=["question-band"],
                        metadata={
                            "fallback_grouping": True,
                            "grouping_source": "fallback_grouping",
                            "question_band_index": 1,
                            "column_index": 1,
                        },
                    ),
                    ProblemUnit(
                        unit_id="page-1-problem-2",
                        subject=Subject.ENGLISH,
                        title=None,
                        figure_block_ids=["instruction-band"],
                        metadata={
                            "fallback_grouping": True,
                            "grouping_source": "fallback_grouping",
                            "question_band_index": 2,
                            "column_index": 1,
                        },
                    ),
                ],
                metadata={
                    "source_type": "hwp",
                    "document_like": True,
                    "segmenter": "document-bands",
                    "hwp_preview_text": prepared.metadata["hwp_preview_text"],
                    "hwp_conversion_quality": prepared.metadata["hwp_conversion_quality"],
                },
            )

            with (
                mock.patch("build_mvp_export.prepare_source_pages", return_value=[prepared]),
                mock.patch("build_mvp_export.build_page_model", return_value=page),
            ):
                result = run_mvp_export(
                    source,
                    output_dir=root / "out",
                    subject_name="english",
                    ocr="none",
                    sync_ui=False,
                )

            self.assertEqual(["page-1-problem-1"], list(result["problem_crop_paths"]))
            self.assertEqual(["page-1-problem-1"], [problem["id"] for problem in result["ui_session"]["problems"]])
            pages_payload = json.loads((root / "out" / "pages.json").read_text(encoding="utf-8"))
            self.assertEqual(["page-1-problem-1"], [problem["unit_id"] for problem in pages_payload[0]["problems"]])
            self.assertEqual(
                ["page-1-problem-2"],
                pages_payload[0]["metadata"]["template_instruction_problem_ids_skipped"],
            )
            with Image.open(result["problem_crop_paths"]["page-1-problem-1"]) as crop:
                self.assertGreaterEqual(crop.height, 320)

    def test_hwp_count_match_suppresses_page_count_similarity_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_image = root / "page.png"
            crop_path = root / "crop.png"
            Image.new("RGB", (900, 1200), "white").save(source_image)
            Image.new("RGB", (600, 420), "white").save(crop_path)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source_image),
                page_number=1,
                image=Image.new("RGB", (900, 1200), "white"),
                original_size=(900, 1200),
                metadata={
                    "source_type": "hwp",
                    "source_hwp_path": str(root / "single.hwp"),
                    "hwp_conversion_quality": {
                        "hwp_text_extractor": "rhwp-markdown",
                        "hwp_text_numbered_problem_count": 1,
                    },
                },
            )
            page = PageModel(
                page_id="page-1",
                width_px=900,
                height_px=1200,
                subject=Subject.KOREAN,
                source_path=str(source_image),
                blocks=[
                    ContentBlock(
                        block_id="band-1",
                        block_type=BlockType.IMAGE,
                        bbox=Box(left=60, top=80, width=620, height=360),
                        reading_order=1,
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="p1",
                        subject=Subject.KOREAN,
                        title=None,
                        figure_block_ids=["band-1"],
                    )
                ],
                metadata=prepared.metadata,
            )
            placement = {
                "problem_id": "p1",
                "title": "1",
                "problem_number": 1,
                "subject": "국어",
                "source_page_id": "page-1",
                "source_path": str(source_image),
                "crop_path": str(crop_path),
                "board_render_path": str(crop_path),
                "actual_content_height_pages": 0.75,
                "overflow_allowed": False,
                "overflow_violation": False,
                "overflow_amount_pages": 0.0,
                "slot_span_count": 1,
                "start_y_pages": 0.0,
                "snapped_next_start_y_pages": 1.0,
                "placement_x_ratio": 0.0,
                "placement_y_ratio": 0.0,
                "placement_scale_ratio": 1.0,
                "record_mode": "problem",
                "processing_step": PROCESSING_STEP_RECONSTRUCT,
                "text_record_count": 0,
                "image_record_count": 1,
                "bbox": {"left": 60, "top": 80, "width": 620, "height": 360},
                "risk_flags": ["fallback_grouping"],
            }

            session = build_problem_ui_session(
                [prepared],
                [placement],
                root / "out",
                None,
                [root / "single.hwp"],
                record_mode="problem",
                pages=[page],
                input_intent="exam",
            )

        self.assertFalse(
            [
                message
                for message in session["warning_messages"]
                if "원본 페이지 수와 비슷" in message
            ]
        )

    def test_mvp_export_preserves_repeated_marker_problem_numbers_in_page_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            quality = {
                "has_pdf_text_markers": True,
                "pdf_text_marker_count": 2,
                "pdf_text_markers_reliable": True,
            }

            prepared_pages = []
            page_models = []
            for page_index in range(2):
                page_id = f"page-{page_index + 1}"
                prepared_pages.append(
                    PreparedPage(
                        page_id=page_id,
                        source_path=str(source),
                        page_number=page_index + 1,
                        image=Image.new("RGB", (720, 960), "white"),
                        original_size=(720, 960),
                        metadata={
                            "source_type": "hwp",
                            "source_hwp_path": str(source),
                            "document_like": True,
                            "hwp_conversion_quality": quality,
                        },
                    )
                )
                block_id = f"{page_id}-block-1"
                page_models.append(
                    PageModel(
                        page_id=page_id,
                        width_px=720,
                        height_px=960,
                        subject=Subject.MATH,
                        source_path=str(source),
                        blocks=[
                            ContentBlock(
                                block_id=block_id,
                                block_type=BlockType.TITLE,
                                bbox=Box(80, 120, 560, 120),
                                reading_order=0,
                                text="1.",
                                metadata={
                                    "segmenter": "pdf-text-markers",
                                    "problem_number": 1,
                                },
                            )
                        ],
                        problems=[
                            ProblemUnit(
                                unit_id=f"{page_id}-problem-1",
                                subject=Subject.MATH,
                                title="1.",
                                stem_block_ids=[block_id],
                                metadata={
                                    "problem_number": 1,
                                    "problem_number_source": "pdf_text_marker",
                                },
                            )
                        ],
                        metadata={
                            "source_type": "hwp",
                            "source_hwp_path": str(source),
                            "document_like": True,
                            "segmenter": "pdf-text-markers",
                            "hwp_conversion_quality": quality,
                        },
                    )
                )

            with (
                mock.patch("build_mvp_export.prepare_source_pages", return_value=prepared_pages),
                mock.patch("build_mvp_export.build_page_model", side_effect=page_models),
            ):
                result = run_mvp_export(
                    source,
                    output_dir=root / "out",
                    subject_name="math",
                    ocr="none",
                    sync_ui=False,
                )

            self.assertEqual(["page-1-problem-1", "page-2-problem-1"], list(result["problem_crop_paths"]))
            self.assertEqual(
                ["page-1-problem-1", "page-2-problem-1"],
                [problem["id"] for problem in result["ui_session"]["problems"]],
            )
            pages_payload = json.loads((root / "out" / "pages.json").read_text(encoding="utf-8"))
            self.assertEqual(["page-2-problem-1"], [problem["unit_id"] for problem in pages_payload[1]["problems"]])
            self.assertTrue(pages_payload[0]["metadata"]["duplicate_problem_numbers_preserved"])
            self.assertTrue(pages_payload[1]["metadata"]["duplicate_problem_numbers_preserved"])
            self.assertNotIn("duplicate_problem_numbers_skipped", pages_payload[1]["metadata"])

    def test_mvp_export_preserves_repeated_numbers_when_hwp_text_signal_expects_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            quality = {
                "has_pdf_text_markers": True,
                "pdf_text_marker_count": 2,
                "pdf_text_markers_reliable": True,
                "hwp_text_extractor": "hwp5txt",
                "hwp_text_numbered_problem_count": 2,
                "hwp_text_stem_problem_count": 0,
            }

            prepared_pages = []
            page_models = []
            for page_index, section in enumerate(("화법과 작문", "언어와 매체")):
                page_id = f"page-{page_index + 1}"
                prepared_pages.append(
                    PreparedPage(
                        page_id=page_id,
                        source_path=str(source),
                        page_number=page_index + 1,
                        image=Image.new("RGB", (720, 960), "white"),
                        original_size=(720, 960),
                        metadata={
                            "source_type": "hwp",
                            "source_hwp_path": str(source),
                            "document_like": True,
                            "hwp_conversion_quality": quality,
                        },
                    )
                )
                block_id = f"{page_id}-block-1"
                page_models.append(
                    PageModel(
                        page_id=page_id,
                        width_px=720,
                        height_px=960,
                        subject=Subject.KOREAN,
                        source_path=str(source),
                        blocks=[
                            ContentBlock(
                                block_id=block_id,
                                block_type=BlockType.TITLE,
                                bbox=Box(80, 120, 560, 120),
                                reading_order=0,
                                text="35.",
                                metadata={
                                    "segmenter": "pdf-text-markers",
                                    "problem_number": 35,
                                    "section_title": section,
                                },
                            )
                        ],
                        problems=[
                            ProblemUnit(
                                unit_id=f"{page_id}-problem-1",
                                subject=Subject.KOREAN,
                                title="35.",
                                stem_block_ids=[block_id],
                                metadata={
                                    "problem_number": 35,
                                    "problem_number_source": "pdf_text_marker",
                                },
                            )
                        ],
                        metadata={
                            "source_type": "hwp",
                            "source_hwp_path": str(source),
                            "document_like": True,
                            "segmenter": "pdf-text-markers",
                            "hwp_conversion_quality": quality,
                        },
                    )
                )

            with (
                mock.patch("build_mvp_export.prepare_source_pages", return_value=prepared_pages),
                mock.patch("build_mvp_export.build_page_model", side_effect=page_models),
            ):
                result = run_mvp_export(
                    source,
                    output_dir=root / "out",
                    subject_name="korean",
                    ocr="none",
                    sync_ui=False,
                )

            self.assertEqual(
                ["page-1-problem-1", "page-2-problem-1"],
                list(result["problem_crop_paths"]),
            )
            pages_payload = json.loads((root / "out" / "pages.json").read_text(encoding="utf-8"))
            self.assertTrue(pages_payload[0]["metadata"]["duplicate_problem_numbers_preserved"])
            self.assertTrue(pages_payload[1]["metadata"]["duplicate_problem_numbers_preserved"])
            self.assertNotIn("duplicate_problem_numbers_skipped", pages_payload[1]["metadata"])

    def test_mvp_export_flags_hwp_problem_count_mismatch_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            quality = {
                "has_pdf_text_markers": True,
                "pdf_text_marker_count": 2,
                "pdf_text_markers_reliable": True,
                "hwp_text_extractor": "hwp5txt",
                "hwp_text_numbered_problem_count": 5,
                "hwp_text_stem_problem_count": 0,
            }
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.new("RGB", (720, 960), "white"),
                original_size=(720, 960),
                metadata={
                    "source_type": "hwp",
                    "source_hwp_path": str(source),
                    "document_like": True,
                    "hwp_conversion_quality": quality,
                },
            )
            page = PageModel(
                page_id="page-1",
                width_px=720,
                height_px=960,
                subject=Subject.KOREAN,
                source_path=str(source),
                blocks=[
                    ContentBlock(
                        block_id="block-1",
                        block_type=BlockType.TITLE,
                        bbox=Box(80, 100, 560, 140),
                        reading_order=0,
                        text="1.",
                        metadata={"segmenter": "pdf-text-markers", "problem_number": 1},
                    ),
                    ContentBlock(
                        block_id="block-2",
                        block_type=BlockType.TITLE,
                        bbox=Box(80, 420, 560, 140),
                        reading_order=1,
                        text="2.",
                        metadata={"segmenter": "pdf-text-markers", "problem_number": 2},
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-problem-1",
                        subject=Subject.KOREAN,
                        title="1.",
                        stem_block_ids=["block-1"],
                        metadata={"problem_number": 1, "problem_number_source": "pdf_text_marker"},
                    ),
                    ProblemUnit(
                        unit_id="page-1-problem-2",
                        subject=Subject.KOREAN,
                        title="2.",
                        stem_block_ids=["block-2"],
                        metadata={"problem_number": 2, "problem_number_source": "pdf_text_marker"},
                    ),
                ],
                metadata={
                    "source_type": "hwp",
                    "source_hwp_path": str(source),
                    "document_like": True,
                    "segmenter": "pdf-text-markers",
                    "hwp_conversion_quality": quality,
                },
            )

            with (
                mock.patch("build_mvp_export.prepare_source_pages", return_value=[prepared]),
                mock.patch("build_mvp_export.build_page_model", return_value=page),
            ):
                result = run_mvp_export(
                    source,
                    output_dir=root / "out",
                    subject_name="korean",
                    ocr="none",
                    sync_ui=False,
                )

            ui_session = result["ui_session"]
            self.assertIn("hwp_problem_count_mismatch", ui_session["pages"][0]["riskFlags"])
            self.assertEqual("check_needed", ui_session["pages"][0]["reviewStatus"])
            self.assertTrue(
                any("HWP" in message and "5" in message and "2" in message for message in ui_session["warning_messages"])
            )

    def test_mvp_export_flags_hwp_oversegmentation_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            board_path = root / "board.png"
            crop_path = root / "crop.png"
            Image.new("RGB", (720, 960), "white").save(board_path)
            Image.new("RGB", (120, 80), "white").save(crop_path)
            quality = {
                "has_pdf_text_markers": True,
                "pdf_text_marker_count": 20,
                "pdf_text_markers_reliable": True,
                "hwp_text_extractor": "hwp5txt",
                "hwp_text_numbered_problem_count": 20,
                "hwp_text_stem_problem_count": 0,
            }
            problems = [
                ProblemUnit(
                    unit_id=f"page-1-problem-{index}",
                    subject=Subject.KOREAN,
                    title=f"{index}.",
                    metadata={"problem_number": index, "problem_number_source": "pdf_text_marker"},
                )
                for index in range(1, 56)
            ]
            page = PageModel(
                page_id="page-1",
                width_px=720,
                height_px=960,
                subject=Subject.KOREAN,
                source_path=str(source),
                problems=problems,
                metadata={
                    "source_type": "hwp",
                    "source_hwp_path": str(source),
                    "document_like": True,
                    "segmenter": "pdf-text-markers",
                    "hwp_conversion_quality": quality,
                },
            )
            placements = [
                SimpleNamespace(
                    problem_id=problem.unit_id,
                    subject=Subject.KOREAN,
                    start_y_pages=0.0,
                    nominal_slot_height_pages=1.0,
                    actual_content_height_pages=0.5,
                    actual_bottom_y_pages=0.5,
                    snapped_next_start_y_pages=1.0,
                    overflow_allowed=False,
                    overflow_amount_pages=0.0,
                    overflow_violation=False,
                    slot_span_count=1,
                    board_capacity_exceeded=False,
                    metadata={},
                )
                for problem in problems
            ]
            export_plan = SimpleNamespace(
                template=LayoutTemplate(name="academy-default"),
                placements=placements,
            )
            ui_session = build_mvp_ui_session(
                [page],
                export_plan,
                [board_path],
                {problem.unit_id: crop_path for problem in problems},
                root / "out",
                None,
                [source],
            )

            self.assertIn("hwp_problem_count_mismatch", ui_session["pages"][0]["riskFlags"])
            self.assertIn("hwp_oversegmentation", ui_session["pages"][0]["riskFlags"])
            self.assertEqual("check_needed", ui_session["pages"][0]["reviewStatus"])
            self.assertTrue(
                any(
                    "과분할" in message and "20" in message and "55" in message
                    for message in ui_session["warning_messages"]
                )
            )

    def test_problem_export_flags_hwp_problem_count_mismatch_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            quality = {
                "has_pdf_text_markers": True,
                "pdf_text_marker_count": 2,
                "pdf_text_markers_reliable": True,
                "hwp_text_extractor": "hwp5txt",
                "hwp_text_numbered_problem_count": 5,
                "hwp_text_stem_problem_count": 0,
            }
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.new("RGB", (720, 960), "white"),
                original_size=(720, 960),
                metadata={
                    "source_type": "hwp",
                    "source_hwp_path": str(source),
                    "document_like": True,
                    "hwp_conversion_quality": quality,
                },
            )
            page = PageModel(
                page_id="page-1",
                width_px=720,
                height_px=960,
                subject=Subject.KOREAN,
                source_path=str(source),
                blocks=[
                    ContentBlock(
                        block_id="block-1",
                        block_type=BlockType.TITLE,
                        bbox=Box(80, 100, 560, 140),
                        reading_order=0,
                        text="1.",
                        metadata={"segmenter": "pdf-text-markers", "problem_number": 1},
                    ),
                    ContentBlock(
                        block_id="block-2",
                        block_type=BlockType.TITLE,
                        bbox=Box(80, 420, 560, 140),
                        reading_order=1,
                        text="2.",
                        metadata={"segmenter": "pdf-text-markers", "problem_number": 2},
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-problem-1",
                        subject=Subject.KOREAN,
                        title="1.",
                        stem_block_ids=["block-1"],
                        metadata={"problem_number": 1, "problem_number_source": "pdf_text_marker"},
                    ),
                    ProblemUnit(
                        unit_id="page-1-problem-2",
                        subject=Subject.KOREAN,
                        title="2.",
                        stem_block_ids=["block-2"],
                        metadata={"problem_number": 2, "problem_number_source": "pdf_text_marker"},
                    ),
                ],
                metadata={
                    "source_type": "hwp",
                    "source_hwp_path": str(source),
                    "document_like": True,
                    "segmenter": "pdf-text-markers",
                    "hwp_conversion_quality": quality,
                },
            )

            with mock.patch("build_problem_board_edb.build_pages", return_value=([prepared], [page])):
                result = run_problem_export(
                    source,
                    output_dir=root / "out",
                    subject_name="korean",
                    ocr="none",
                    record_mode="image-only",
                    export_edb=False,
                    sync_ui=False,
                )

            ui_session = result["ui_session"]
            self.assertIn("hwp_problem_count_mismatch", ui_session["pages"][0]["riskFlags"])
            self.assertEqual("check_needed", ui_session["pages"][0]["reviewStatus"])
            self.assertTrue(
                any("HWP" in message and "5" in message and "2" in message for message in ui_session["warning_messages"])
            )

    def test_problem_export_flags_hwp_oversegmentation_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            crop_path = root / "crop.png"
            Image.new("RGB", (120, 80), "white").save(crop_path)
            quality = {
                "has_pdf_text_markers": True,
                "pdf_text_marker_count": 20,
                "pdf_text_markers_reliable": True,
                "hwp_text_extractor": "hwp5txt",
                "hwp_text_numbered_problem_count": 20,
                "hwp_text_stem_problem_count": 0,
            }
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.new("RGB", (720, 960), "white"),
                original_size=(720, 960),
                metadata={
                    "source_type": "hwp",
                    "source_hwp_path": str(source),
                    "document_like": True,
                    "hwp_conversion_quality": quality,
                },
            )
            problems = [
                ProblemUnit(
                    unit_id=f"page-1-problem-{index}",
                    subject=Subject.KOREAN,
                    title=f"{index}.",
                    metadata={"problem_number": index, "problem_number_source": "pdf_text_marker"},
                )
                for index in range(1, 56)
            ]
            page = PageModel(
                page_id="page-1",
                width_px=720,
                height_px=960,
                subject=Subject.KOREAN,
                source_path=str(source),
                problems=problems,
                metadata={
                    "source_type": "hwp",
                    "source_hwp_path": str(source),
                    "document_like": True,
                    "segmenter": "pdf-text-markers",
                    "hwp_conversion_quality": quality,
                },
            )
            placements = [
                {
                    "problem_id": problem.unit_id,
                    "title": problem.title,
                    "problem_number": index,
                    "subject": "korean",
                    "crop_path": str(crop_path),
                    "source_path": str(source),
                    "source_page_id": "page-1",
                    "board_render_path": str(crop_path),
                    "actual_content_height_pages": 0.5,
                    "overflow_allowed": False,
                    "start_y_pages": 0.0,
                    "snapped_next_start_y_pages": 1.0,
                    "overflow_amount_pages": 0.0,
                    "overflow_violation": False,
                    "slot_span_count": 1,
                    "bbox": {"left": 0, "top": 0, "width": 120, "height": 80},
                    "risk_flags": [],
                    "record_mode": "image-only",
                    "text_record_count": 0,
                    "image_record_count": 1,
                }
                for index, problem in enumerate(problems, start=1)
            ]

            ui_session = build_problem_ui_session(
                [prepared],
                placements,
                root / "out",
                None,
                [source],
                record_mode="image-only",
                pages=[page],
                template=LayoutTemplate(name="academy-default"),
            )

            self.assertIn("hwp_problem_count_mismatch", ui_session["pages"][0]["riskFlags"])
            self.assertIn("hwp_oversegmentation", ui_session["pages"][0]["riskFlags"])
            self.assertEqual("check_needed", ui_session["pages"][0]["reviewStatus"])
            self.assertTrue(
                any(
                    "과분할" in message and "20" in message and "55" in message
                    for message in ui_session["warning_messages"]
                )
            )

    def test_problem_ui_session_does_not_apply_layout_duplicate_skips_to_text_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            crop_path = root / "crop.png"
            Image.new("RGB", (120, 80), "white").save(crop_path)
            quality = {
                "has_pdf_text_markers": True,
                "pdf_text_marker_count": 20,
                "pdf_text_markers_reliable": True,
                "hwp_text_extractor": "hwp5txt",
                "hwp_text_numbered_problem_count": 20,
                "hwp_text_stem_problem_count": 0,
                "hwp_layout_problem_marker_count": 31,
            }
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.new("RGB", (720, 960), "white"),
                original_size=(720, 960),
                metadata={
                    "source_type": "hwp",
                    "source_hwp_path": str(source),
                    "document_like": True,
                    "hwp_conversion_quality": quality,
                },
            )
            problems = [
                ProblemUnit(
                    unit_id=f"page-1-problem-{index}",
                    subject=Subject.KOREAN,
                    title=f"{index}.",
                    metadata={"problem_number": index, "problem_number_source": "pdf_text_marker"},
                )
                for index in range(1, 21)
            ]
            page = PageModel(
                page_id="page-1",
                width_px=720,
                height_px=960,
                subject=Subject.KOREAN,
                source_path=str(source),
                problems=problems,
                metadata={
                    "source_type": "hwp",
                    "source_hwp_path": str(source),
                    "document_like": True,
                    "segmenter": "pdf-text-markers",
                    "hwp_conversion_quality": quality,
                    "duplicate_problem_numbers_skipped": list(range(21, 32)),
                },
            )
            placements = [
                {
                    "problem_id": problem.unit_id,
                    "title": problem.title,
                    "problem_number": index,
                    "subject": "korean",
                    "crop_path": str(crop_path),
                    "source_path": str(source),
                    "source_page_id": "page-1",
                    "board_render_path": str(crop_path),
                    "actual_content_height_pages": 0.5,
                    "overflow_allowed": False,
                    "start_y_pages": 0.0,
                    "snapped_next_start_y_pages": 1.0,
                    "overflow_amount_pages": 0.0,
                    "overflow_violation": False,
                    "slot_span_count": 1,
                    "bbox": {"left": 0, "top": 0, "width": 120, "height": 80},
                    "risk_flags": [],
                    "record_mode": "image-only",
                    "text_record_count": 0,
                    "image_record_count": 1,
                }
                for index, problem in enumerate(problems, start=1)
            ]

            ui_session = build_problem_ui_session(
                [prepared],
                placements,
                root / "out",
                None,
                [source],
                record_mode="image-only",
                pages=[page],
                template=LayoutTemplate(name="academy-default"),
            )

            self.assertNotIn("hwp_problem_count_mismatch", ui_session["pages"][0]["riskFlags"])
            self.assertNotIn("hwp_oversegmentation", ui_session["pages"][0]["riskFlags"])
            self.assertFalse(
                any("HWP 내부 텍스트 기준 문항 수" in message for message in ui_session["warning_messages"])
            )

    def test_problem_ui_session_uses_final_placements_for_hwp_count_qa(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.hwp"
            source.write_bytes(b"hwp")
            crop_1 = root / "crop-1.png"
            crop_2 = root / "crop-2.png"
            crop_3 = root / "crop-3.png"
            Image.new("RGB", (120, 80), "white").save(crop_1)
            Image.new("RGB", (120, 80), "white").save(crop_2)
            Image.new("RGB", (120, 80), "white").save(crop_3)
            quality = {
                "hwp_text_extractor": "hwp5txt",
                "hwp_text_numbered_problem_count": 2,
                "hwp_text_stem_problem_count": 0,
            }
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.new("RGB", (720, 960), "white"),
                original_size=(720, 960),
                metadata={
                    "source_type": "hwp",
                    "source_hwp_path": str(source),
                    "hwp_conversion_quality": quality,
                },
            )
            page = PageModel(
                page_id="page-1",
                width_px=720,
                height_px=960,
                subject=Subject.KOREAN,
                source_path=str(source),
                blocks=[],
                problems=[
                    ProblemUnit(unit_id="kept-1", subject=Subject.KOREAN, title="1.", metadata={"problem_number": 1}),
                    ProblemUnit(unit_id="kept-2", subject=Subject.KOREAN, title="2.", metadata={"problem_number": 2}),
                    ProblemUnit(
                        unit_id="kept-continuation",
                        subject=Subject.KOREAN,
                        title="이어지는 자료",
                        metadata={"marker_document_continuation": True},
                    ),
                    ProblemUnit(unit_id="skipped-before-ui", subject=Subject.KOREAN, title="skip", metadata={}),
                ],
                metadata={
                    "source_type": "hwp",
                    "source_hwp_path": str(source),
                    "hwp_conversion_quality": quality,
                },
            )
            placements = [
                {
                    "problem_id": "kept-1",
                    "title": "1.",
                    "problem_number": 1,
                    "subject": "korean",
                    "crop_path": str(crop_1),
                    "source_path": str(source),
                    "source_page_id": "page-1",
                    "board_render_path": str(crop_1),
                    "actual_content_height_pages": 0.4,
                    "overflow_allowed": False,
                    "start_y_pages": 0.0,
                    "snapped_next_start_y_pages": 1.0,
                    "overflow_amount_pages": 0.0,
                    "overflow_violation": False,
                    "slot_span_count": 1,
                    "bbox": {"left": 0, "top": 0, "width": 120, "height": 80},
                    "risk_flags": [],
                    "record_mode": "image-only",
                    "text_record_count": 0,
                    "image_record_count": 1,
                },
                {
                    "problem_id": "kept-2",
                    "title": "2.",
                    "problem_number": 2,
                    "subject": "korean",
                    "crop_path": str(crop_2),
                    "source_path": str(source),
                    "source_page_id": "page-1",
                    "board_render_path": str(crop_2),
                    "actual_content_height_pages": 0.4,
                    "overflow_allowed": False,
                    "start_y_pages": 1.0,
                    "snapped_next_start_y_pages": 2.0,
                    "overflow_amount_pages": 0.0,
                    "overflow_violation": False,
                    "slot_span_count": 1,
                    "bbox": {"left": 0, "top": 100, "width": 120, "height": 80},
                    "risk_flags": [],
                    "record_mode": "image-only",
                    "text_record_count": 0,
                    "image_record_count": 1,
                },
                {
                    "problem_id": "kept-continuation",
                    "title": "이어지는 자료",
                    "problem_number": None,
                    "subject": "korean",
                    "crop_path": str(crop_3),
                    "source_path": str(source),
                    "source_page_id": "page-1",
                    "board_render_path": str(crop_3),
                    "actual_content_height_pages": 0.4,
                    "overflow_allowed": False,
                    "start_y_pages": 2.0,
                    "snapped_next_start_y_pages": 3.0,
                    "overflow_amount_pages": 0.0,
                    "overflow_violation": False,
                    "slot_span_count": 1,
                    "bbox": {"left": 0, "top": 200, "width": 120, "height": 80},
                    "risk_flags": ["marker_document_continuation"],
                    "record_mode": "image-only",
                    "text_record_count": 0,
                    "image_record_count": 1,
                },
            ]

            ui_session = build_problem_ui_session(
                [prepared],
                placements,
                root / "out",
                None,
                [source],
                record_mode="image-only",
                pages=[page],
                template=LayoutTemplate(name="academy-default"),
            )

            self.assertEqual(3, ui_session["detected_problem_count"])
            self.assertEqual(2, ui_session["core_problem_count"])
            self.assertEqual(1, ui_session["supplemental_item_count"])
            self.assertNotIn("hwp_problem_count_mismatch", ui_session["pages"][0]["riskFlags"])
            self.assertFalse([message for message in ui_session["warning_messages"] if "HWP 내부 텍스트" in message])

    def test_social_inquiry_problem_entries_allow_overflow_for_long_passages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "social.png"
            Image.new("RGB", (900, 1200), "white").save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.open(source).convert("RGB"),
                original_size=(900, 1200),
            )
            page = PageModel(
                page_id="page-1",
                width_px=900,
                height_px=1200,
                subject=Subject.SOCIAL,
                source_path=str(source),
                blocks=[
                    ContentBlock(
                        block_id="long-social-passage",
                        block_type=BlockType.STEM,
                        bbox=Box(left=40, top=60, width=760, height=1020),
                        reading_order=0,
                        text="1. 사회탐구 긴 지문",
                    ),
                ],
                problems=[
                    ProblemUnit(
                        unit_id="social-passage-1",
                        subject=Subject.SOCIAL,
                        title="1.",
                        stem_block_ids=["long-social-passage"],
                        metadata={"problem_number": 1},
                    ),
                ],
            )

            entries = build_problem_entries(
                [prepared],
                [page],
                root / "out",
                LayoutTemplate(name="academy-default"),
            )

            self.assertEqual(1, len(entries))
            self.assertTrue(entries[0].reading_heavy)
            self.assertTrue(entries[0].overflow_allowed)

    def test_edge_vertical_guides_are_trimmed_without_removing_internal_lines(self):
        image = Image.new("RGB", (120, 90), "white")
        draw = ImageDraw.Draw(image)
        draw.line((8, 0, 8, 89), fill=(170, 170, 170), width=2)
        draw.line((112, 0, 112, 89), fill=(170, 170, 170), width=2)
        draw.line((60, 10, 60, 80), fill="black", width=2)
        trimmed = _trim_edge_vertical_guides(image)

        self.assertLess(trimmed.width, image.width)
        self.assertEqual(trimmed.height, image.height)
        gray = trimmed.convert("L")
        internal_dark_columns = [
            x
            for x in range(10, trimmed.width - 10)
            if sum(1 for y in range(trimmed.height) if gray.getpixel((x, y)) < 80) >= 50
        ]
        self.assertTrue(internal_dark_columns)

    def test_passage_chrome_cleanup_preserves_lower_right_column_text(self):
        image = Image.new("RGB", (320, 240), "white")
        draw = ImageDraw.Draw(image)
        draw.text((24, 40), "shared passage", fill="black")
        draw.rectangle((292, 212, 319, 239), fill="black")

        cleaned = _trim_source_page_chrome(
            image,
            preserve_horizontal_bounds=True,
        )

        self.assertEqual(image.size, cleaned.size)
        self.assertEqual(
            image.crop((292, 212, 320, 240)).tobytes(),
            cleaned.crop((292, 212, 320, 240)).tobytes(),
        )

    def test_text_priority_page_badge_is_trimmed_below_real_content(self):
        image = Image.new("RGB", (500, 420), "white")
        draw = ImageDraw.Draw(image)
        draw.text((24, 60), "question", fill="black")
        for index, y in enumerate(range(140, 280, 28), start=1):
            draw.text((24, y), f"{index}. complete choice", fill="black")
        draw.line((0, 352, 64, 352), fill="black", width=2)
        draw.line((0, 378, 64, 378), fill="black", width=2)
        draw.line((64, 352, 64, 378), fill="black", width=2)
        draw.text((34, 358), "8", fill="black")

        trimmed = _trim_text_priority_bottom_page_badge(image)

        self.assertLess(trimmed.height, 352)
        self.assertGreater(trimmed.height, 280)
        self.assertLess(
            trimmed.convert("L").crop((20, 246, 180, 276)).getextrema()[0],
            200,
        )

    def test_tall_listening_page_badge_is_trimmed_after_choices(self):
        image = Image.new("RGB", (960, 236), "white")
        draw = ImageDraw.Draw(image)
        draw.text((18, 16), "6. listening prompt", fill="black")
        draw.text(
            (42, 90),
            "1  $14      2  $19      3  $24      4  $28      5  $33",
            fill="black",
        )
        draw.rectangle((866, 160, 959, 223), outline="black", width=2)
        draw.text((878, 172), "1", fill="black")
        padded = problem_board._pad_problem_crop_edges(
            image,
            left_padding_px=problem_board.TEXT_PRIORITY_CROP_HORIZONTAL_SAFE_PADDING_PX,
            right_padding_px=problem_board.TEXT_PRIORITY_CROP_HORIZONTAL_SAFE_PADDING_PX,
        )

        trimmed = _trim_text_priority_bottom_page_badge(padded)

        self.assertEqual(992, trimmed.width)
        self.assertGreater(trimmed.height, 140)
        self.assertLess(trimmed.height, 196)
        self.assertLess(
            trimmed.convert("L").crop((0, 100, 850, trimmed.height)).getextrema()[0],
            200,
        )

    def test_text_priority_lone_outer_guide_is_erased_without_cropping(self):
        image = Image.new("RGB", (500, 360), "white")
        draw = ImageDraw.Draw(image)
        draw.line((12, 24, 12, 335), fill="black", width=2)
        draw.text((48, 60), "16. complete question", fill="black")
        draw.text((48, 140), "1. complete choice", fill="black")

        cleaned = problem_board._erase_text_priority_unpaired_outer_vertical_guide(image)

        self.assertEqual(image.size, cleaned.size)
        self.assertEqual((255, 255, 255), cleaned.getpixel((12, 180)))
        self.assertLess(cleaned.convert("L").crop((40, 40, 260, 180)).getextrema()[0], 200)

    def test_trusted_pdf_asset_protection_skips_horizontal_chrome_cropping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = Image.new("RGB", (935, 600), "white")
            draw = ImageDraw.Draw(source)
            draw.line((92, 20, 92, 580), fill="black", width=3)
            draw.rectangle((160, 120, 670, 520), outline="black", width=3)
            draw.text((24, 48), "17. complete vocational problem", fill="black")
            task = problem_board._ProblemAssetTask(
                source_image=source,
                bounds=Box(left=0.0, top=0.0, width=935.0, height=600.0),
                crop_path=root / "trusted-pdf-crop.png",
                board_render_path=root / "trusted-pdf-stage2.png",
                chalk_color=(238, 238, 226),
                trim_edge_guides=True,
                protect_horizontal_bounds=True,
                pad_edges=True,
                subject=Subject.UNKNOWN,
            )

            with mock.patch.object(
                problem_board,
                "_trim_edge_vertical_guides",
                side_effect=AssertionError("trusted PDF bounds must not be cropped"),
            ), mock.patch.object(
                problem_board,
                "_trim_edge_attached_page_chrome",
                side_effect=AssertionError("trusted PDF content must keep its width"),
            ):
                self.assertEqual((967, 688), problem_board._render_problem_asset(task))

            with Image.open(task.crop_path) as crop:
                self.assertEqual((967, 688), crop.size)
                self.assertLess(
                    crop.convert("L").crop((160, 120, 700, 560)).getextrema()[0],
                    200,
                )

    def test_text_priority_paired_outer_frame_is_preserved(self):
        image = Image.new("RGB", (500, 360), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((12, 24, 487, 335), outline="black", width=2)
        draw.text((48, 60), "boxed content", fill="black")

        cleaned = problem_board._erase_text_priority_unpaired_outer_vertical_guide(image)

        self.assertEqual(image.tobytes(), cleaned.tobytes())

    def test_text_priority_final_choice_row_is_not_mistaken_for_page_badge(self):
        image = Image.new("RGB", (500, 420), "white")
        draw = ImageDraw.Draw(image)
        draw.text((24, 60), "question", fill="black")
        draw.text((0, 350), "5. final choice text extends across the row", fill="black")

        trimmed = _trim_text_priority_bottom_page_badge(image)

        self.assertEqual(image.size, trimmed.size)

    def test_slanted_edge_vertical_guides_are_trimmed(self):
        image = Image.new("RGB", (180, 180), "white")
        draw = ImageDraw.Draw(image)
        draw.text((24, 36), "1. problem", fill="black")
        draw.line((90, 24, 90, 154), fill="black", width=2)
        draw.line((156, 0, 148, 179), fill=(40, 40, 40), width=3)
        draw.text((164, 36), "4", fill="black")

        trimmed = _trim_edge_vertical_guides(image)

        self.assertLess(trimmed.width, 150)
        gray = trimmed.convert("L")
        right_band_dark_pixels = sum(
            1
            for x in range(max(0, trimmed.width - 8), trimmed.width)
            for y in range(trimmed.height)
            if gray.getpixel((x, y)) < 80
        )
        self.assertLess(right_band_dark_pixels, 20)
        internal_dark_columns = [
            x
            for x in range(50, trimmed.width - 20)
            if sum(1 for y in range(trimmed.height) if gray.getpixel((x, y)) < 80) >= 80
        ]
        self.assertTrue(internal_dark_columns)

    def test_problem_crop_bottom_padding_preserves_last_choice(self):
        image = Image.new("RGB", (120, 80), "white")
        draw = ImageDraw.Draw(image)
        draw.text((12, 64), "⑤", fill="black")

        padded = _pad_problem_crop_bottom(image, padding_px=18)

        self.assertEqual(padded.size, (120, 98))
        self.assertEqual(padded.getpixel((12, 96)), (255, 255, 255))

    def test_problem_crop_edge_padding_protects_top_and_bottom_content(self):
        image = Image.new("RGB", (120, 80), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((12, 0, 28, 4), fill="black")
        draw.rectangle((12, 76, 28, 79), fill="black")

        padded = _pad_problem_crop_edges(image, top_padding_px=10, bottom_padding_px=14)

        self.assertEqual(padded.size, (120, 104))
        self.assertEqual(padded.getpixel((12, 0)), (255, 255, 255))
        self.assertEqual(padded.getpixel((12, 9)), (255, 255, 255))
        self.assertEqual(padded.getpixel((12, 10)), (0, 0, 0))
        self.assertEqual(padded.getpixel((12, 89)), (0, 0, 0))
        self.assertEqual(padded.getpixel((12, 90)), (255, 255, 255))

    def test_document_band_problem_bounds_keep_top_and_bottom_margin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            Image.new("RGB", (600, 420), "white").save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.open(source).convert("RGB"),
                original_size=(600, 420),
            )
            blocks = [
                ContentBlock(
                    block_id="p1-stem",
                    block_type=BlockType.STEM,
                    bbox=Box(60, 100, 360, 60),
                    reading_order=0,
                    text="1. problem",
                    metadata={"column_index": 0, "question_band_index": 0},
                ),
            ]
            page = PageModel(
                page_id="page-1",
                width_px=600,
                height_px=420,
                subject=Subject.MATH,
                source_path=str(source),
                blocks=blocks,
                problems=[
                    ProblemUnit(
                        unit_id="problem-1",
                        subject=Subject.MATH,
                        title="1.",
                        stem_block_ids=["p1-stem"],
                        metadata={"problem_number": 1, "column_index": 0, "question_band_index": 0},
                    ),
                ],
            )

            entries = build_problem_entries(
                [prepared],
                [page],
                root / "out",
                LayoutTemplate(name="academy-default"),
            )

            self.assertEqual(1, len(entries))
            self.assertLessEqual(entries[0].bounds.top, 78)
            self.assertGreaterEqual(entries[0].bounds.bottom, 174)

    def test_pdf_passage_range_bounds_do_not_pull_in_page_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            page_image = Image.new("RGB", (600, 520), "white")
            draw = ImageDraw.Draw(page_image)
            draw.line((0, 70, 599, 70), fill="black", width=3)
            draw.text((24, 24), "PAGE HEADER", fill="black")
            draw.rectangle((40, 150, 440, 390), outline="black", width=2)
            draw.text((60, 174), "[1~2] shared passage", fill="black")
            page_image.save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=page_image,
                original_size=page_image.size,
            )
            passage_block = ContentBlock(
                block_id="passage",
                block_type=BlockType.IMAGE,
                bbox=Box(40, 150, 400, 240),
                reading_order=0,
                text="[1~2] shared passage",
                metadata={
                    "segmenter": "pdf-passage-range",
                    "column_index": 0,
                    "question_band_index": 0,
                },
            )
            page = PageModel(
                page_id="page-1",
                width_px=600,
                height_px=520,
                subject=Subject.ENGLISH,
                source_path=str(source),
                blocks=[passage_block],
                problems=[
                    ProblemUnit(
                        unit_id="passage-1-2",
                        subject=Subject.ENGLISH,
                        title="지문 1~2",
                        figure_block_ids=["passage"],
                        metadata={
                            "passage_role": "passage_fragment",
                            "supplemental_item": True,
                            "passage_range": {"start": 1, "end": 2},
                        },
                    )
                ],
            )

            entries = build_problem_entries(
                [prepared],
                [page],
                root / "out",
                LayoutTemplate(name="academy-default"),
            )

            self.assertEqual(1, len(entries))
            self.assertAlmostEqual(150.0, entries[0].bounds.top)
            with Image.open(entries[0].crop_path) as crop:
                self.assertGreater(crop.convert("L").crop((0, 0, crop.width, 24)).getextrema()[0], 180)

    def test_problem_bounds_expand_when_edge_ink_would_be_clipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            page_image = Image.new("RGB", (600, 440), "white")
            draw = ImageDraw.Draw(page_image)
            draw.rectangle((68, 68, 92, 72), fill="black")
            draw.rectangle((72, 300, 220, 303), fill="black")
            page_image.save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=page_image,
                original_size=(600, 440),
            )
            blocks = [
                ContentBlock(
                    block_id="p1-stem",
                    block_type=BlockType.STEM,
                    bbox=Box(60, 100, 360, 60),
                    reading_order=0,
                    text="1. problem",
                    metadata={"column_index": 0, "question_band_index": 0},
                ),
                ContentBlock(
                    block_id="p1-choice",
                    block_type=BlockType.CHOICE,
                    bbox=Box(72, 220, 330, 40),
                    reading_order=1,
                    text="⑤ choice",
                    metadata={"column_index": 0, "question_band_index": 0},
                ),
            ]
            page = PageModel(
                page_id="page-1",
                width_px=600,
                height_px=440,
                subject=Subject.MATH,
                source_path=str(source),
                blocks=blocks,
                problems=[
                    ProblemUnit(
                        unit_id="problem-1",
                        subject=Subject.MATH,
                        title="1.",
                        stem_block_ids=["p1-stem"],
                        choice_block_ids=["p1-choice"],
                        metadata={"problem_number": 1, "column_index": 0, "question_band_index": 0},
                    ),
                ],
            )

            entries = build_problem_entries(
                [prepared],
                [page],
                root / "out",
                LayoutTemplate(name="academy-default"),
            )

            self.assertEqual(1, len(entries))
            self.assertLessEqual(entries[0].bounds.top, 44)
            self.assertGreaterEqual(entries[0].bounds.bottom, 346)

    def test_footer_chrome_is_removed_from_last_problem_crop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            page_image = Image.new("RGB", (600, 520), "white")
            draw = ImageDraw.Draw(page_image)
            draw.text((72, 102), "27 problem", fill="black")
            draw.line((28, 480, 572, 480), fill="black", width=3)
            draw.text((360, 454), "윤자매 놀이학습(fillthevoid82.com)", fill="black")
            page_image.save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=page_image,
                original_size=(600, 520),
            )
            blocks = [
                ContentBlock(
                    block_id="p1-stem",
                    block_type=BlockType.STEM,
                    bbox=Box(60, 100, 360, 120),
                    reading_order=0,
                    text="27 problem",
                    metadata={"column_index": 0, "question_band_index": 0},
                ),
                ContentBlock(
                    block_id="footer-watermark",
                    block_type=BlockType.NOTE,
                    bbox=Box(350, 450, 220, 24),
                    reading_order=1,
                    text="윤자매 놀이학습(fillthevoid82.com)",
                    metadata={"column_index": 0, "question_band_index": 0},
                ),
            ]
            page = PageModel(
                page_id="page-1",
                width_px=600,
                height_px=520,
                subject=Subject.MATH,
                source_path=str(source),
                blocks=blocks,
                problems=[
                    ProblemUnit(
                        unit_id="problem-1",
                        subject=Subject.MATH,
                        title="27",
                        stem_block_ids=["p1-stem"],
                        metadata={
                            "problem_number": 27,
                            "column_index": 0,
                            "question_band_index": 0,
                            "grouping_source": "ai_fallback",
                            "bbox_px": {"left": 40, "top": 80, "width": 520, "height": 420},
                        },
                    ),
                ],
            )

            entries = build_problem_entries(
                [prepared],
                [page],
                root / "out",
                LayoutTemplate(name="academy-default"),
            )

            self.assertEqual(1, len(entries))
            self.assertLess(entries[0].bounds.bottom, 450)
            self.assertGreater(entries[0].bounds.bottom, 230)
            self.assertNotIn("footer-watermark", {block.block_id for block in entries[0].blocks})

    def test_choice_bottom_survives_near_next_problem_clamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            Image.new("RGB", (600, 420), "white").save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=Image.open(source).convert("RGB"),
                original_size=(600, 420),
            )
            blocks = [
                ContentBlock(
                    block_id="p1-stem",
                    block_type=BlockType.STEM,
                    bbox=Box(60, 100, 360, 60),
                    reading_order=0,
                    text="1. problem",
                    metadata={"column_index": 0, "question_band_index": 0},
                ),
                ContentBlock(
                    block_id="p1-choice",
                    block_type=BlockType.CHOICE,
                    bbox=Box(72, 192, 330, 28),
                    reading_order=1,
                    text="⑤ choice",
                    metadata={"column_index": 0, "question_band_index": 0},
                ),
                ContentBlock(
                    block_id="p2-stem",
                    block_type=BlockType.STEM,
                    bbox=Box(60, 225, 360, 55),
                    reading_order=2,
                    text="2. next",
                    metadata={"column_index": 0, "question_band_index": 1},
                ),
            ]
            page = PageModel(
                page_id="page-1",
                width_px=600,
                height_px=420,
                subject=Subject.MATH,
                source_path=str(source),
                blocks=blocks,
                problems=[
                    ProblemUnit(
                        unit_id="problem-1",
                        subject=Subject.MATH,
                        title="1.",
                        stem_block_ids=["p1-stem"],
                        choice_block_ids=["p1-choice"],
                        metadata={"problem_number": 1, "column_index": 0, "question_band_index": 0},
                    ),
                    ProblemUnit(
                        unit_id="problem-2",
                        subject=Subject.MATH,
                        title="2.",
                        stem_block_ids=["p2-stem"],
                        metadata={"problem_number": 2, "column_index": 0, "question_band_index": 1},
                    ),
                ],
            )

            entries = build_problem_entries(
                [prepared],
                [page],
                root / "out",
                LayoutTemplate(name="academy-default"),
            )

            problem_1 = next(entry for entry in entries if entry.problem_number == 1)
            self.assertGreaterEqual(problem_1.bounds.bottom, 248)

    def test_session_source_images_point_to_rendered_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            self._make_source_image(source)

            result = run_problem_export(
                source,
                output_dir=root / "out",
                input_intent="single-problem",
                ocr="noop",
                record_mode="image-only",
                export_edb=False,
            )

            session = result["ui_session"]
            page_source = Path(session["pages"][0]["sourceImagePath"])
            problem_source = _path_from_file_uri(session["problems"][0]["sourceImagePath"])
            self.assertEqual(page_source.suffix.lower(), ".png")
            self.assertEqual(problem_source.suffix.lower(), ".png")
            self.assertTrue(page_source.exists())
            self.assertTrue(problem_source.exists())

    def test_page_as_is_full_page_crop_preserves_source_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "page.png"
            image = Image.new("RGB", (180, 140), "white")
            draw = ImageDraw.Draw(image)
            draw.text((12, 28), "page as is", fill="black")
            draw.line((179, 0, 179, 139), fill="black", width=1)
            image.save(source)
            prepared = PreparedPage(
                page_id="page-1",
                source_path=str(source),
                page_number=1,
                image=image,
                original_size=image.size,
            )
            page = PageModel(
                page_id="page-1",
                width_px=image.width,
                height_px=image.height,
                subject=Subject.UNKNOWN,
                source_path=str(source),
                blocks=[],
                problems=[
                    ProblemUnit(
                        unit_id="page-1-problem-1",
                        subject=Subject.UNKNOWN,
                        title="페이지 1",
                        metadata={
                            "force_full_page_bounds": True,
                            "input_intent": "page-as-is",
                        },
                    ),
                ],
                metadata={"input_intent": "page-as-is"},
            )

            entries = build_problem_entries(
                [prepared],
                [page],
                root / "out",
                LayoutTemplate(name="academy-default"),
            )

            crop = Image.open(entries[0].crop_path).convert("RGB")
            self.assertEqual(source.resolve(), entries[0].crop_path.resolve())
            self.assertEqual(source.resolve(), entries[0].board_render_path.resolve())
            self.assertEqual(image.size, crop.size)
            self.assertEqual((0, 0, 0), crop.getpixel((179, 70)))

    def test_page_as_is_export_skips_segmentation_and_ocr_page_model_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "page.png"
            image = Image.new("RGB", (180, 140), "white")
            draw = ImageDraw.Draw(image)
            draw.text((12, 28), "page as is", fill="black")
            image.save(source)

            with mock.patch.object(
                problem_board,
                "build_page_models_for_prepared_pages",
                side_effect=AssertionError("page-as-is should not build OCR page models"),
            ), mock.patch.object(
                problem_board,
                "_encode_image_bytes",
                side_effect=AssertionError("page-as-is preview should not encode EDB records"),
            ):
                result = run_problem_export(
                    source,
                    output_dir=root / "out",
                    input_intent="page-as-is",
                    ocr="auto",
                    record_mode="image-only",
                    export_edb=False,
                    skip_deskew=True,
                    skip_crop=True,
                    max_dimension=2400,
                )

            page = result["ui_session"]["pages"][0]
            self.assertIs(True, page["pageAsIsFastPath"])
            self.assertIs(True, page["segmentationSkipped"])
            self.assertIs(True, page["ocrSkipped"])
            self.assertEqual(1, len(result["ui_session"]["problems"]))
            problem = result["ui_session"]["problems"][0]
            self.assertIs(True, problem["forceFullPageBounds"])
            self.assertEqual("page-as-is", problem["inputIntent"])
            self.assertEqual({"left": 0.0, "top": 0.0, "width": 180.0, "height": 140.0}, problem["bbox"])


if __name__ == "__main__":
    unittest.main()
