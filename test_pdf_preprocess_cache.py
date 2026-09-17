from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import preprocess


class TestPdfPreprocessCache(unittest.TestCase):
    def _rendered_pdf_page(
        self,
        source: Path,
        output_dir: Path,
        *,
        page_id: str = "page-001",
        size: tuple[int, int] = (96, 128),
        color: str = "white",
    ) -> preprocess.NormalizedPageImage:
        output_dir.mkdir(parents=True, exist_ok=True)
        rendered_path = output_dir / f"{source.stem}-{page_id}.png"
        Image.new("RGB", size, color).save(rendered_path)
        return preprocess.NormalizedPageImage(
            page_id=page_id,
            source_path=str(source),
            normalized_path=str(rendered_path),
            page_index=0,
            width_px=size[0],
            height_px=size[1],
            metadata={
                "source_type": "pdf",
                "pdf_problem_markers": [{"number": 1}],
            },
        )

    def test_prepare_pages_reuses_pdf_normalized_cache_for_same_source_and_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4 same source bytes")
            out_dir = root / "out"

            def fake_render_pdf_pages(
                src: Path, output_dir: Path, dpi: int, write_files: bool = True
            ) -> list[preprocess.NormalizedPageImage]:
                self.assertEqual(source, src)
                self.assertEqual(out_dir / "rendered", output_dir)
                self.assertEqual(144, dpi)
                return [self._rendered_pdf_page(src, output_dir)]

            with mock.patch.object(preprocess, "render_pdf_pages", side_effect=fake_render_pdf_pages) as render_mock:
                first = preprocess.prepare_pages(
                    source,
                    out_dir,
                    dpi=144,
                    enable_deskew=False,
                    enable_margin_crop=False,
                )
                second = preprocess.prepare_pages(
                    source,
                    out_dir,
                    dpi=144,
                    enable_deskew=False,
                    enable_margin_crop=False,
                )

            self.assertEqual(1, render_mock.call_count)
            self.assertEqual(Path(first[0].normalized_path).resolve(), Path(second[0].normalized_path).resolve())
            self.assertIs(True, second[0].metadata["pdf_normalized_cache_hit"])
            self.assertEqual(str(source), second[0].metadata["source_pdf_path"])

    def test_prepare_source_pages_preserves_full_200_dpi_pdf_master_without_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "a4.pdf"
            source.write_bytes(b"%PDF-1.4 full page master")
            rendered_size = (1654, 2339)

            def fake_render_pdf_pages(
                src: Path, output_dir: Path, dpi: int, write_files: bool = True
            ) -> list[preprocess.NormalizedPageImage]:
                self.assertEqual(200, dpi)
                page = self._rendered_pdf_page(src, output_dir, size=rendered_size)
                page.metadata["dpi"] = dpi
                return [page]

            with mock.patch.object(preprocess, "render_pdf_pages", side_effect=fake_render_pdf_pages):
                prepared = preprocess.prepare_source_pages(
                    source,
                    pdf_dpi=200,
                    deskew=False,
                    crop_margins=False,
                    max_dimension=None,
                )

            self.assertEqual(rendered_size, prepared[0].image.size)
            self.assertEqual(rendered_size, prepared[0].original_size)
            self.assertEqual(200, prepared[0].metadata["dpi"])
            self.assertNotIn("resized_to_max_dimension", prepared[0].metadata)

    def test_prepare_pages_pdf_cache_is_content_keyed_and_updates_source_pdf_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_source = root / "upload-a.pdf"
            second_source = root / "upload-b.pdf"
            first_source.write_bytes(b"%PDF-1.4 identical pdf bytes")
            second_source.write_bytes(b"%PDF-1.4 identical pdf bytes")
            out_dir = root / "out"

            def fake_render_pdf_pages(
                src: Path, output_dir: Path, dpi: int, write_files: bool = True
            ) -> list[preprocess.NormalizedPageImage]:
                return [self._rendered_pdf_page(src, output_dir)]

            with mock.patch.object(preprocess, "render_pdf_pages", side_effect=fake_render_pdf_pages) as render_mock:
                preprocess.prepare_pages(
                    first_source,
                    out_dir,
                    dpi=144,
                    enable_deskew=False,
                    enable_margin_crop=False,
                )
                second = preprocess.prepare_pages(
                    second_source,
                    out_dir,
                    dpi=144,
                    enable_deskew=False,
                    enable_margin_crop=False,
                )

            self.assertEqual(1, render_mock.call_count)
            self.assertIs(True, second[0].metadata["pdf_normalized_cache_hit"])
            self.assertEqual(str(second_source), second[0].metadata["source_pdf_path"])

    def test_prepare_pages_pdf_cache_misses_when_dpi_or_options_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4 option sensitive bytes")
            out_dir = root / "out"

            def fake_render_pdf_pages(
                src: Path, output_dir: Path, dpi: int, write_files: bool = True
            ) -> list[preprocess.NormalizedPageImage]:
                return [self._rendered_pdf_page(src, output_dir, size=(120, 160))]

            with mock.patch.object(preprocess, "render_pdf_pages", side_effect=fake_render_pdf_pages) as render_mock:
                first = preprocess.prepare_pages(
                    source,
                    out_dir,
                    dpi=144,
                    enable_deskew=False,
                    enable_margin_crop=False,
                )
                dpi_changed = preprocess.prepare_pages(
                    source,
                    out_dir,
                    dpi=200,
                    enable_deskew=False,
                    enable_margin_crop=False,
                )
                option_changed = preprocess.prepare_pages(
                    source,
                    out_dir,
                    dpi=144,
                    enable_deskew=False,
                    enable_margin_crop=False,
                    max_dimension=80,
                )

            self.assertEqual(3, render_mock.call_count)
            self.assertNotEqual(True, dpi_changed[0].metadata.get("pdf_normalized_cache_hit"))
            self.assertNotEqual(True, option_changed[0].metadata.get("pdf_normalized_cache_hit"))
            self.assertNotEqual(Path(first[0].normalized_path).parent, Path(dpi_changed[0].normalized_path).parent)
            self.assertNotEqual(Path(first[0].normalized_path).parent, Path(option_changed[0].normalized_path).parent)

    def test_prepare_source_pages_prefers_source_pdf_path_for_original_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_pdf = root / "exam.pdf"
            source_pdf.write_bytes(b"%PDF-1.4 original")
            cached_source = root / "out" / "rendered" / "exam-page-001.png"
            normalized_path = root / "out" / "normalized" / "exam-page-001.png"
            normalized_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (64, 88), "white").save(normalized_path)

            normalized_page = preprocess.NormalizedPageImage(
                page_id="exam-page-001",
                source_path=str(cached_source),
                normalized_path=str(normalized_path),
                page_index=0,
                width_px=64,
                height_px=88,
                metadata={
                    "source_type": "pdf",
                    "source_pdf_path": str(source_pdf),
                    "pdf_normalized_cache_hit": True,
                },
            )

            with mock.patch.object(preprocess, "prepare_pages", return_value=[normalized_page]) as prepare_mock:
                prepared = preprocess.prepare_source_pages(source_pdf, pdf_dpi=144)

            prepare_mock.assert_called_once()
            self.assertEqual(str(source_pdf.resolve()), prepared[0].metadata["original_source_path"])
            self.assertEqual(str(normalized_path.resolve()), prepared[0].metadata["normalized_path"])
            self.assertEqual(str(source_pdf), prepared[0].metadata["source_pdf_path"])


if __name__ == "__main__":
    unittest.main()
