from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image

import preprocess


class TestPreprocessHwp(unittest.TestCase):
    def test_extract_hwp_numbered_problem_snippets_keeps_question_and_choices(self) -> None:
        text = (
            "[32 ~ 34] 다음 글을 읽고 물음에 답하시오.\n"
            "(가) 긴 지문입니다.\n"
            "32.(가), (나)의 표현상 특징에 대한 설명으로 가장 적절한 것은?\n"
            "① 첫 번째 선택지\n"
            "② 두 번째 선택지\n"
            "⑤ 다섯 번째 선택지\n"
            "33. [A]～[C]에 대한 이해로 적절하지 않은 것은?\n"
            "① 다음 문제 선택지\n"
        )

        snippets = preprocess._extract_hwp_numbered_problem_snippets(text)

        by_number = {item["number"]: item["text"] for item in snippets}
        self.assertIn(32, by_number)
        self.assertIn("(가), (나)의 표현상 특징", by_number[32])
        self.assertIn("⑤ 다섯 번째 선택지", by_number[32])
        self.assertNotIn("33.", by_number[32])

    def test_extract_hwp_passage_ranges_finds_ranges_beyond_preview_limit(self) -> None:
        text = (
            ("머리말\n" * 900)
            + "[35～37] 다음 자료를 보고 물음에 답하시오.\n"
            + "35. 첫 번째 문항\n"
            + "40번부터 41번까지는 다음 글을 읽고 물음에 답하시오.\n"
            + "40. 두 번째 묶음\n"
            + "[42~45] 선택 과목 안내\n"
        )

        ranges = preprocess._extract_hwp_passage_ranges(text)

        self.assertEqual(
            [
                {"start": 35, "end": 37, "text": "[35~37] 다음 자료를 보고 물음에 답하시오."},
                {"start": 40, "end": 41, "text": "40번부터 41번까지는 다음 글을 읽고 물음에 답하시오."},
            ],
            ranges,
        )

    def test_extract_hwp_passage_ranges_accepts_compact_english_and_korean_ranges(self) -> None:
        text = (
            "Questions 18-21 refer to the following passage.\n"
            "18. first question\n"
            "18-21번은 다음 글을 읽고 물음에 답하시오.\n"
            "문항 24~26은 다음 자료를 보고 물음에 답하시오.\n"
        )

        ranges = preprocess._extract_hwp_passage_ranges(text)

        self.assertEqual(
            [
                {"start": 18, "end": 21, "text": "Questions 18-21 refer to the following passage."},
                {"start": 24, "end": 26, "text": "문항 24~26은 다음 자료를 보고 물음에 답하시오."},
            ],
            ranges,
        )

    def test_hwp_normalized_cache_rejects_pre_snippet_cache_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "worksheet.hwp"
            source.write_bytes(b"hwp")
            normalized = root / "worksheet-page-001.png"
            Image.new("RGB", (120, 160), "white").save(normalized)
            source_sha1 = preprocess._file_sha1(source)
            cache_path = preprocess._hwp_normalized_pages_cache_path(root, source, source_sha1)
            cache_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "source_name": source.name,
                        "source_suffix": ".hwp",
                        "source_sha1": source_sha1,
                        "options": preprocess._hwp_normalized_cache_options(
                            dpi=200,
                            enable_deskew=True,
                            enable_margin_crop=True,
                            max_dimension=None,
                        ),
                        "pages": [
                            {
                                "page_id": "worksheet-page-001",
                                "normalized_name": normalized.name,
                                "page_index": 0,
                                "width_px": 120,
                                "height_px": 160,
                                "metadata": {"source_type": "hwp"},
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                [],
                preprocess._load_cached_hwp_normalized_pages(
                    source,
                    root,
                    dpi=200,
                    enable_deskew=True,
                    enable_margin_crop=True,
                    max_dimension=None,
                ),
            )

    def test_hwp_normalized_cache_rejects_pre_core_priority_cache_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "worksheet.hwp"
            source.write_bytes(b"hwp")
            normalized = root / "worksheet-page-001.png"
            Image.new("RGB", (120, 160), "white").save(normalized)
            source_sha1 = preprocess._file_sha1(source)
            cache_path = preprocess._hwp_normalized_pages_cache_path(root, source, source_sha1)
            cache_path.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "source_name": source.name,
                        "source_suffix": ".hwp",
                        "source_sha1": source_sha1,
                        "options": preprocess._hwp_normalized_cache_options(
                            dpi=200,
                            enable_deskew=True,
                            enable_margin_crop=True,
                            max_dimension=None,
                        ),
                        "pages": [
                            {
                                "page_id": "worksheet-page-001",
                                "normalized_name": normalized.name,
                                "page_index": 0,
                                "width_px": 120,
                                "height_px": 160,
                                "metadata": {"source_type": "hwp", "hwp_renderer": "rhwp-python"},
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                [],
                preprocess._load_cached_hwp_normalized_pages(
                    source,
                    root,
                    dpi=200,
                    enable_deskew=True,
                    enable_margin_crop=True,
                    max_dimension=None,
                ),
            )

    def test_hwp_normalized_cache_rejects_pre_passage_range_cache_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "worksheet.hwp"
            source.write_bytes(b"hwp")
            normalized = root / "worksheet-page-001.png"
            Image.new("RGB", (120, 160), "white").save(normalized)
            source_sha1 = preprocess._file_sha1(source)
            cache_path = preprocess._hwp_normalized_pages_cache_path(root, source, source_sha1)
            cache_path.write_text(
                json.dumps(
                    {
                        "version": 4,
                        "source_name": source.name,
                        "source_suffix": ".hwp",
                        "source_sha1": source_sha1,
                        "options": preprocess._hwp_normalized_cache_options(
                            dpi=200,
                            enable_deskew=True,
                            enable_margin_crop=True,
                            max_dimension=None,
                        ),
                        "pages": [
                            {
                                "page_id": "worksheet-page-001",
                                "normalized_name": normalized.name,
                                "page_index": 0,
                                "width_px": 120,
                                "height_px": 160,
                                "metadata": {"source_type": "hwp", "hwp_renderer": "rhwp-core"},
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                [],
                preprocess._load_cached_hwp_normalized_pages(
                    source,
                    root,
                    dpi=200,
                    enable_deskew=True,
                    enable_margin_crop=True,
                    max_dimension=None,
                ),
            )

    def test_hwp_layout_markers_convert_to_rendered_page_marker_coordinates(self) -> None:
        quality = {
            "hwp_layout_problem_markers": [
                {
                    "pageIndex": 0,
                    "number": 7,
                    "text": "7. 윗글에 대한 이해로 적절하지 않은 것은?",
                    "bbox": {"x": 72, "y": 100.5, "w": 18, "h": 9.5},
                },
                {
                    "pageIndex": 1,
                    "number": 8,
                    "text": "8. 다른 페이지 문제",
                    "bbox": {"x": 40, "y": 60, "w": 10, "h": 8},
                },
            ]
        }

        markers = preprocess._hwp_layout_problem_markers_for_page(
            quality,
            page_index=0,
            dpi=144,
        )

        self.assertEqual(1, len(markers))
        self.assertEqual(7, markers[0]["number"])
        self.assertEqual("hwp_layout_number", markers[0]["marker_kind"])
        self.assertEqual("hwp_layout_marker", markers[0]["source"])
        self.assertEqual(
            {"left": 144.0, "top": 201.0, "right": 180.0, "bottom": 220.0, "width": 36.0, "height": 19.0},
            markers[0]["bbox"],
        )

    def test_inspect_hwp_document_prefers_pyhwp_text_preview_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            pyhwp_text = (
                "1. 윗글의 내용과 일치하지 않은 것은?\n"
                "다음 자료에 대한 설명으로 옳은 것은?\n"
                "Ctrl+3 누르면 지시문 생성됩니다."
            )
            header = (
                b"HWP Document File".ljust(32, b"\0")
                + (0).to_bytes(4, "little")
                + (1).to_bytes(4, "little")
                + b"\0" * 216
            )

            class FakeOle:
                def listdir(self, streams=True, storages=False):
                    return [["BodyText", "Section0"]]

                def exists(self, name):
                    return name in {"FileHeader", "PrvText"}

                def openstream(self, name):
                    if name == "FileHeader":
                        return io.BytesIO(header)
                    return io.BytesIO("짧은 미리보기".encode("utf-16le"))

                def close(self):
                    pass

            with (
                mock.patch.object(preprocess.olefile, "isOleFile", return_value=True),
                mock.patch.object(preprocess.olefile, "OleFileIO", return_value=FakeOle()),
                mock.patch.object(preprocess, "_extract_hwp_text_with_pyhwp", return_value=pyhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_unhwp", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwpilot", return_value="", create=True),
            ):
                inspection = preprocess.inspect_hwp_document(source)

            self.assertEqual(pyhwp_text, inspection["hwp_preview_text"])
            self.assertEqual(len(pyhwp_text), inspection["hwp_preview_text_length"])
            self.assertEqual("hwp5txt", inspection["hwp_text_extractor"])
            self.assertEqual(1, inspection["hwp_text_numbered_problem_count"])
            self.assertEqual(1, inspection["hwp_text_stem_problem_count"])

    def test_inspect_hwp_document_counts_hwp_text_signals_beyond_preview_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            pyhwp_text = (
                "1. 윗글의 내용과 일치하지 않은 것은?\n"
                + ("본문입니다.\n" * 700)
                + "45. 윗글의 내용 전개 방식으로 가장 적절한 것은?\n"
                + "다음 자료에 대한 설명으로 옳은 것은?"
            )
            header = (
                b"HWP Document File".ljust(32, b"\0")
                + (0).to_bytes(4, "little")
                + (1).to_bytes(4, "little")
                + b"\0" * 216
            )

            class FakeOle:
                def listdir(self, streams=True, storages=False):
                    return [["BodyText", "Section0"]]

                def exists(self, name):
                    return name == "FileHeader"

                def openstream(self, name):
                    return io.BytesIO(header)

                def close(self):
                    pass

            with (
                mock.patch.object(preprocess.olefile, "isOleFile", return_value=True),
                mock.patch.object(preprocess.olefile, "OleFileIO", return_value=FakeOle()),
                mock.patch.object(preprocess, "_extract_hwp_text_with_pyhwp", return_value=pyhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_unhwp", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwpilot", return_value="", create=True),
            ):
                inspection = preprocess.inspect_hwp_document(source)

            self.assertLessEqual(len(inspection["hwp_preview_text"]), 4000)
            self.assertEqual(2, inspection["hwp_text_numbered_problem_count"])
            self.assertEqual(1, inspection["hwp_text_stem_problem_count"])

    def test_inspect_hwp_document_can_use_unhwp_text_when_longer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            pyhwp_text = "짧은 텍스트"
            unhwp_text = (
                "1. 윗글의 내용과 일치하지 않은 것은?\n"
                + ("본문입니다.\n" * 20)
                + "2. ㉠에 대한 설명으로 가장 적절한 것은?"
            )
            header = (
                b"HWP Document File".ljust(32, b"\0")
                + (0).to_bytes(4, "little")
                + (1).to_bytes(4, "little")
                + b"\0" * 216
            )

            class FakeOle:
                def listdir(self, streams=True, storages=False):
                    return [["BodyText", "Section0"]]

                def exists(self, name):
                    return name == "FileHeader"

                def openstream(self, name):
                    return io.BytesIO(header)

                def close(self):
                    pass

            with (
                mock.patch.object(preprocess.olefile, "isOleFile", return_value=True),
                mock.patch.object(preprocess.olefile, "OleFileIO", return_value=FakeOle()),
                mock.patch.object(preprocess, "_extract_hwp_text_with_pyhwp", return_value=pyhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_unhwp", return_value=unhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwpilot", return_value="", create=True),
            ):
                inspection = preprocess.inspect_hwp_document(source)

            self.assertEqual("unhwp", inspection["hwp_text_extractor"])
            self.assertEqual(2, inspection["hwp_text_numbered_problem_count"])

    def test_inspect_hwp_document_prefers_stronger_problem_signal_over_longer_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            pyhwp_text = "\n".join(f"{number}. 문제" for number in range(1, 21))
            unhwp_text = "\n".join(["본문입니다."] * 300 + ["1. 문제", "2. 문제", "3. 문제"])
            header = (
                b"HWP Document File".ljust(32, b"\0")
                + (0).to_bytes(4, "little")
                + (1).to_bytes(4, "little")
                + b"\0" * 216
            )

            class FakeOle:
                def listdir(self, streams=True, storages=False):
                    return [["BodyText", "Section0"]]

                def exists(self, name):
                    return name == "FileHeader"

                def openstream(self, name):
                    return io.BytesIO(header)

                def close(self):
                    pass

            with (
                mock.patch.object(preprocess.olefile, "isOleFile", return_value=True),
                mock.patch.object(preprocess.olefile, "OleFileIO", return_value=FakeOle()),
                mock.patch.object(preprocess, "_extract_hwp_text_with_pyhwp", return_value=pyhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_unhwp", return_value=unhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwpilot", return_value="", create=True),
            ):
                inspection = preprocess.inspect_hwp_document(source)

            self.assertEqual("hwp5txt", inspection["hwp_text_extractor"])
            self.assertEqual(20, inspection["hwp_text_numbered_problem_count"])

    def test_inspect_hwp_document_can_use_rhwp_text_when_signal_is_stronger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            pyhwp_text = "본문입니다."
            unhwp_text = "1. 문제"
            rhwp_text = "\n".join(["1. 문제", "2. 문제", "3. 문제", "4. 문제"])
            header = (
                b"HWP Document File".ljust(32, b"\0")
                + (0).to_bytes(4, "little")
                + (1).to_bytes(4, "little")
                + b"\0" * 216
            )

            class FakeOle:
                def listdir(self, streams=True, storages=False):
                    return [["BodyText", "Section0"]]

                def exists(self, name):
                    return name == "FileHeader"

                def openstream(self, name):
                    return io.BytesIO(header)

                def close(self):
                    pass

            with (
                mock.patch.object(preprocess.olefile, "isOleFile", return_value=True),
                mock.patch.object(preprocess.olefile, "OleFileIO", return_value=FakeOle()),
                mock.patch.object(preprocess, "_extract_hwp_text_with_pyhwp", return_value=pyhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_unhwp", return_value=unhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_rhwp", return_value=rhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwpilot", return_value="", create=True),
            ):
                inspection = preprocess.inspect_hwp_document(source)

            self.assertEqual("rhwp", inspection["hwp_text_extractor"])
            self.assertEqual(4, inspection["hwp_text_numbered_problem_count"])

    def test_inspect_hwp_document_can_use_rhwp_markdown_when_signal_is_stronger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            pyhwp_text = "본문입니다."
            unhwp_text = "1. 문제"
            rhwp_text = "1. 문제"
            rhwp_markdown = "\n".join(["1. 문제", "2. 문제", "3. 문제", "![image 1](asset.png)"])
            header = (
                b"HWP Document File".ljust(32, b"\0")
                + (0).to_bytes(4, "little")
                + (1).to_bytes(4, "little")
                + b"\0" * 216
            )

            class FakeOle:
                def listdir(self, streams=True, storages=False):
                    return [["BodyText", "Section0"]]

                def exists(self, name):
                    return name == "FileHeader"

                def openstream(self, name):
                    return io.BytesIO(header)

                def close(self):
                    pass

            with (
                mock.patch.object(preprocess.olefile, "isOleFile", return_value=True),
                mock.patch.object(preprocess.olefile, "OleFileIO", return_value=FakeOle()),
                mock.patch.object(preprocess, "_extract_hwp_text_with_pyhwp", return_value=pyhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_unhwp", return_value=unhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_rhwp", return_value=rhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_markdown_with_rhwp", return_value=rhwp_markdown, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwpilot", return_value="", create=True),
            ):
                inspection = preprocess.inspect_hwp_document(source)

            self.assertEqual("rhwp-markdown", inspection["hwp_text_extractor"])
            self.assertEqual(3, inspection["hwp_text_numbered_problem_count"])

    def test_inspect_hwp_document_can_use_hwpilot_text_when_signal_is_stronger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            pyhwp_text = "본문입니다."
            unhwp_text = "1. 문제"
            rhwp_text = "1. 문제"
            rhwp_markdown = "1. 문제"
            hwpilot_text = "\n".join(f"{number}. 문제" for number in range(1, 6))
            header = (
                b"HWP Document File".ljust(32, b"\0")
                + (0).to_bytes(4, "little")
                + (1).to_bytes(4, "little")
                + b"\0" * 216
            )

            class FakeOle:
                def listdir(self, streams=True, storages=False):
                    return [["BodyText", "Section0"]]

                def exists(self, name):
                    return name == "FileHeader"

                def openstream(self, name):
                    return io.BytesIO(header)

                def close(self):
                    pass

            with (
                mock.patch.object(preprocess.olefile, "isOleFile", return_value=True),
                mock.patch.object(preprocess.olefile, "OleFileIO", return_value=FakeOle()),
                mock.patch.object(preprocess, "_extract_hwp_text_with_pyhwp", return_value=pyhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_unhwp", return_value=unhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_rhwp", return_value=rhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_markdown_with_rhwp", return_value=rhwp_markdown, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwpilot", return_value=hwpilot_text, create=True),
            ):
                inspection = preprocess.inspect_hwp_document(source)

            self.assertEqual("hwpilot", inspection["hwp_text_extractor"])
            self.assertEqual(5, inspection["hwp_text_numbered_problem_count"])

    def test_inspect_hwp_document_can_use_hwp_hwpx_parser_when_signal_is_stronger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            pyhwp_text = "본문입니다."
            unhwp_text = "1. 문제"
            rhwp_text = "\n".join(f"{number}. 선택지처럼 보이는 줄" for number in range(1, 32))
            hwp_hwpx_parser_text = "\n".join("다음 자료에 대한 설명으로 옳은 것은?" for _ in range(20))
            header = (
                b"HWP Document File".ljust(32, b"\0")
                + (0).to_bytes(4, "little")
                + (1).to_bytes(4, "little")
                + b"\0" * 216
            )

            class FakeOle:
                def listdir(self, streams=True, storages=False):
                    return [["BodyText", "Section0"]]

                def exists(self, name):
                    return name == "FileHeader"

                def openstream(self, name):
                    return io.BytesIO(header)

                def close(self):
                    pass

            with (
                mock.patch.object(preprocess.olefile, "isOleFile", return_value=True),
                mock.patch.object(preprocess.olefile, "OleFileIO", return_value=FakeOle()),
                mock.patch.object(preprocess, "_extract_hwp_text_with_pyhwp", return_value=pyhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_unhwp", return_value=unhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_rhwp", return_value=rhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_markdown_with_rhwp", return_value="1. 문제", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwpilot", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_kordoc", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwp_hwpx_parser", return_value=hwp_hwpx_parser_text, create=True),
            ):
                inspection = preprocess.inspect_hwp_document(source)

            self.assertEqual("hwp-hwpx-parser", inspection["hwp_text_extractor"])
            self.assertEqual(20, inspection["hwp_text_stem_problem_count"])

    def test_extract_hwp_text_with_rhwp_python_reads_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "worksheet.hwp"
            source.write_bytes(b"hwp")

            def fake_run(command, **kwargs):
                self.assertIn(str(source), command)
                return subprocess.CompletedProcess(command, 0, "1. 문제\n2. 문제\x00", "")

            with (
                mock.patch.object(preprocess, "_iter_rhwp_python_text_converter_commands", return_value=[["/venv/bin/python", "-c", "script"]]),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                text = preprocess._extract_hwp_text_with_rhwp_python(source)

            self.assertEqual("1. 문제\n2. 문제", text)

    def test_inspect_hwp_document_can_use_rhwp_python_when_signal_is_stronger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            pyhwp_text = "본문입니다."
            rhwp_python_text = "\n".join(f"{number}. 문제" for number in range(1, 7))
            header = (
                b"HWP Document File".ljust(32, b"\0")
                + (0).to_bytes(4, "little")
                + (1).to_bytes(4, "little")
                + b"\0" * 216
            )

            class FakeOle:
                def listdir(self, streams=True, storages=False):
                    return [["BodyText", "Section0"]]

                def exists(self, name):
                    return name == "FileHeader"

                def openstream(self, name):
                    return io.BytesIO(header)

                def close(self):
                    pass

            with (
                mock.patch.object(preprocess.olefile, "isOleFile", return_value=True),
                mock.patch.object(preprocess.olefile, "OleFileIO", return_value=FakeOle()),
                mock.patch.object(preprocess, "_extract_hwp_text_with_pyhwp", return_value=pyhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_unhwp", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwp_hwpx_parser", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_rhwp_python", return_value=rhwp_python_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_rhwp", return_value="1. 문제", create=True),
                mock.patch.object(preprocess, "_extract_hwp_markdown_with_rhwp", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwpilot", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_kordoc", return_value="", create=True),
            ):
                inspection = preprocess.inspect_hwp_document(source)

            self.assertEqual("rhwp-python", inspection["hwp_text_extractor"])
            self.assertEqual(6, inspection["hwp_text_numbered_problem_count"])

    def test_inspect_hwp_document_skips_slow_extractors_when_fast_signal_is_strong(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            rhwp_python_text = "\n".join(f"{number}. 문제" for number in range(1, 21))
            header = (
                b"HWP Document File".ljust(32, b"\0")
                + (0).to_bytes(4, "little")
                + (1).to_bytes(4, "little")
                + b"\0" * 216
            )

            class FakeOle:
                def listdir(self, streams=True, storages=False):
                    return [["BodyText", "Section0"]]

                def exists(self, name):
                    return name == "FileHeader"

                def openstream(self, name):
                    return io.BytesIO(header)

                def close(self):
                    pass

            slow_call = AssertionError("slow extractor should be skipped")
            with (
                mock.patch.object(preprocess.olefile, "isOleFile", return_value=True),
                mock.patch.object(preprocess.olefile, "OleFileIO", return_value=FakeOle()),
                mock.patch.object(preprocess, "_extract_hwp_text_with_pyhwp", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_unhwp", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwp_hwpx_parser", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_rhwp_python", return_value=rhwp_python_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_rhwp", side_effect=slow_call, create=True),
                mock.patch.object(preprocess, "_extract_hwp_markdown_with_rhwp", side_effect=slow_call, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwpilot", side_effect=slow_call, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_kordoc", side_effect=slow_call, create=True),
            ):
                inspection = preprocess.inspect_hwp_document(source)

            self.assertEqual("rhwp-python", inspection["hwp_text_extractor"])
            self.assertEqual(20, inspection["hwp_text_numbered_problem_count"])

    def test_extract_hwp_markdown_with_rhwp_reads_exported_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")

            def fake_run(command, **kwargs):
                output_dir = Path(command[command.index("-o") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "worksheet_001.md").write_text("1. 문제\n![image](asset.png)", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch.object(preprocess, "_iter_rhwp_converter_commands", return_value=[["/usr/local/bin/rhwp"]]),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                text = preprocess._extract_hwp_markdown_with_rhwp(source)

            self.assertEqual("1. 문제\n![image](asset.png)", text)

    def test_extract_hwp_render_tree_summary_with_rhwp_reads_numbered_text_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")

            def fake_run(command, **kwargs):
                output_dir = Path(command[command.index("-o") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "render_tree_001.json").write_text(
                    json.dumps(
                        {
                            "type": "Page",
                            "bbox": {"x": 0, "y": 0, "w": 100, "h": 200},
                            "children": [
                                {
                                    "type": "TextLine",
                                    "bbox": {"x": 10, "y": 20, "w": 80, "h": 10},
                                    "children": [
                                        {"type": "TextRun", "text": "1", "bbox": {"x": 10, "y": 20, "w": 5, "h": 10}},
                                        {"type": "TextRun", "text": ". 다음 글의", "bbox": {"x": 15, "y": 20, "w": 40, "h": 10}},
                                    ],
                                },
                                {
                                    "type": "TextLine",
                                    "bbox": {"x": 10, "y": 40, "w": 80, "h": 10},
                                    "children": [{"type": "TextRun", "text": "① 선지", "bbox": {"x": 10, "y": 40, "w": 30, "h": 10}}],
                                },
                                {
                                    "type": "TextLine",
                                    "bbox": {"x": 10, "y": 60, "w": 80, "h": 10},
                                    "children": [{"type": "TextRun", "text": "2. ㉠에 대한 설명", "bbox": {"x": 10, "y": 60, "w": 50, "h": 10}}],
                                },
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            extractor = getattr(preprocess, "_extract_hwp_render_tree_summary_with_rhwp", None)
            self.assertIsNotNone(extractor)
            with (
                mock.patch.object(preprocess, "_iter_rhwp_converter_commands", return_value=[["/usr/local/bin/rhwp"]]),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                summary = extractor(source)

            self.assertEqual("rhwp-render-tree", summary.get("hwp_layout_extractor"))
            self.assertEqual(1, summary.get("hwp_layout_page_count"))
            self.assertEqual(2, summary.get("hwp_layout_problem_marker_count"))
            self.assertEqual(3, summary.get("hwp_layout_text_line_count"))
            self.assertEqual([1, 2], summary.get("hwp_layout_problem_numbers"))

    def test_inspect_hwp_document_includes_rhwp_render_tree_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            header = (
                b"HWP Document File".ljust(32, b"\0")
                + (0).to_bytes(4, "little")
                + (1).to_bytes(4, "little")
                + b"\0" * 216
            )

            class FakeOle:
                def listdir(self, streams=True, storages=False):
                    return [["BodyText", "Section0"]]

                def exists(self, name):
                    return name == "FileHeader"

                def openstream(self, name):
                    return io.BytesIO(header)

                def close(self):
                    pass

            with (
                mock.patch.object(preprocess.olefile, "isOleFile", return_value=True),
                mock.patch.object(preprocess.olefile, "OleFileIO", return_value=FakeOle()),
                mock.patch.object(preprocess, "_extract_hwp_text_with_pyhwp", return_value="1. 문제", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_unhwp", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_rhwp", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_markdown_with_rhwp", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwpilot", return_value="", create=True),
                mock.patch.object(
                    preprocess,
                    "_extract_hwp_render_tree_summary_with_rhwp",
                    return_value={
                        "hwp_layout_extractor": "rhwp-render-tree",
                        "hwp_layout_page_count": 1,
                        "hwp_layout_problem_marker_count": 1,
                        "hwp_layout_text_line_count": 1,
                        "hwp_layout_problem_numbers": [1],
                    },
                    create=True,
                ),
            ):
                inspection = preprocess.inspect_hwp_document(source)

            self.assertEqual("rhwp-render-tree", inspection.get("hwp_layout_extractor"))
            self.assertEqual(1, inspection.get("hwp_layout_problem_marker_count"))

    def test_inspect_hwp_document_includes_hwpilot_image_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            header = (
                b"HWP Document File".ljust(32, b"\0")
                + (0).to_bytes(4, "little")
                + (1).to_bytes(4, "little")
                + b"\0" * 216
            )

            class FakeOle:
                def listdir(self, streams=True, storages=False):
                    return [["BodyText", "Section0"]]

                def exists(self, name):
                    return name == "FileHeader"

                def openstream(self, name):
                    return io.BytesIO(header)

                def close(self):
                    pass

            with (
                mock.patch.object(preprocess.olefile, "isOleFile", return_value=True),
                mock.patch.object(preprocess.olefile, "OleFileIO", return_value=FakeOle()),
                mock.patch.object(preprocess, "_extract_hwp_text_with_pyhwp", return_value="1. 문제", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_unhwp", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_rhwp", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_markdown_with_rhwp", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwpilot", return_value="", create=True),
                mock.patch.object(preprocess, "_extract_hwp_render_tree_summary_with_rhwp", return_value={}, create=True),
                mock.patch.object(
                    preprocess,
                    "_extract_hwp_image_summary_with_hwpilot",
                    return_value={
                        "hwp_image_extractor": "hwpilot",
                        "hwp_image_count": 2,
                    },
                    create=True,
                ),
            ):
                inspection = preprocess.inspect_hwp_document(source)

            self.assertEqual("hwpilot", inspection.get("hwp_image_extractor"))
            self.assertEqual(2, inspection.get("hwp_image_count"))

    def test_inspect_hwp_document_ignores_rhwp_numbered_spike_when_existing_signal_is_strong(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            pyhwp_text = "\n".join(["다음 자료에 대한 설명으로 옳은 것은?"] * 20)
            unhwp_text = "\n".join(["1. 문제", "2. 문제", "3. 문제"])
            rhwp_text = "\n".join(f"{number}. 선택지처럼 보이는 줄" for number in range(1, 32))
            header = (
                b"HWP Document File".ljust(32, b"\0")
                + (0).to_bytes(4, "little")
                + (1).to_bytes(4, "little")
                + b"\0" * 216
            )

            class FakeOle:
                def listdir(self, streams=True, storages=False):
                    return [["BodyText", "Section0"]]

                def exists(self, name):
                    return name == "FileHeader"

                def openstream(self, name):
                    return io.BytesIO(header)

                def close(self):
                    pass

            with (
                mock.patch.object(preprocess.olefile, "isOleFile", return_value=True),
                mock.patch.object(preprocess.olefile, "OleFileIO", return_value=FakeOle()),
                mock.patch.object(preprocess, "_extract_hwp_text_with_pyhwp", return_value=pyhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_unhwp", return_value=unhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_rhwp", return_value=rhwp_text, create=True),
                mock.patch.object(preprocess, "_extract_hwp_text_with_hwpilot", return_value="", create=True),
            ):
                inspection = preprocess.inspect_hwp_document(source)

            self.assertEqual("hwp5txt", inspection["hwp_text_extractor"])
            self.assertEqual(20, inspection["hwp_text_stem_problem_count"])

    def test_inspect_hwpx_document_extracts_xml_text_problem_signals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwpx"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr(
                    "Contents/section0.xml",
                    """<?xml version="1.0" encoding="UTF-8"?>
                    <hp:sec xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
                      <hp:p><hp:run><hp:t>1. 윗글의 내용과 일치하지 않은 것은?</hp:t></hp:run></hp:p>
                      <hp:p><hp:run><hp:t>다음 자료에 대한 설명으로 옳은 것은?</hp:t></hp:run></hp:p>
                      <hp:p><hp:run><hp:t>2. ㉠에 대한 설명으로 가장 적절한 것은?</hp:t></hp:run></hp:p>
                    </hp:sec>
                    """,
                )

            inspector = getattr(preprocess, "inspect_hwpx_document", None)
            self.assertIsNotNone(inspector)
            inspection = inspector(source)

            self.assertIs(True, inspection["hwpx_zip_file"])
            self.assertEqual("hwpx-xml", inspection["hwp_text_extractor"])
            self.assertIn("1. 윗글의 내용과 일치하지 않은 것은?", inspection["hwp_preview_text"])
            self.assertEqual(2, inspection["hwp_text_numbered_problem_count"])
            self.assertEqual(1, inspection["hwp_text_stem_problem_count"])

    def test_hwp_routes_through_converted_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "exam.hwp"
            source.write_bytes(b"hwp")
            converted = tmp_path / "converted" / "exam.pdf"
            rendered = tmp_path / "rendered.png"
            Image.new("RGB", (40, 50), "white").save(rendered)

            def fake_convert_hwp_to_pdf(src, out_dir):
                self.assertEqual(source, src)
                self.assertEqual(tmp_path / "out" / "converted", out_dir)
                return converted

            def fake_render_pdf_pages(src, out_dir, dpi):
                self.assertEqual(converted, src)
                self.assertEqual(tmp_path / "out" / "rendered", out_dir)
                self.assertEqual(144, dpi)
                return [
                    preprocess.NormalizedPageImage(
                        page_id="page-001",
                        source_path=str(src),
                        normalized_path=str(rendered),
                        page_index=0,
                        width_px=40,
                        height_px=50,
                        metadata={"source_type": "pdf"},
                    )
                ]

            with (
                mock.patch.object(preprocess, "convert_hwp_to_pdf", side_effect=fake_convert_hwp_to_pdf, create=True),
                mock.patch.object(preprocess, "render_pdf_pages", side_effect=fake_render_pdf_pages),
            ):
                pages = preprocess.prepare_pages(source, tmp_path / "out", dpi=144)

            self.assertEqual(1, len(pages))
            self.assertEqual("hwp", pages[0].metadata["source_type"])
            self.assertIs(True, pages[0].metadata["document_like"])
            self.assertEqual(str(source), pages[0].metadata["source_hwp_path"])
            self.assertEqual(str(converted), pages[0].metadata["converted_pdf_path"])

    def test_hwp_uses_rhwp_core_renderer_before_python_and_pdf_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "exam.hwp"
            source.write_bytes(b"hwp")
            core_rendered = tmp_path / "rhwp-core-page.png"
            python_rendered = tmp_path / "rhwp-python-page.png"
            Image.new("RGB", (40, 50), "white").save(core_rendered)
            Image.new("RGB", (40, 50), "white").save(python_rendered)
            core_pages = [
                preprocess.NormalizedPageImage(
                    page_id="rhwp-core-page-001",
                    source_path=str(source),
                    normalized_path=str(core_rendered),
                    page_index=0,
                    width_px=40,
                    height_px=50,
                    metadata={
                        "source_type": "hwp",
                        "hwp_renderer": "rhwp-core",
                        "hwp_renderer_page_count": 1,
                    },
                )
            ]
            python_pages = [
                preprocess.NormalizedPageImage(
                    page_id="rhwp-python-page-001",
                    source_path=str(source),
                    normalized_path=str(python_rendered),
                    page_index=0,
                    width_px=40,
                    height_px=50,
                    metadata={
                        "source_type": "hwp",
                        "hwp_renderer": "rhwp-python",
                        "hwp_renderer_page_count": 1,
                    },
                )
            ]

            with (
                mock.patch.object(preprocess, "inspect_hangul_document", return_value={"hwp_signature": "HWP Document File"}),
                mock.patch.object(preprocess, "_render_hwp_pages_with_rhwp_core", return_value=core_pages),
                mock.patch.object(preprocess, "_render_hwp_pages_with_rhwp_python", return_value=python_pages),
                mock.patch.object(preprocess, "convert_hwp_to_pdf", side_effect=AssertionError("PDF conversion should not run")),
                mock.patch.object(preprocess, "render_pdf_pages", side_effect=AssertionError("PDF rendering should not run")),
            ):
                pages = preprocess.prepare_pages(source, tmp_path / "out", dpi=144)

            self.assertEqual(1, len(pages))
            self.assertEqual("hwp", pages[0].metadata["source_type"])
            self.assertEqual("rhwp-core", pages[0].metadata["hwp_renderer"])
            self.assertEqual(str(source), pages[0].metadata["source_hwp_path"])
            self.assertNotIn("converted_pdf_path", pages[0].metadata)

    def test_hwp_uses_rhwp_core_renderer_before_pdf_conversion_when_python_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "exam.hwp"
            source.write_bytes(b"hwp")
            rendered = tmp_path / "rhwp-page.png"
            Image.new("RGB", (40, 50), "white").save(rendered)
            rhwp_pages = [
                preprocess.NormalizedPageImage(
                    page_id="rhwp-page-001",
                    source_path=str(source),
                    normalized_path=str(rendered),
                    page_index=0,
                    width_px=40,
                    height_px=50,
                    metadata={
                        "source_type": "hwp",
                        "hwp_renderer": "rhwp-core",
                        "hwp_renderer_page_count": 1,
                    },
                )
            ]

            with (
                mock.patch.object(preprocess, "inspect_hangul_document", return_value={"hwp_signature": "HWP Document File"}),
                mock.patch.object(preprocess, "_render_hwp_pages_with_rhwp_python", return_value=[]),
                mock.patch.object(preprocess, "_render_hwp_pages_with_rhwp_core", return_value=rhwp_pages),
                mock.patch.object(preprocess, "convert_hwp_to_pdf", side_effect=AssertionError("PDF conversion should not run")),
                mock.patch.object(preprocess, "render_pdf_pages", side_effect=AssertionError("PDF rendering should not run")),
            ):
                pages = preprocess.prepare_pages(source, tmp_path / "out", dpi=144)

            self.assertEqual(1, len(pages))
            self.assertEqual("hwp", pages[0].metadata["source_type"])
            self.assertEqual("rhwp-core", pages[0].metadata["hwp_renderer"])
            self.assertEqual(str(source), pages[0].metadata["source_hwp_path"])
            self.assertNotIn("converted_pdf_path", pages[0].metadata)

    def test_hwp_prepare_pages_reuses_normalized_cache_for_same_source_and_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "exam.hwp"
            source.write_bytes(b"hwp v1")
            rendered = tmp_path / "rhwp-page.png"
            Image.new("RGB", (40, 50), "white").save(rendered)
            rhwp_pages = [
                preprocess.NormalizedPageImage(
                    page_id="rhwp-page-001",
                    source_path=str(source),
                    normalized_path=str(rendered),
                    page_index=0,
                    width_px=40,
                    height_px=50,
                    metadata={
                        "source_type": "hwp",
                        "hwp_renderer": "rhwp-core",
                        "hwp_renderer_page_count": 1,
                    },
                )
            ]

            with (
                mock.patch.object(
                    preprocess,
                    "inspect_hangul_document",
                    return_value={
                        "hwp_signature": "HWP Document File",
                        "hwp_preview_text": "1. 문제",
                        "hwp_text_numbered_problem_count": 1,
                    },
                ) as inspect_mock,
                mock.patch.object(preprocess, "_render_hwp_pages_with_rhwp_core", return_value=rhwp_pages) as render_mock,
            ):
                first = preprocess.prepare_pages(
                    source,
                    tmp_path / "out",
                    dpi=144,
                    enable_deskew=False,
                    enable_margin_crop=False,
                )
                second = preprocess.prepare_pages(
                    source,
                    tmp_path / "out",
                    dpi=144,
                    enable_deskew=False,
                    enable_margin_crop=False,
                )

            self.assertEqual(1, inspect_mock.call_count)
            self.assertEqual(1, render_mock.call_count)
            self.assertEqual(Path(first[0].normalized_path).resolve(), Path(second[0].normalized_path).resolve())
            self.assertIs(True, second[0].metadata["hwp_normalized_cache_hit"])
            self.assertEqual(str(source), second[0].metadata["source_hwp_path"])

    def test_hwp_prepare_pages_reuses_normalized_cache_for_same_content_with_new_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            first_source = tmp_path / "upload-a.hwp"
            second_source = tmp_path / "upload-b.hwp"
            first_source.write_bytes(b"same hwp bytes")
            second_source.write_bytes(b"same hwp bytes")
            rendered = tmp_path / "rhwp-page.png"
            Image.new("RGB", (40, 50), "white").save(rendered)
            rhwp_pages = [
                preprocess.NormalizedPageImage(
                    page_id="rhwp-page-001",
                    source_path=str(first_source),
                    normalized_path=str(rendered),
                    page_index=0,
                    width_px=40,
                    height_px=50,
                    metadata={
                        "source_type": "hwp",
                        "hwp_renderer": "rhwp-core",
                        "hwp_renderer_page_count": 1,
                    },
                )
            ]

            with (
                mock.patch.object(
                    preprocess,
                    "inspect_hangul_document",
                    return_value={"hwp_signature": "HWP Document File"},
                ) as inspect_mock,
                mock.patch.object(preprocess, "_render_hwp_pages_with_rhwp_core", return_value=rhwp_pages) as render_mock,
            ):
                preprocess.prepare_pages(
                    first_source,
                    tmp_path / "out",
                    dpi=144,
                    enable_deskew=False,
                    enable_margin_crop=False,
                )
                second = preprocess.prepare_pages(
                    second_source,
                    tmp_path / "out",
                    dpi=144,
                    enable_deskew=False,
                    enable_margin_crop=False,
                )

            self.assertEqual(1, inspect_mock.call_count)
            self.assertEqual(1, render_mock.call_count)
            self.assertIs(True, second[0].metadata["hwp_normalized_cache_hit"])
            self.assertEqual(str(second_source), second[0].metadata["source_hwp_path"])

    def test_render_hwp_pages_with_rhwp_core_reads_renderer_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "exam.hwp"
            source.write_bytes(b"hwp")
            output_dir = tmp_path / "rhwp"
            page_path = output_dir / "exam_page_001.png"
            output_dir.mkdir(parents=True)
            Image.new("RGB", (30, 40), "white").save(page_path)
            payload = [
                {
                    "page_id": "exam-page-001",
                    "source_path": str(source),
                    "normalized_path": str(page_path),
                    "page_index": 0,
                    "width_px": 30,
                    "height_px": 40,
                    "metadata": {
                        "source_type": "hwp",
                        "hwp_renderer": "rhwp-core",
                        "hwp_renderer_page_count": 1,
                    },
                }
            ]

            def fake_run(cmd, check, stdout, stderr, text, encoding, errors, timeout):
                self.assertEqual(["/usr/bin/node", "/tmp/render_hwp_with_rhwp_core.mjs"], cmd[:2])
                self.assertIn(str(source), cmd)
                self.assertIn(str(output_dir), cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

            with (
                mock.patch.object(
                    preprocess,
                    "_iter_rhwp_core_renderer_commands",
                    return_value=[["/usr/bin/node", "/tmp/render_hwp_with_rhwp_core.mjs"]],
                    create=True,
                ),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                pages = preprocess._render_hwp_pages_with_rhwp_core(source, output_dir, dpi=144)

            self.assertEqual(1, len(pages))
            self.assertEqual(page_path, Path(pages[0].normalized_path))
            self.assertEqual("rhwp-core", pages[0].metadata["hwp_renderer"])

    def test_render_hwp_pages_with_rhwp_python_reads_renderer_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "exam.hwp"
            source.write_bytes(b"hwp")
            output_dir = tmp_path / "rhwp-python"
            page_path = output_dir / "exam_page_001.png"
            output_dir.mkdir(parents=True)
            Image.new("RGB", (30, 40), "white").save(page_path)

            def fake_run(cmd, check, stdout, stderr, text, encoding, errors, timeout):
                self.assertEqual(["/venv/bin/python", "-c", "script"], cmd[:3])
                self.assertEqual(str(source), cmd[3])
                self.assertEqual(str(output_dir), cmd[4])
                self.assertEqual("144", cmd[5])
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps(
                        [
                            {
                                "page_id": "exam-page-001",
                                "source_path": str(source),
                                "normalized_path": str(page_path),
                                "page_index": 0,
                                "width_px": 30,
                                "height_px": 40,
                                "metadata": {"source_type": "hwp", "hwp_renderer": "rhwp-python"},
                            }
                        ]
                    ),
                    stderr="",
                )

            with (
                mock.patch.object(preprocess, "_iter_rhwp_python_renderer_commands", return_value=[["/venv/bin/python", "-c", "script"]]),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                pages = preprocess._render_hwp_pages_with_rhwp_python(source, output_dir, dpi=144)

            self.assertEqual(1, len(pages))
            self.assertEqual(page_path, Path(pages[0].normalized_path))
            self.assertEqual("rhwp-python", pages[0].metadata["hwp_renderer"])

    def test_render_hwp_pages_with_rhwp_python_reuses_cache_for_same_source_and_dpi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "exam.hwp"
            source.write_bytes(b"hwp v1")
            output_dir = tmp_path / "rhwp-python"
            page_path = output_dir / "exam_page_001.png"
            output_dir.mkdir(parents=True)
            Image.new("RGB", (30, 40), "white").save(page_path)
            payload = [
                {
                    "page_id": "exam-page-001",
                    "source_path": str(source),
                    "normalized_path": str(page_path),
                    "page_index": 0,
                    "width_px": 30,
                    "height_px": 40,
                    "metadata": {"source_type": "hwp", "hwp_renderer": "rhwp-python"},
                }
            ]
            run_count = 0

            def fake_run(cmd, check, stdout, stderr, text, encoding, errors, timeout):
                nonlocal run_count
                run_count += 1
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

            with (
                mock.patch.object(preprocess, "_iter_rhwp_python_renderer_commands", return_value=[["/venv/bin/python", "-c", "script"]]),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                first = preprocess._render_hwp_pages_with_rhwp_python(source, output_dir, dpi=144)
                second = preprocess._render_hwp_pages_with_rhwp_python(source, output_dir, dpi=144)

            self.assertEqual(1, run_count)
            self.assertEqual(Path(first[0].normalized_path).resolve(), Path(second[0].normalized_path).resolve())
            self.assertIs(True, second[0].metadata["hwp_renderer_cache_hit"])

    def test_render_hwp_pages_with_rhwp_core_reuses_cache_for_same_source_and_dpi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "exam.hwp"
            source.write_bytes(b"hwp v1")
            output_dir = tmp_path / "rhwp"
            page_path = output_dir / "exam_page_001.png"
            output_dir.mkdir(parents=True)
            Image.new("RGB", (30, 40), "white").save(page_path)
            payload = [
                {
                    "page_id": "exam-page-001",
                    "source_path": str(source),
                    "normalized_path": str(page_path),
                    "page_index": 0,
                    "width_px": 30,
                    "height_px": 40,
                    "metadata": {"source_type": "hwp", "hwp_renderer": "rhwp-core"},
                }
            ]
            run_count = 0

            def fake_run(cmd, check, stdout, stderr, text, encoding, errors, timeout):
                nonlocal run_count
                run_count += 1
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

            with (
                mock.patch.object(
                    preprocess,
                    "_iter_rhwp_core_renderer_commands",
                    return_value=[["/usr/bin/node", "/tmp/render_hwp_with_rhwp_core.mjs"]],
                    create=True,
                ),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                first = preprocess._render_hwp_pages_with_rhwp_core(source, output_dir, dpi=144)
                second = preprocess._render_hwp_pages_with_rhwp_core(source, output_dir, dpi=144)

            self.assertEqual(1, run_count)
            self.assertEqual(Path(first[0].normalized_path).resolve(), Path(second[0].normalized_path).resolve())
            self.assertIs(True, second[0].metadata["hwp_renderer_cache_hit"])

    def test_render_hwp_pages_with_rhwp_core_caches_absolute_paths_with_relative_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "exam.hwp"
            source.write_bytes(b"hwp v1")
            output_dir = Path("rhwp")
            page_path = tmp_path / output_dir / "exam_page_001.png"
            page_path.parent.mkdir(parents=True)
            Image.new("RGB", (30, 40), "white").save(page_path)
            payload = [
                {
                    "page_id": "exam-page-001",
                    "source_path": str(source),
                    "normalized_path": str(page_path),
                    "page_index": 0,
                    "width_px": 30,
                    "height_px": 40,
                    "metadata": {"source_type": "hwp", "hwp_renderer": "rhwp-core"},
                }
            ]
            run_count = 0
            previous_cwd = Path.cwd()

            def fake_run(cmd, check, stdout, stderr, text, encoding, errors, timeout):
                nonlocal run_count
                run_count += 1
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

            try:
                os.chdir(tmp_path)
                with (
                    mock.patch.object(
                        preprocess,
                        "_iter_rhwp_core_renderer_commands",
                        return_value=[["/usr/bin/node", "/tmp/render_hwp_with_rhwp_core.mjs"]],
                        create=True,
                    ),
                    mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
                ):
                    first = preprocess._render_hwp_pages_with_rhwp_core(source, output_dir, dpi=144)
                    second = preprocess._render_hwp_pages_with_rhwp_core(source, output_dir, dpi=144)
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(1, run_count)
            self.assertEqual(Path(first[0].normalized_path).resolve(), Path(second[0].normalized_path).resolve())
            self.assertIs(True, second[0].metadata["hwp_renderer_cache_hit"])

    def test_render_hwp_pages_with_rhwp_core_reuses_cache_for_same_content_with_new_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            first_source = tmp_path / "upload-a.hwp"
            second_source = tmp_path / "upload-b.hwp"
            first_source.write_bytes(b"same hwp bytes")
            second_source.write_bytes(b"same hwp bytes")
            output_dir = tmp_path / "rhwp"
            page_path = output_dir / "upload_a_page_001.png"
            output_dir.mkdir(parents=True)
            Image.new("RGB", (30, 40), "white").save(page_path)
            payload = [
                {
                    "page_id": "upload-a-page-001",
                    "source_path": str(first_source),
                    "normalized_path": str(page_path),
                    "page_index": 0,
                    "width_px": 30,
                    "height_px": 40,
                    "metadata": {"source_type": "hwp", "hwp_renderer": "rhwp-core"},
                }
            ]
            run_count = 0

            def fake_run(cmd, check, stdout, stderr, text, encoding, errors, timeout):
                nonlocal run_count
                run_count += 1
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

            with (
                mock.patch.object(
                    preprocess,
                    "_iter_rhwp_core_renderer_commands",
                    return_value=[["/usr/bin/node", "/tmp/render_hwp_with_rhwp_core.mjs"]],
                    create=True,
                ),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                first = preprocess._render_hwp_pages_with_rhwp_core(first_source, output_dir, dpi=144)
                second = preprocess._render_hwp_pages_with_rhwp_core(second_source, output_dir, dpi=144)

            self.assertEqual(1, run_count)
            self.assertEqual(Path(first[0].normalized_path).resolve(), Path(second[0].normalized_path).resolve())
            self.assertEqual(str(second_source), second[0].source_path)
            self.assertIs(True, second[0].metadata["hwp_renderer_cache_hit"])

    def test_render_hwp_pages_with_rhwp_core_invalidates_cache_when_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "exam.hwp"
            source.write_bytes(b"hwp v1")
            output_dir = tmp_path / "rhwp"
            page_path = output_dir / "exam_page_001.png"
            output_dir.mkdir(parents=True)
            Image.new("RGB", (30, 40), "white").save(page_path)
            run_count = 0

            def fake_run(cmd, check, stdout, stderr, text, encoding, errors, timeout):
                nonlocal run_count
                run_count += 1
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps(
                        [
                            {
                                "page_id": f"exam-page-{run_count:03d}",
                                "source_path": str(source),
                                "normalized_path": str(page_path),
                                "page_index": 0,
                                "width_px": 30,
                                "height_px": 40,
                                "metadata": {"source_type": "hwp", "hwp_renderer": "rhwp-core"},
                            }
                        ]
                    ),
                    stderr="",
                )

            with (
                mock.patch.object(
                    preprocess,
                    "_iter_rhwp_core_renderer_commands",
                    return_value=[["/usr/bin/node", "/tmp/render_hwp_with_rhwp_core.mjs"]],
                    create=True,
                ),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                first = preprocess._render_hwp_pages_with_rhwp_core(source, output_dir, dpi=144)
                source.write_bytes(b"hwp v2")
                second = preprocess._render_hwp_pages_with_rhwp_core(source, output_dir, dpi=144)

            self.assertEqual(2, run_count)
            self.assertEqual("exam-page-001", first[0].page_id)
            self.assertEqual("exam-page-002", second[0].page_id)

    def test_hwp_records_conversion_quality_from_rendered_pdf_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "exam.hwpx"
            source.write_bytes(b"hwpx")
            converted = tmp_path / "converted" / "exam.pdf"
            page_with_marker = tmp_path / "page_with_marker.png"
            blank_page = tmp_path / "blank_page.png"
            image = Image.new("RGB", (80, 80), "white")
            for x in range(8, 24):
                for y in range(8, 24):
                    image.putpixel((x, y), (0, 0, 0))
            image.save(page_with_marker)
            Image.new("RGB", (80, 80), "white").save(blank_page)

            rendered_pages = [
                preprocess.NormalizedPageImage(
                    page_id="page-001",
                    source_path=str(converted),
                    normalized_path=str(page_with_marker),
                    page_index=0,
                    width_px=80,
                    height_px=80,
                    metadata={
                        "source_type": "pdf",
                        "pdf_problem_markers": [{"number": 1}, {"number": 2}],
                    },
                ),
                preprocess.NormalizedPageImage(
                    page_id="page-002",
                    source_path=str(converted),
                    normalized_path=str(blank_page),
                    page_index=1,
                    width_px=80,
                    height_px=80,
                    metadata={"source_type": "pdf", "pdf_problem_markers": []},
                ),
            ]

            with (
                mock.patch.object(preprocess, "convert_hwp_to_pdf", return_value=converted, create=True),
                mock.patch.object(preprocess, "render_pdf_pages", return_value=rendered_pages),
            ):
                pages = preprocess.prepare_pages(source, tmp_path / "out", dpi=144)

            quality = pages[0].metadata["hwp_conversion_quality"]
            self.assertEqual(2, quality["page_count"])
            self.assertEqual(2, quality["pdf_text_marker_count"])
            self.assertEqual(1, quality["pdf_pages_with_text_markers"])
            self.assertEqual(1, quality["pdf_pages_without_text_markers"])
            self.assertEqual(1, quality["blank_page_count"])
            self.assertTrue(quality["has_pdf_text_markers"])
            self.assertIn("blank_pages_detected", quality["warnings"])
            self.assertEqual(quality, pages[1].metadata["hwp_conversion_quality"])

    def test_hwp_conversion_quality_records_hwp_text_problem_signals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "exam.hwp"
            source.write_bytes(b"hwp")
            converted = tmp_path / "converted" / "exam.pdf"
            rendered = tmp_path / "page.png"
            Image.new("RGB", (80, 80), "white").save(rendered)

            rendered_pages = [
                preprocess.NormalizedPageImage(
                    page_id="page-001",
                    source_path=str(converted),
                    normalized_path=str(rendered),
                    page_index=0,
                    width_px=80,
                    height_px=80,
                    metadata={"source_type": "pdf", "pdf_problem_markers": [{"number": 1}]},
                )
            ]

            with (
                mock.patch.object(
                    preprocess,
                    "inspect_hwp_document",
                    return_value={
                        "hwp_preview_text_length": 3000,
                        "hwp_text_extractor": "hwp5txt",
                        "hwp_text_numbered_problem_count": 45,
                        "hwp_text_stem_problem_count": 0,
                    },
                ),
                mock.patch.object(preprocess, "convert_hwp_to_pdf", return_value=converted, create=True),
                mock.patch.object(preprocess, "render_pdf_pages", return_value=rendered_pages),
            ):
                pages = preprocess.prepare_pages(source, tmp_path / "out", dpi=144)

            quality = pages[0].metadata["hwp_conversion_quality"]
            self.assertEqual("hwp5txt", quality["hwp_text_extractor"])
            self.assertEqual(45, quality["hwp_text_numbered_problem_count"])
            self.assertEqual(0, quality["hwp_text_stem_problem_count"])

    def test_hwpx_conversion_quality_records_hwpx_text_problem_signals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "exam.hwpx"
            source.write_bytes(b"hwpx")
            converted = tmp_path / "converted" / "exam.pdf"
            rendered = tmp_path / "page.png"
            Image.new("RGB", (80, 80), "white").save(rendered)

            rendered_pages = [
                preprocess.NormalizedPageImage(
                    page_id="page-001",
                    source_path=str(converted),
                    normalized_path=str(rendered),
                    page_index=0,
                    width_px=80,
                    height_px=80,
                    metadata={"source_type": "pdf", "pdf_problem_markers": [{"number": 1}]},
                )
            ]

            with (
                mock.patch.object(
                    preprocess,
                    "inspect_hwpx_document",
                    return_value={
                        "hwp_preview_text_length": 2400,
                        "hwp_preview_text": "1. 윗글의 내용과 일치하지 않은 것은?",
                        "hwp_text_extractor": "hwpx-xml",
                        "hwp_text_numbered_problem_count": 30,
                        "hwp_text_stem_problem_count": 0,
                    },
                    create=True,
                ),
                mock.patch.object(preprocess, "convert_hwp_to_pdf", return_value=converted, create=True),
                mock.patch.object(preprocess, "render_pdf_pages", return_value=rendered_pages),
            ):
                pages = preprocess.prepare_pages(source, tmp_path / "out", dpi=144)

            quality = pages[0].metadata["hwp_conversion_quality"]
            self.assertEqual("hwpx-xml", quality["hwp_text_extractor"])
            self.assertEqual(30, quality["hwp_text_numbered_problem_count"])
            self.assertEqual("1. 윗글의 내용과 일치하지 않은 것은?", pages[0].metadata["hwp_preview_text"])

    def test_hwp_preview_text_is_preserved_in_page_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "school.hwp"
            source.write_bytes(b"hwp")
            converted = tmp_path / "converted" / "school.pdf"
            rendered = tmp_path / "page.png"
            Image.new("RGB", (80, 80), "white").save(rendered)

            rendered_pages = [
                preprocess.NormalizedPageImage(
                    page_id="page-001",
                    source_path=str(converted),
                    normalized_path=str(rendered),
                    page_index=0,
                    width_px=80,
                    height_px=80,
                    metadata={"source_type": "pdf", "pdf_problem_markers": []},
                )
            ]

            with (
                mock.patch.object(
                    preprocess,
                    "inspect_hwp_document",
                    return_value={
                        "hwp_preview_text_length": 86,
                        "hwp_preview_text": "단어의 뜻이 옳게 짝지어진 것은?\nCtrl+3 누르면 지시문 생성됩니다.",
                    },
                ),
                mock.patch.object(preprocess, "convert_hwp_to_pdf", return_value=converted, create=True),
                mock.patch.object(preprocess, "render_pdf_pages", return_value=rendered_pages),
            ):
                pages = preprocess.prepare_pages(source, tmp_path / "out", dpi=144)

            self.assertEqual(
                "단어의 뜻이 옳게 짝지어진 것은?\nCtrl+3 누르면 지시문 생성됩니다.",
                pages[0].metadata["hwp_preview_text"],
            )

    def test_hwp_conversion_quality_marks_sparse_pdf_markers_unreliable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "social.hwp"
            source.write_bytes(b"hwp")
            converted = tmp_path / "converted" / "social.pdf"
            page_image = tmp_path / "page.png"
            image = Image.new("RGB", (80, 80), "white")
            for x in range(8, 24):
                for y in range(8, 24):
                    image.putpixel((x, y), (0, 0, 0))
            image.save(page_image)

            rendered_pages = []
            for index in range(13):
                rendered_pages.append(
                    preprocess.NormalizedPageImage(
                        page_id=f"page-{index + 1:03d}",
                        source_path=str(converted),
                        normalized_path=str(page_image),
                        page_index=index,
                        width_px=80,
                        height_px=80,
                        metadata={
                            "source_type": "pdf",
                            "pdf_problem_markers": (
                                [{"number": 1}, {"number": 2}, {"number": 3}]
                                if index == 12
                                else []
                            ),
                        },
                    )
                )

            with (
                mock.patch.object(preprocess, "convert_hwp_to_pdf", return_value=converted, create=True),
                mock.patch.object(preprocess, "render_pdf_pages", return_value=rendered_pages),
            ):
                pages = preprocess.prepare_pages(source, tmp_path / "out", dpi=144)

            quality = pages[0].metadata["hwp_conversion_quality"]
            self.assertTrue(quality["has_pdf_text_markers"])
            self.assertFalse(quality["pdf_text_markers_reliable"])
            self.assertEqual("ocr_fallback", quality["preferred_segmentation_path"])
            self.assertIn("low_pdf_text_marker_coverage", quality["warnings"])

    def test_hwp_sparse_pdf_number_markers_can_use_text_stem_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "social.hwp"
            source.write_bytes(b"hwp")
            converted = tmp_path / "converted" / "social.pdf"
            page_image = tmp_path / "page.png"
            image = Image.new("RGB", (80, 80), "white")
            for x in range(8, 24):
                for y in range(8, 24):
                    image.putpixel((x, y), (0, 0, 0))
            image.save(page_image)

            rendered_pages = []
            for index in range(3):
                rendered_pages.append(
                    preprocess.NormalizedPageImage(
                        page_id=f"page-{index + 1:03d}",
                        source_path=str(converted),
                        normalized_path=str(page_image),
                        page_index=index,
                        width_px=80,
                        height_px=80,
                        metadata={
                            "source_type": "pdf",
                            "pdf_problem_markers": ([{"number": 1}] if index == 2 else []),
                            "pdf_text_stem_markers": [
                                {
                                    "marker_kind": "text_stem",
                                    "text": "다음 자료에 대한 설명으로 옳은 것은?",
                                    "bbox": {"left": 5, "top": 10, "right": 70, "bottom": 18},
                                }
                            ],
                        },
                    )
                )

            with (
                mock.patch.object(preprocess, "convert_hwp_to_pdf", return_value=converted, create=True),
                mock.patch.object(preprocess, "render_pdf_pages", return_value=rendered_pages),
            ):
                pages = preprocess.prepare_pages(source, tmp_path / "out", dpi=144)

            quality = pages[0].metadata["hwp_conversion_quality"]
            self.assertEqual(3, quality["pdf_text_marker_count"])
            self.assertEqual(1, quality["pdf_numeric_text_marker_count"])
            self.assertTrue(quality["pdf_text_markers_reliable"])
            self.assertEqual("pdf_text_stem_markers", quality["preferred_segmentation_path"])
            self.assertEqual("text_stem", pages[0].metadata["pdf_problem_markers"][0]["marker_kind"])
            self.assertIn("using_pdf_text_stem_markers", quality["warnings"])

    def test_convert_hwp_to_pdf_uses_soffice_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            output_dir = tmp_path / "converted"
            expected_pdf = output_dir / "worksheet.pdf"

            def fake_run(cmd, check, stdout, stderr, text, timeout):
                self.assertEqual(["/usr/bin/soffice", "--headless"], cmd[:2])
                self.assertIn("--convert-to", cmd)
                self.assertIn("pdf", cmd)
                self.assertIn(str(output_dir), cmd)
                output_dir.mkdir(parents=True, exist_ok=True)
                expected_pdf.write_bytes(b"%PDF-1.4")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with (
                mock.patch.object(preprocess, "_iter_hwp_pdf_converter_commands", return_value=[["/usr/bin/soffice", "--headless"]], create=True),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                self.assertEqual(expected_pdf, preprocess.convert_hwp_to_pdf(source, output_dir))

    def test_hwp_pdf_converter_commands_include_rhwp_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rhwp_path = Path(temp_dir) / "rhwp"
            rhwp_path.write_text("#!/bin/sh\n", encoding="utf-8")
            rhwp_path.chmod(0o755)

            with (
                mock.patch.dict(os.environ, {"EDB_RHWP": str(rhwp_path)}),
                mock.patch.object(preprocess.shutil, "which", return_value=None),
            ):
                commands = preprocess._iter_hwp_pdf_converter_commands()

            self.assertIn([str(rhwp_path)], commands)

    def test_convert_hwp_to_pdf_can_use_rhwp_export_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            output_dir = tmp_path / "converted"
            expected_pdf = output_dir / "worksheet.pdf"
            calls = []

            def fake_run(cmd, check, stdout, stderr, text, timeout):
                calls.append(cmd)
                if cmd == ["/usr/local/bin/rhwp", "export-pdf", str(source), "-o", str(expected_pdf)]:
                    output_dir.mkdir(parents=True, exist_ok=True)
                    expected_pdf.write_bytes(b"%PDF-1.4")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with (
                mock.patch.object(preprocess, "_iter_hwp_pdf_converter_commands", return_value=[["/usr/local/bin/rhwp"]], create=True),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                self.assertEqual(expected_pdf, preprocess.convert_hwp_to_pdf(source, output_dir))

            self.assertEqual([["/usr/local/bin/rhwp", "export-pdf", str(source), "-o", str(expected_pdf)]], calls)

    def test_convert_hwp_to_pdf_reuses_cache_for_unchanged_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp v1")
            output_dir = tmp_path / "converted"
            expected_pdf = output_dir / "worksheet.pdf"
            run_count = 0

            def fake_run(cmd, check, stdout, stderr, text, timeout):
                nonlocal run_count
                run_count += 1
                output_dir.mkdir(parents=True, exist_ok=True)
                expected_pdf.write_bytes(b"%PDF-1.4")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with (
                mock.patch.object(preprocess, "_iter_hwp_pdf_converter_commands", return_value=[["/usr/bin/soffice", "--headless"]], create=True),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                self.assertEqual(expected_pdf, preprocess.convert_hwp_to_pdf(source, output_dir))
                self.assertEqual(expected_pdf, preprocess.convert_hwp_to_pdf(source, output_dir))

            self.assertEqual(1, run_count)

    def test_convert_hwp_to_pdf_invalidates_cache_when_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp v1")
            output_dir = tmp_path / "converted"
            expected_pdf = output_dir / "worksheet.pdf"
            run_count = 0

            def fake_run(cmd, check, stdout, stderr, text, timeout):
                nonlocal run_count
                run_count += 1
                output_dir.mkdir(parents=True, exist_ok=True)
                expected_pdf.write_bytes(f"%PDF run {run_count}".encode("ascii"))
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with (
                mock.patch.object(preprocess, "_iter_hwp_pdf_converter_commands", return_value=[["/usr/bin/soffice", "--headless"]], create=True),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                self.assertEqual(expected_pdf, preprocess.convert_hwp_to_pdf(source, output_dir))
                source.write_bytes(b"hwp v2")
                self.assertEqual(expected_pdf, preprocess.convert_hwp_to_pdf(source, output_dir))

            self.assertEqual(2, run_count)
            self.assertEqual(b"%PDF run 2", expected_pdf.read_bytes())

    def test_convert_hwp_to_pdf_uses_hwpilot_hwpx_bridge_after_direct_pdf_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            output_dir = tmp_path / "converted"
            expected_pdf = output_dir / "worksheet.pdf"
            calls = []

            def fake_run(cmd, check, stdout, stderr, text, timeout):
                calls.append(cmd)
                if cmd[0] == "/usr/local/bin/hwpilot":
                    Path(cmd[-1]).write_bytes(b"hwpx")
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                if len(calls) == 3:
                    output_dir.mkdir(parents=True, exist_ok=True)
                    expected_pdf.write_bytes(b"%PDF-1.4")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with (
                mock.patch.object(preprocess, "_iter_hwp_pdf_converter_commands", return_value=[["/usr/bin/soffice", "--headless"]], create=True),
                mock.patch.object(preprocess, "_iter_hwp_hwpx_converter_commands", return_value=[["/usr/local/bin/hwpilot"]], create=True),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                self.assertEqual(expected_pdf, preprocess.convert_hwp_to_pdf(source, output_dir))

            self.assertEqual("/usr/bin/soffice", calls[0][0])
            self.assertEqual(["/usr/local/bin/hwpilot", "convert", str(source)], calls[1][:3])
            self.assertEqual(output_dir / "_hwpilot" / "worksheet.hwpx", Path(calls[1][3]))
            self.assertEqual("/usr/bin/soffice", calls[2][0])
            self.assertEqual(str(output_dir / "_hwpilot" / "worksheet.hwpx"), calls[2][-1])

    def test_hwpilot_hwpx_bridge_can_be_configured_by_env_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            hwpilot = tmp_path / "hwpilot"
            hwpilot.write_text("#!/bin/sh\n", encoding="utf-8")
            hwpilot.chmod(0o755)

            with (
                mock.patch.dict(os.environ, {"EDB_HWPILOT": str(hwpilot)}),
                mock.patch.object(preprocess.shutil, "which", return_value=None),
            ):
                commands = preprocess._iter_hwp_hwpx_converter_commands()

            self.assertEqual([[str(hwpilot)]], commands)

    def test_extract_hwp_text_with_hwpilot_uses_no_daemon_json_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            calls = []

            def fake_run(cmd, check, stdout, stderr, text, timeout, env):
                calls.append((cmd, env))
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps({"text": "1. 문제\n2. 문제"}),
                    stderr="",
                )

            with (
                mock.patch.object(preprocess, "_iter_hwpilot_text_converter_commands", return_value=[["/usr/local/bin/hwpilot"]], create=True),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                text = preprocess._extract_hwp_text_with_hwpilot(source)

            self.assertEqual("1. 문제\n2. 문제", text)
            self.assertEqual(["/usr/local/bin/hwpilot", "text", str(source)], calls[0][0])
            self.assertEqual("1", calls[0][1].get("HWPILOT_NO_DAEMON"))

    def test_extract_hwp_text_with_kordoc_reads_markdown_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            calls = []

            def fake_run(cmd, check, stdout, stderr, text, encoding, errors, timeout):
                calls.append(cmd)
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="1. 문제\n2. 문제",
                    stderr="",
                )

            with (
                mock.patch.object(preprocess, "_iter_kordoc_text_converter_commands", return_value=[["/usr/local/bin/kordoc"]], create=True),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                text = preprocess._extract_hwp_text_with_kordoc(source)

            self.assertEqual("1. 문제\n2. 문제", text)
            self.assertEqual(["/usr/local/bin/kordoc", str(source)], calls[0])

    def test_extract_hwp_text_with_hwp_hwpx_parser_reads_reader_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            calls = []

            def fake_run(cmd, check, stdout, stderr, text, encoding, errors, timeout):
                calls.append(cmd)
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="1. 문제\n2. 문제",
                    stderr="",
                )

            with (
                mock.patch.object(preprocess, "_iter_hwp_hwpx_parser_text_converter_commands", return_value=[["/venv/bin/python", "-c", "script"]], create=True),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                text = preprocess._extract_hwp_text_with_hwp_hwpx_parser(source)

            self.assertEqual("1. 문제\n2. 문제", text)
            self.assertEqual(["/venv/bin/python", "-c", "script", str(source)], calls[0])

    def test_hwp_hwpx_parser_text_converter_can_be_configured_by_env_python(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            python_path = tmp_path / "python"
            python_path.write_text("#!/bin/sh\n", encoding="utf-8")
            python_path.chmod(0o755)

            with (
                mock.patch.dict(os.environ, {"EDB_HWP_HWPX_PARSER_PYTHON": str(python_path)}),
                mock.patch.object(preprocess, "_python_can_import_hwp_hwpx_parser", return_value=True, create=True),
            ):
                commands = preprocess._iter_hwp_hwpx_parser_text_converter_commands()

            self.assertEqual(str(python_path), commands[0][0])
            self.assertEqual("-c", commands[0][1])

    def test_kordoc_text_converter_can_be_configured_by_env_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            kordoc = tmp_path / "kordoc"
            kordoc.write_text("#!/bin/sh\n", encoding="utf-8")
            kordoc.chmod(0o755)

            with (
                mock.patch.dict(os.environ, {"EDB_KORDOC": str(kordoc)}),
                mock.patch.object(preprocess.shutil, "which", return_value=None),
            ):
                commands = preprocess._iter_kordoc_text_converter_commands()

            self.assertEqual([str(kordoc)], commands[0])
            self.assertIn([str(kordoc)], commands)

    def test_extract_hwp_image_summary_with_hwpilot_reads_image_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            calls = []

            def fake_run(cmd, check, stdout, stderr, text, timeout, env):
                calls.append((cmd, env))
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps(
                        [
                            {"ref": "s0.img0", "width": 120, "height": 80, "format": "bmp"},
                            {"ref": "s0.img1", "width": 320, "height": 160, "format": "png"},
                        ]
                    ),
                    stderr="",
                )

            with (
                mock.patch.object(preprocess, "_iter_hwpilot_text_converter_commands", return_value=[["/usr/local/bin/hwpilot"]], create=True),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                summary = preprocess._extract_hwp_image_summary_with_hwpilot(source)

            self.assertEqual(
                {
                    "hwp_image_extractor": "hwpilot",
                    "hwp_image_count": 2,
                    "hwp_image_formats": ["bmp", "png"],
                    "hwp_image_max_width": 320,
                    "hwp_image_max_height": 160,
                },
                summary,
            )
            self.assertEqual(["/usr/local/bin/hwpilot", "image", "list", str(source)], calls[0][0])
            self.assertEqual("1", calls[0][1].get("HWPILOT_NO_DAEMON"))

    def test_convert_hwpx_to_pdf_can_use_airun_hwp_converter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwpx"
            source.write_bytes(b"hwpx")
            output_dir = tmp_path / "converted"
            staged_source = output_dir / "worksheet" / "worksheet.hwpx"
            expected_pdf = output_dir / "worksheet" / "worksheet.pdf"
            calls = []

            def fake_run(cmd, check, stdout, stderr, text, timeout, env=None):
                calls.append(cmd)
                if cmd[:3] == ["/usr/local/bin/airun-hwp", str(staged_source), "--format"]:
                    expected_pdf.parent.mkdir(parents=True, exist_ok=True)
                    expected_pdf.write_bytes(b"%PDF-1.4")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with (
                mock.patch.object(preprocess, "_iter_hwp_pdf_converter_commands", return_value=[["/usr/local/bin/airun-hwp"]], create=True),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                self.assertEqual(expected_pdf, preprocess.convert_hwp_to_pdf(source, output_dir))

            self.assertEqual("/usr/local/bin/airun-hwp", calls[0][0])
            self.assertEqual(str(staged_source), calls[0][1])
            self.assertIn("--format", calls[0])
            self.assertIn("pdf", calls[0])
            self.assertIn("--output", calls[0])
            self.assertIn(str(output_dir), calls[0])
            self.assertNotIn("--convert-to", calls[0])
            self.assertEqual(b"hwpx", staged_source.read_bytes())

    def test_convert_hwpx_to_pdf_gives_airun_hwp_a_libreoffice_shim_when_only_soffice_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwpx"
            source.write_bytes(b"hwpx")
            output_dir = tmp_path / "converted"
            expected_pdf = output_dir / "worksheet" / "worksheet.pdf"
            run_envs = []

            def fake_run(cmd, check, stdout, stderr, text, timeout, env=None):
                run_envs.append(dict(env or {}))
                path_dirs = str((env or {}).get("PATH") or "").split(os.pathsep)
                shim = next((Path(item) / "libreoffice" for item in path_dirs if (Path(item) / "libreoffice").exists()), None)
                if shim is not None:
                    expected_pdf.parent.mkdir(parents=True, exist_ok=True)
                    expected_pdf.write_bytes(b"%PDF-1.4")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            def fake_which(name):
                if name == "soffice":
                    return "/opt/homebrew/bin/soffice"
                if name == "libreoffice":
                    return None
                return None

            with (
                mock.patch.object(preprocess, "_iter_hwp_pdf_converter_commands", return_value=[["/usr/local/bin/airun-hwp"]], create=True),
                mock.patch.object(preprocess.sys, "platform", "darwin"),
                mock.patch.object(preprocess.shutil, "which", side_effect=fake_which),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                self.assertEqual(expected_pdf, preprocess.convert_hwp_to_pdf(source, output_dir))

            self.assertEqual(1, len(run_envs))
            self.assertIn(str(output_dir / "_airun_bin"), run_envs[0]["PATH"].split(os.pathsep))

    def test_convert_hwp_to_pdf_uses_pyhwp_html_chrome_fallback_after_direct_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            output_dir = tmp_path / "converted"
            expected_pdf = output_dir / "worksheet.pdf"
            calls = []

            def fake_run(cmd, check, stdout, stderr, text, timeout):
                calls.append(cmd)
                if cmd[0] == "/usr/local/bin/hwp5html":
                    html_root = Path(cmd[cmd.index("--output") + 1])
                    html_root.mkdir(parents=True, exist_ok=True)
                    (html_root / "index.xhtml").write_text("<html />", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="Error: source file could not be loaded")

            class FakeChromeProcess:
                def __init__(self, cmd, stdout, stderr, text):
                    calls.append(cmd)
                    for part in cmd:
                        if part.startswith("--print-to-pdf="):
                            Path(part.split("=", 1)[1]).write_bytes(b"%PDF-1.4")
                    self._terminated = False

                def poll(self):
                    return None

                def terminate(self):
                    self._terminated = True

                def wait(self, timeout=None):
                    return 0

                def kill(self):
                    self._terminated = True

            with (
                mock.patch.object(preprocess, "_iter_hwp_pdf_converter_commands", return_value=[["/usr/bin/soffice", "--headless"]], create=True),
                mock.patch.object(preprocess, "_iter_hwp_hwpx_converter_commands", return_value=[], create=True),
                mock.patch.object(preprocess, "_iter_pyhwp_html_converter_commands", return_value=[["/usr/local/bin/hwp5html"]], create=True),
                mock.patch.object(preprocess, "_iter_chrome_pdf_commands", return_value=[["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]], create=True),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
                mock.patch.object(preprocess.subprocess, "Popen", side_effect=FakeChromeProcess),
            ):
                self.assertEqual(expected_pdf, preprocess.convert_hwp_to_pdf(source, output_dir))

            self.assertEqual("/usr/bin/soffice", calls[0][0])
            self.assertEqual("/usr/local/bin/hwp5html", calls[1][0])
            self.assertEqual("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", calls[2][0])
            self.assertIn("--print-to-pdf-no-header", calls[2])
            self.assertIn("--no-pdf-header-footer", calls[2])

    def test_convert_hwp_to_pdf_caps_direct_converter_timeout_when_html_fallback_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")
            output_dir = tmp_path / "converted"
            timeouts = []

            def fake_run(cmd, check, stdout, stderr, text, timeout):
                timeouts.append((cmd[0], timeout))
                if cmd[0] == "/usr/local/bin/hwp5html":
                    html_root = Path(cmd[cmd.index("--output") + 1])
                    html_root.mkdir(parents=True, exist_ok=True)
                    (html_root / "index.xhtml").write_text("<html />", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="Error: source file could not be loaded")

            class FakeChromeProcess:
                def __init__(self, cmd, stdout, stderr, text):
                    for part in cmd:
                        if part.startswith("--print-to-pdf="):
                            Path(part.split("=", 1)[1]).write_bytes(b"%PDF-1.4")

                def poll(self):
                    return None

                def terminate(self):
                    pass

                def wait(self, timeout=None):
                    return 0

                def kill(self):
                    pass

            with (
                mock.patch.object(preprocess, "_iter_hwp_pdf_converter_commands", return_value=[["/usr/bin/soffice", "--headless"]], create=True),
                mock.patch.object(preprocess, "_iter_hwp_hwpx_converter_commands", return_value=[], create=True),
                mock.patch.object(preprocess, "_iter_pyhwp_html_converter_commands", return_value=[["/usr/local/bin/hwp5html"]], create=True),
                mock.patch.object(preprocess, "_iter_chrome_pdf_commands", return_value=[["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]], create=True),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
                mock.patch.object(preprocess.subprocess, "Popen", side_effect=FakeChromeProcess),
            ):
                preprocess.convert_hwp_to_pdf(source, output_dir, timeout_seconds=90)

            self.assertEqual(15, timeouts[0][1])
            self.assertEqual(90, timeouts[1][1])

    def test_convert_hwp_to_pdf_without_converter_raises_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")

            with mock.patch.object(preprocess, "_iter_hwp_pdf_converter_commands", return_value=[], create=True):
                with self.assertRaisesRegex(ValueError, "HWP.*LibreOffice|LibreOffice.*HWP"):
                    preprocess.convert_hwp_to_pdf(source, tmp_path / "converted")

    def test_convert_hwpx_to_pdf_without_converter_reports_valid_hwpx(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwpx"
            source.write_bytes(b"hwpx")
            inspection = {
                "hwpx_zip_file": True,
                "hwpx_xml_file_count": 12,
                "hwp_preview_text_length": 2400,
            }

            with (
                mock.patch.object(preprocess, "_iter_hwp_pdf_converter_commands", return_value=[], create=True),
                mock.patch.object(preprocess, "inspect_hwpx_document", return_value=inspection, create=True),
            ):
                with self.assertRaisesRegex(ValueError, "valid HWPX ZIP.*xml_files=12.*preview_text_length=2400") as raised:
                    preprocess.convert_hwp_to_pdf(source, tmp_path / "converted")
            message = str(raised.exception)
            self.assertIn("한컴오피스", message)
            self.assertIn("PDF", message)

    def test_convert_hwp_to_pdf_reports_valid_hwp_when_converter_cannot_load_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwp"
            source.write_bytes(b"hwp")

            def fake_run(cmd, check, stdout, stderr, text, timeout):
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="",
                    stderr="Error: source file could not be loaded",
                )

            inspection = {
                "ole_file": True,
                "hwp_signature": "HWP Document File",
                "hwp_flags": {"compressed": True, "password": False, "distribution": False},
                "hwp_section_count": 2,
                "hwp_preview_text_length": 1200,
            }

            with (
                mock.patch.object(preprocess, "_iter_hwp_pdf_converter_commands", return_value=[["/usr/bin/soffice", "--headless"]], create=True),
                mock.patch.object(preprocess, "_iter_hwp_hwpx_converter_commands", return_value=[], create=True),
                mock.patch.object(preprocess, "inspect_hwp_document", return_value=inspection, create=True),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                with self.assertRaisesRegex(ValueError, "valid HWP.*converter could not load"):
                    preprocess.convert_hwp_to_pdf(source, tmp_path / "converted")

    def test_convert_hwpx_to_pdf_reports_valid_hwpx_when_converter_cannot_load_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            source = tmp_path / "worksheet.hwpx"
            source.write_bytes(b"hwpx")

            def fake_run(cmd, check, stdout, stderr, text, timeout):
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="",
                    stderr="Error: source file could not be loaded",
                )

            inspection = {
                "hwpx_zip_file": True,
                "hwpx_xml_file_count": 8,
                "hwp_preview_text_length": 1800,
            }

            with (
                mock.patch.object(preprocess, "_iter_hwp_pdf_converter_commands", return_value=[["/usr/bin/soffice", "--headless"]], create=True),
                mock.patch.object(preprocess, "_iter_hwp_hwpx_converter_commands", return_value=[], create=True),
                mock.patch.object(preprocess, "_iter_pyhwp_html_converter_commands", return_value=[], create=True),
                mock.patch.object(preprocess, "inspect_hwpx_document", return_value=inspection, create=True),
                mock.patch.object(preprocess.subprocess, "run", side_effect=fake_run),
            ):
                with self.assertRaisesRegex(ValueError, "valid HWPX ZIP.*converter could not load") as raised:
                    preprocess.convert_hwp_to_pdf(source, tmp_path / "converted")
            message = str(raised.exception)
            self.assertIn("한컴오피스", message)
            self.assertIn("PDF", message)


if __name__ == "__main__":
    unittest.main()
