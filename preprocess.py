#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from PIL import Image, ImageOps

from passage_detection import parse_shared_passage_range_header

try:
    import fitz  # type: ignore
except ImportError:  # pragma: no cover
    fitz = None

try:
    import olefile  # type: ignore
except ImportError:  # pragma: no cover
    olefile = None

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover
    cv2 = None

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover
    np = None


SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
HWP_DOCUMENT_EXTENSIONS = {".hwp", ".hwpx"}
HWP_RENDER_TREE_BASE_DPI = 72.0
PDF_NORMALIZED_CACHE_VERSION = 3
IMAGE_NORMALIZED_CACHE_VERSION = 1
HWP_NORMALIZED_CACHE_VERSION = 5
HWP_FAST_TEXT_SIGNAL_GOOD_ENOUGH = 20


def _configured_path(value: str | Path) -> Path:
    """Normalize a user-configured path without requiring shell expansion.

    Windows environment variables are commonly copied from registry or shell
    examples with surrounding quotes and ``%NAME%`` references. Passing those
    strings directly to :class:`Path` makes an otherwise valid executable look
    missing.
    """

    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    text = re.sub(
        r"%([^%]+)%",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        text,
    )
    return Path(os.path.expandvars(os.path.expanduser(text)))


def _path_identity(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _windows_application_roots() -> list[Path]:
    if not sys.platform.startswith("win"):
        return []
    roots: list[Path] = []
    seen: set[str] = set()
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        raw_root = os.environ.get(env_name, "").strip()
        if not raw_root:
            continue
        root = _configured_path(raw_root)
        key = _path_identity(root)
        if key not in seen:
            roots.append(root)
            seen.add(key)
    return roots


def _iter_libreoffice_executables() -> list[Path]:
    raw_candidates: list[str | Path | None] = [
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
    ]
    for root in _windows_application_roots():
        raw_candidates.append(root / "LibreOffice" / "program" / "soffice.exe")
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        raw_candidates.append(
            _configured_path(local_app_data)
            / "Programs"
            / "LibreOffice"
            / "program"
            / "soffice.exe"
        )

    candidates: list[Path] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not raw_candidate:
            continue
        candidate = _configured_path(raw_candidate)
        key = _path_identity(candidate)
        if key in seen or not candidate.is_file():
            continue
        candidates.append(candidate)
        seen.add(key)
    return candidates


@dataclass(slots=True)
class PreprocessOptions:
    dpi: int = 160
    enable_perspective: bool = True
    enable_deskew: bool = True
    enable_margin_crop: bool = True
    max_dimension: int | None = None


@dataclass(slots=True)
class PreparedPage:
    page_id: str
    source_path: str
    page_number: int
    image: Image.Image
    original_size: tuple[int, int]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size


@dataclass(slots=True)
class NormalizedPageImage:
    page_id: str
    source_path: str
    normalized_path: str
    page_index: int
    width_px: int
    height_px: int
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def image(self) -> Image.Image:
        return Image.open(self.normalized_path).convert("RGB")


def _require_cv2_numpy() -> None:
    if cv2 is None or np is None:
        raise RuntimeError("opencv-python and numpy are required for this preprocessing step")


def _pil_to_bgr(image: Image.Image):
    _require_cv2_numpy()
    rgb = image.convert("RGB")
    return cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)


def _bgr_to_pil(image_bgr) -> Image.Image:
    _require_cv2_numpy()
    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb)


_EXTERNAL_PYMUPDF_RENDER_SCRIPT = r"""
import json
import re
import sys
from pathlib import Path

import fitz


def looks_like_pdf_print_date_header(text):
    normalized = str(text or "").strip()
    if re.match(r"^[0-9]{2,4}\.\s*[0-9]{1,2}\.\s*[0-9]{1,2}\.", normalized):
        return True
    if ("오전" in normalized or "오후" in normalized or "AM" in normalized or "PM" in normalized):
        return normalized[:1].isdigit() and len(re.findall(r"[0-9]+", normalized)) >= 3
    return False


def looks_like_decimal_continuation(text, match):
    after_dot_index = match.end(1) + 1
    return after_dot_index < len(text) and text[after_dot_index].isdigit()


def extract_pdf_problem_markers(page, scale, data=None):
    markers = []
    if data is None:
        try:
            data = page.get_text("dict")
        except Exception:
            return markers
    for block in data.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for line in block.get("lines") or []:
            if not isinstance(line, dict):
                continue
            text = "".join(
                str(span.get("text") or "")
                for span in line.get("spans") or []
                if isinstance(span, dict)
            ).strip()
            if looks_like_pdf_print_date_header(text):
                continue
            match = re.match(r"^([1-9][0-9]?)\.\s*", text)
            if not match:
                continue
            if looks_like_decimal_continuation(text, match):
                continue
            bbox = line.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            number = int(match.group(1))
            left, top, right, bottom = [float(value) * scale for value in bbox]
            markers.append(
                {
                    "number": number,
                    "text": text[:120],
                    "bbox": {
                        "left": left,
                        "top": top,
                        "right": right,
                        "bottom": bottom,
                        "width": max(0.0, right - left),
                        "height": max(0.0, bottom - top),
                    },
                }
            )
    return markers


def extract_pdf_text_lines(page, scale, data=None):
    lines = []
    if data is None:
        try:
            data = page.get_text("dict")
        except Exception:
            return lines
    line_index = 0
    for block in data.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for line in block.get("lines") or []:
            if not isinstance(line, dict):
                continue
            text = "".join(
                str(span.get("text") or "")
                for span in line.get("spans") or []
                if isinstance(span, dict)
            ).strip()
            if not text:
                continue
            bbox = line.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            left, top, right, bottom = [float(value) * scale for value in bbox]
            lines.append(
                {
                    "line_index": line_index,
                    "text": text[:400],
                    "bbox": {
                        "left": left,
                        "top": top,
                        "right": right,
                        "bottom": bottom,
                        "width": max(0.0, right - left),
                        "height": max(0.0, bottom - top),
                    },
                }
            )
            line_index += 1
    return lines


def bbox_payload(raw_bbox, scale):
    left, top, right, bottom = [float(value) * scale for value in raw_bbox]
    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "width": max(0.0, right - left),
        "height": max(0.0, bottom - top),
    }


def extract_pdf_media_regions(page, scale, data=None):
    regions = []
    page_area = max(1.0, float(page.rect.width) * float(page.rect.height))
    if data is None:
        try:
            data = page.get_text("dict")
        except Exception:
            data = {}
    for block in data.get("blocks") or []:
        if not isinstance(block, dict) or block.get("type") != 1:
            continue
        bbox = block.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        width = max(0.0, float(bbox[2]) - float(bbox[0]))
        height = max(0.0, float(bbox[3]) - float(bbox[1]))
        area_ratio = width * height / page_area
        if width < 12.0 or height < 12.0 or area_ratio < 0.0005 or area_ratio > 0.25:
            continue
        regions.append(
            {
                "kind": "image",
                "source": "pdf_image_block",
                "confidence": 0.99,
                "bbox": bbox_payload(bbox, scale),
            }
        )
    try:
        tables = list(page.find_tables().tables)
    except Exception:
        tables = []
    for table in tables:
        bbox = table.bbox
        width = max(0.0, float(bbox[2]) - float(bbox[0]))
        height = max(0.0, float(bbox[3]) - float(bbox[1]))
        area_ratio = width * height / page_area
        cell_count = int(table.row_count) * int(table.col_count)
        if (
            int(table.row_count) < 2
            or int(table.col_count) < 2
            or cell_count < 6
            or width < 24.0
            or height < 24.0
            or area_ratio < 0.002
            or area_ratio > 0.15
        ):
            continue
        regions.append(
            {
                "kind": "table",
                "source": "pymupdf_table",
                "confidence": 0.94,
                "row_count": int(table.row_count),
                "column_count": int(table.col_count),
                "bbox": bbox_payload(bbox, scale),
            }
        )
    return regions


source_path = Path(sys.argv[1])
target_dir = Path(sys.argv[2])
dpi = int(sys.argv[3])
target_dir.mkdir(parents=True, exist_ok=True)
scale = dpi / 72.0
matrix = fitz.Matrix(scale, scale)
doc = fitz.open(source_path)
pages = []
try:
    for page_index in range(doc.page_count):
        page = doc.load_page(page_index)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        out_path = target_dir / f"{source_path.stem}_page_{page_index + 1:03d}.png"
        pix.save(out_path.as_posix())
        try:
            page_dict = page.get_text("dict")
        except Exception:
            page_dict = {}
        pages.append(
            {
                "page_id": f"{source_path.stem}-page-{page_index + 1:03d}",
                "source_path": str(source_path),
                "normalized_path": str(out_path),
                "page_index": page_index,
                "width_px": pix.width,
                "height_px": pix.height,
                "metadata": {
                    "source_type": "pdf",
                    "dpi": dpi,
                    "pdf_page_width_pt": float(page.rect.width),
                    "pdf_page_height_pt": float(page.rect.height),
                    "pdf_problem_markers": extract_pdf_problem_markers(page, scale, page_dict),
                    "pdf_text_lines": extract_pdf_text_lines(page, scale, page_dict),
                    "pdf_media_regions": extract_pdf_media_regions(page, scale, page_dict),
                },
            }
        )
finally:
    doc.close()
print(json.dumps(pages, ensure_ascii=True))
"""


def _iter_external_pymupdf_python_candidates() -> list[Path]:
    base_dir = Path(__file__).resolve().parent
    virtual_env = os.environ.get("VIRTUAL_ENV")
    raw_candidates: list[str | Path | None] = [
        os.environ.get("EDB_PYMUPDF_PYTHON"),
        sys.executable,
        Path(virtual_env) / "bin" / "python" if virtual_env else None,
        Path(virtual_env) / "Scripts" / "python.exe" if virtual_env else None,
        base_dir / ".venv" / "bin" / "python",
        base_dir / ".venv" / "bin" / "python3",
        base_dir / ".venv" / "Scripts" / "python.exe",
        Path.home() / "AppData" / "Local" / "Python" / "bin" / "python.exe",
        shutil.which("python"),
        shutil.which("python3"),
        shutil.which("py"),
    ]
    candidates: list[Path] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not raw_candidate:
            continue
        candidate = _configured_path(raw_candidate)
        key = _path_identity(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def _render_pdf_pages_with_external_pymupdf(
    source_path: Path,
    target_dir: Path,
    *,
    dpi: int,
) -> list[NormalizedPageImage]:
    errors: list[str] = []
    for python_exe in _iter_external_pymupdf_python_candidates():
        command = [
            str(python_exe),
            "-c",
            _EXTERNAL_PYMUPDF_RENDER_SCRIPT,
            str(source_path),
            str(target_dir),
            str(dpi),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{python_exe}: {exc}")
            continue
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().replace("\n", " ")[:240]
            errors.append(f"{python_exe}: exit {completed.returncode} {detail}")
            continue
        try:
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            errors.append(f"{python_exe}: invalid renderer output {exc}")
            continue

        pages: list[NormalizedPageImage] = []
        for item in payload:
            metadata = dict(item.get("metadata") or {})
            metadata["pdf_renderer"] = "external_pymupdf"
            metadata["pdf_renderer_python"] = str(python_exe)
            pages.append(
                NormalizedPageImage(
                    page_id=str(item["page_id"]),
                    source_path=str(item["source_path"]),
                    normalized_path=str(item["normalized_path"]),
                    page_index=int(item["page_index"]),
                    width_px=int(item["width_px"]),
                    height_px=int(item["height_px"]),
                    metadata=metadata,
                )
            )
        return pages

    detail = "; ".join(errors) if errors else "no Python candidates found"
    if len(detail) > 1200:
        detail = f"{detail[:1200]}..."
    raise RuntimeError(f"PyMuPDF is required to render PDF pages ({detail})")


def render_pdf_pages(source: str | Path, output_dir: str | Path, dpi: int = 160) -> list[NormalizedPageImage]:
    source_path = Path(source)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    if fitz is None:
        return _render_pdf_pages_with_external_pymupdf(source_path, target_dir, dpi=dpi)

    doc = fitz.open(source_path)
    pages: list[NormalizedPageImage] = []
    try:
        scale = dpi / 72.0
        matrix = fitz.Matrix(scale, scale)
        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            out_path = target_dir / f"{source_path.stem}_page_{page_index + 1:03d}.png"
            pix.save(out_path.as_posix())
            try:
                page_dict = page.get_text("dict")
            except Exception:
                page_dict = {}
            pages.append(
                NormalizedPageImage(
                    page_id=f"{source_path.stem}-page-{page_index + 1:03d}",
                    source_path=str(source_path),
                    normalized_path=str(out_path),
                    page_index=page_index,
                    width_px=pix.width,
                    height_px=pix.height,
                    metadata={
                        "source_type": "pdf",
                        "dpi": dpi,
                        "pdf_page_width_pt": float(page.rect.width),
                        "pdf_page_height_pt": float(page.rect.height),
                        "pdf_problem_markers": _extract_pdf_problem_markers(page, scale, page_dict),
                        "pdf_text_stem_markers": _extract_pdf_text_stem_markers(page, scale, page_dict),
                        "pdf_text_lines": _extract_pdf_text_lines(page, scale, page_dict),
                        "pdf_media_regions": _extract_pdf_media_regions(page, scale, page_dict),
                    },
                )
            )
    finally:
        doc.close()
    return pages


def _iter_rhwp_converter_commands() -> list[list[str]]:
    rhwp_candidates: list[str | Path | None] = [
        os.environ.get("EDB_RHWP"),
        shutil.which("rhwp"),
        Path(__file__).resolve().parent / ".app_runtime" / "rhwp" / "rhwp",
        Path(__file__).resolve().parent / ".app_runtime" / "rhwp" / "rhwp.exe",
    ]
    candidates: list[list[str]] = []
    seen: set[str] = set()
    for raw_candidate in rhwp_candidates:
        if not raw_candidate:
            continue
        candidate = _configured_path(raw_candidate)
        if not candidate.exists():
            continue
        key = _path_identity(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append([str(candidate)])
    return candidates


def _iter_rhwp_core_node_module_dirs() -> list[Path]:
    base_dir = Path(__file__).resolve().parent
    raw_candidates: list[str | Path | None] = [
        os.environ.get("EDB_RHWP_CORE_NODE_MODULES"),
        base_dir / ".app_runtime" / "rhwp_core" / "node_modules",
        base_dir / ".app_runtime" / "kordoc" / "node_modules",
        base_dir / "node_modules",
    ]
    candidates: list[Path] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not raw_candidate:
            continue
        for raw_part in str(raw_candidate).split(os.pathsep):
            if not raw_part:
                continue
            candidate = _configured_path(raw_part)
            key = _path_identity(candidate)
            if key in seen:
                continue
            seen.add(key)
            if (
                (candidate / "@rhwp" / "core" / "rhwp.js").exists()
                and (candidate / "@rhwp" / "core" / "rhwp_bg.wasm").exists()
                and (candidate / "sharp" / "package.json").exists()
            ):
                candidates.append(candidate)
    return candidates


def _iter_rhwp_core_renderer_commands() -> list[list[str]]:
    node = os.environ.get("EDB_NODE") or shutil.which("node")
    script_path = Path(__file__).resolve().parent / "scripts" / "render_hwp_with_rhwp_core.mjs"
    if not node or not script_path.exists():
        return []
    node_path = _configured_path(node)
    return [
        [str(node_path), str(script_path), "--node-modules", str(module_dir)]
        for module_dir in _iter_rhwp_core_node_module_dirs()
    ]


def _iter_hwp_pdf_converter_commands() -> list[list[str]]:
    candidates: list[list[str]] = [
        [str(executable), "--headless"]
        for executable in _iter_libreoffice_executables()
    ]

    hwp5pdf = shutil.which("hwp5pdf")
    if hwp5pdf:
        candidates.append([hwp5pdf])

    candidates.extend(_iter_rhwp_converter_commands())

    airun_candidates: list[str | Path | None] = [
        os.environ.get("EDB_AIRUN_HWP"),
        shutil.which("airun-hwp"),
        Path(__file__).resolve().parent / ".app_runtime" / "airun_hwp_env" / "bin" / "airun-hwp",
        Path(__file__).resolve().parent / ".app_runtime" / "airun_hwp_env" / "Scripts" / "airun-hwp.exe",
    ]
    for raw_candidate in airun_candidates:
        if not raw_candidate:
            continue
        candidate = _configured_path(raw_candidate)
        if candidate.exists():
            candidates.append([str(candidate)])

    seen: set[str] = set()
    unique: list[list[str]] = []
    for command in candidates:
        key = "\0".join(command)
        if key in seen:
            continue
        seen.add(key)
        unique.append(command)
    return unique


def _render_hwp_pages_with_rhwp_core(
    source: str | Path,
    output_dir: str | Path,
    *,
    dpi: int = 160,
    timeout_seconds: int = 90,
) -> list[NormalizedPageImage]:
    source_path = Path(source)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    cached_pages = _load_cached_hwp_core_pages(source_path, target_dir, dpi)
    if cached_pages:
        return cached_pages
    for command_prefix in _iter_rhwp_core_renderer_commands():
        command = [*command_prefix, str(source_path), str(target_dir), "--dpi", str(dpi)]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        try:
            payload = json.loads((result.stdout or "").strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            continue
        pages: list[NormalizedPageImage] = []
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            normalized_path = Path(str(item.get("normalized_path") or ""))
            if not normalized_path.exists():
                continue
            metadata = dict(item.get("metadata") or {})
            metadata.setdefault("source_type", "hwp")
            metadata.setdefault("document_like", True)
            metadata.setdefault("hwp_renderer", "rhwp-core")
            pages.append(
                NormalizedPageImage(
                    page_id=str(item.get("page_id") or f"{source_path.stem}-page-{len(pages) + 1:03d}"),
                    source_path=str(item.get("source_path") or source_path),
                    normalized_path=str(normalized_path),
                    page_index=int(item.get("page_index") or len(pages)),
                    width_px=int(item.get("width_px") or 0),
                    height_px=int(item.get("height_px") or 0),
                    metadata=metadata,
                )
            )
        if pages:
            _save_hwp_core_render_cache(source_path, target_dir, dpi, pages)
            return pages
    return []


def _render_hwp_pages_with_rhwp_python(
    source: str | Path,
    output_dir: str | Path,
    *,
    dpi: int = 160,
    timeout_seconds: int = 90,
) -> list[NormalizedPageImage]:
    source_path = Path(source)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    cached_pages = _load_cached_hwp_core_pages(source_path, target_dir, dpi, renderer="rhwp-python")
    if cached_pages:
        return cached_pages
    for command_prefix in _iter_rhwp_python_renderer_commands():
        command = [*command_prefix, str(source_path), str(target_dir), str(dpi)]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        try:
            payload = json.loads((result.stdout or "").strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list):
            continue
        pages: list[NormalizedPageImage] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            normalized_path = Path(str(item.get("normalized_path") or ""))
            if not normalized_path.exists():
                continue
            metadata = dict(item.get("metadata") or {})
            metadata.setdefault("source_type", "hwp")
            metadata.setdefault("document_like", True)
            metadata.setdefault("hwp_renderer", "rhwp-python")
            pages.append(
                NormalizedPageImage(
                    page_id=str(item.get("page_id") or f"{source_path.stem}-page-{len(pages) + 1:03d}"),
                    source_path=str(item.get("source_path") or source_path),
                    normalized_path=str(normalized_path),
                    page_index=int(item.get("page_index") if item.get("page_index") is not None else len(pages)),
                    width_px=int(item.get("width_px") or 0),
                    height_px=int(item.get("height_px") or 0),
                    metadata=metadata,
                )
            )
        if pages:
            _save_hwp_core_render_cache(source_path, target_dir, dpi, pages, renderer="rhwp-python")
            return pages
    return []


def _iter_hwp_hwpx_converter_commands() -> list[list[str]]:
    base_dir = Path(__file__).resolve().parent
    raw_candidates: list[str | Path | None] = [
        os.environ.get("EDB_HWPILOT"),
        shutil.which("hwpilot"),
        base_dir / ".app_runtime" / "hwpilot-src" / "dist" / "src" / "cli" / "main.js",
        base_dir / ".app_runtime" / "hwpilot" / "node_modules" / "hwpilot" / "dist" / "src" / "cli" / "main.js",
    ]
    raw_node = shutil.which("node")
    node = str(_configured_path(raw_node)) if raw_node else None
    commands: list[list[str]] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not raw_candidate:
            continue
        candidate = _configured_path(raw_candidate)
        if not candidate.exists():
            continue
        if candidate.suffix.lower() == ".js":
            if not node:
                continue
            command = [node, str(candidate)]
        else:
            command = [str(candidate)]
        key = "\0".join(command)
        if key in seen:
            continue
        seen.add(key)
        commands.append(command)
    return commands


def _iter_pyhwp_html_converter_commands() -> list[list[str]]:
    raw_candidates: list[str | Path | None] = [
        os.environ.get("EDB_HWP5HTML"),
        Path(sys.executable).resolve().parent / "hwp5html",
        Path(sys.executable).resolve().parent / "hwp5html.exe",
        shutil.which("hwp5html"),
        Path(__file__).resolve().parent / ".app_runtime" / "pyhwp_env" / "bin" / "hwp5html",
        Path(__file__).resolve().parent / ".app_runtime" / "pyhwp_env" / "Scripts" / "hwp5html.exe",
    ]
    candidates: list[list[str]] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not raw_candidate:
            continue
        candidate = _configured_path(raw_candidate)
        if not candidate.exists():
            continue
        key = _path_identity(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append([str(candidate)])
    return candidates


def _iter_pyhwp_text_converter_commands() -> list[list[str]]:
    raw_candidates: list[str | Path | None] = [
        os.environ.get("EDB_HWP5TXT"),
        Path(sys.executable).resolve().parent / "hwp5txt",
        Path(sys.executable).resolve().parent / "hwp5txt.exe",
        shutil.which("hwp5txt"),
        Path(__file__).resolve().parent / ".app_runtime" / "pyhwp_env" / "bin" / "hwp5txt",
        Path(__file__).resolve().parent / ".app_runtime" / "pyhwp_env" / "Scripts" / "hwp5txt.exe",
    ]
    candidates: list[list[str]] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not raw_candidate:
            continue
        candidate = _configured_path(raw_candidate)
        if not candidate.exists():
            continue
        key = _path_identity(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append([str(candidate)])
    return candidates


def _iter_hwpilot_text_converter_commands() -> list[list[str]]:
    return _iter_hwp_hwpx_converter_commands()


def _iter_kordoc_text_converter_commands() -> list[list[str]]:
    base_dir = Path(__file__).resolve().parent
    raw_candidates: list[str | Path | None] = [
        os.environ.get("EDB_KORDOC"),
        shutil.which("kordoc"),
        base_dir / ".app_runtime" / "kordoc" / "node_modules" / ".bin" / "kordoc",
        base_dir / ".app_runtime" / "kordoc" / "node_modules" / ".bin" / "kordoc.cmd",
    ]
    candidates: list[list[str]] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not raw_candidate:
            continue
        candidate = _configured_path(raw_candidate)
        if not candidate.exists():
            continue
        key = _path_identity(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append([str(candidate)])
    return candidates


_UNHWP_EXTRACT_TEXT_SCRIPT = (
    "import sys, unhwp; "
    "sys.stdout.write((unhwp.extract_text(sys.argv[1]) or '').replace('\\x00', ''))"
)

_HWP_HWPX_PARSER_EXTRACT_TEXT_SCRIPT = """
import sys
from hwp_hwpx_parser import Reader
with Reader(sys.argv[1]) as reader:
    text = reader.text or ""
    if not text:
        result = reader.extract_text_with_notes()
        text = result.text if hasattr(result, "text") else str(result or "")
sys.stdout.write((text or "").replace("\\x00", ""))
""".strip()

_RHWP_PYTHON_EXTRACT_TEXT_SCRIPT = """
import sys
import rhwp
doc = rhwp.parse(sys.argv[1])
text = doc.extract_text() if hasattr(doc, "extract_text") else ""
sys.stdout.write((text or "").replace("\\x00", ""))
""".strip()

_RHWP_PYTHON_RENDER_PNG_SCRIPT = """
import json
import struct
import sys
from pathlib import Path

import rhwp


def png_size(path):
    with open(path, "rb") as fh:
        header = fh.read(24)
    if len(header) < 24 or header[:8] != b"\\x89PNG\\r\\n\\x1a\\n":
        return 0, 0
    return struct.unpack(">II", header[16:24])


source_path = Path(sys.argv[1])
target_dir = Path(sys.argv[2])
dpi = int(sys.argv[3])
target_dir.mkdir(parents=True, exist_ok=True)
doc = rhwp.parse(str(source_path))
written = doc.export_png(str(target_dir), prefix=f"{source_path.stem}_page")
pages = []
for index, raw_path in enumerate(written):
    page_path = Path(raw_path)
    width, height = png_size(page_path)
    pages.append(
        {
            "page_id": f"{source_path.stem}-page-{index + 1:03d}",
            "source_path": str(source_path),
            "normalized_path": str(page_path),
            "page_index": index,
            "width_px": width,
            "height_px": height,
            "metadata": {
                "source_type": "hwp",
                "document_like": True,
                "dpi": dpi,
                "hwp_renderer": "rhwp-python",
                "hwp_renderer_version": rhwp.version(),
                "hwp_renderer_core_version": rhwp.rhwp_core_version(),
                "hwp_renderer_page_count": len(written),
            },
        }
    )
print(json.dumps(pages, ensure_ascii=False))
""".strip()


def _python_can_import_unhwp(python_path: Path) -> bool:
    try:
        result = subprocess.run(
            [str(python_path), "-c", "import unhwp"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _python_can_import_hwp_hwpx_parser(python_path: Path) -> bool:
    try:
        result = subprocess.run(
            [str(python_path), "-c", "import hwp_hwpx_parser"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _python_can_import_rhwp_python(python_path: Path) -> bool:
    try:
        result = subprocess.run(
            [str(python_path), "-c", "import rhwp"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _iter_unhwp_text_converter_commands() -> list[list[str]]:
    raw_candidates: list[str | Path | None] = [
        os.environ.get("EDB_UNHWP_PYTHON"),
        Path(__file__).resolve().parent / ".app_runtime" / "unhwp_probe_env" / "bin" / "python",
        Path(__file__).resolve().parent / ".app_runtime" / "hwp_extra_probe_env" / "bin" / "python",
        Path(__file__).resolve().parent / ".app_runtime" / "unhwp_probe_env" / "Scripts" / "python.exe",
        Path(__file__).resolve().parent / ".app_runtime" / "hwp_extra_probe_env" / "Scripts" / "python.exe",
        sys.executable,
    ]
    candidates: list[list[str]] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not raw_candidate:
            continue
        candidate = _configured_path(raw_candidate)
        if not candidate.exists():
            continue
        key = _path_identity(candidate)
        if key in seen or not _python_can_import_unhwp(candidate):
            continue
        seen.add(key)
        candidates.append([str(candidate), "-c", _UNHWP_EXTRACT_TEXT_SCRIPT])
    return candidates


def _iter_hwp_hwpx_parser_text_converter_commands() -> list[list[str]]:
    base_dir = Path(__file__).resolve().parent
    raw_candidates: list[str | Path | None] = [
        os.environ.get("EDB_HWP_HWPX_PARSER_PYTHON"),
        base_dir / ".app_runtime" / "hwp_extra_probe_env" / "bin" / "python",
        base_dir / ".app_runtime" / "hwp_extra_probe_env" / "Scripts" / "python.exe",
        sys.executable,
    ]
    candidates: list[list[str]] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not raw_candidate:
            continue
        candidate = _configured_path(raw_candidate)
        if not candidate.exists():
            continue
        key = _path_identity(candidate)
        if key in seen or not _python_can_import_hwp_hwpx_parser(candidate):
            continue
        seen.add(key)
        candidates.append([str(candidate), "-c", _HWP_HWPX_PARSER_EXTRACT_TEXT_SCRIPT])
    return candidates


def _iter_rhwp_python_text_converter_commands() -> list[list[str]]:
    base_dir = Path(__file__).resolve().parent
    raw_candidates: list[str | Path | None] = [
        os.environ.get("EDB_RHWP_PYTHON"),
        base_dir / ".app_runtime" / "rhwp_python_probe_env" / "bin" / "python",
        base_dir / ".app_runtime" / "hwp_extra_probe_env" / "bin" / "python",
        base_dir / ".app_runtime" / "rhwp_python_probe_env" / "Scripts" / "python.exe",
        base_dir / ".app_runtime" / "hwp_extra_probe_env" / "Scripts" / "python.exe",
        sys.executable,
    ]
    candidates: list[list[str]] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not raw_candidate:
            continue
        candidate = _configured_path(raw_candidate)
        if not candidate.exists():
            continue
        key = _path_identity(candidate)
        if key in seen or not _python_can_import_rhwp_python(candidate):
            continue
        seen.add(key)
        candidates.append([str(candidate), "-c", _RHWP_PYTHON_EXTRACT_TEXT_SCRIPT])
    return candidates


def _iter_rhwp_python_renderer_commands() -> list[list[str]]:
    base_dir = Path(__file__).resolve().parent
    raw_candidates: list[str | Path | None] = [
        os.environ.get("EDB_RHWP_PYTHON"),
        base_dir / ".app_runtime" / "rhwp_python_probe_env" / "bin" / "python",
        base_dir / ".app_runtime" / "hwp_extra_probe_env" / "bin" / "python",
        base_dir / ".app_runtime" / "rhwp_python_probe_env" / "Scripts" / "python.exe",
        base_dir / ".app_runtime" / "hwp_extra_probe_env" / "Scripts" / "python.exe",
        sys.executable,
    ]
    candidates: list[list[str]] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not raw_candidate:
            continue
        candidate = _configured_path(raw_candidate)
        if not candidate.exists():
            continue
        key = _path_identity(candidate)
        if key in seen or not _python_can_import_rhwp_python(candidate):
            continue
        seen.add(key)
        candidates.append([str(candidate), "-c", _RHWP_PYTHON_RENDER_PNG_SCRIPT])
    return candidates


def _iter_hwp_text_converter_commands() -> list[list[str]]:
    return [
        *_iter_pyhwp_text_converter_commands(),
        *_iter_unhwp_text_converter_commands(),
        *_iter_hwp_hwpx_parser_text_converter_commands(),
        *_iter_rhwp_python_text_converter_commands(),
        *_iter_rhwp_converter_commands(),
        *_iter_hwpilot_text_converter_commands(),
        *_iter_kordoc_text_converter_commands(),
    ]


def _iter_chrome_pdf_commands() -> list[list[str]]:
    raw_candidates: list[str | Path | None] = [
        os.environ.get("EDB_CHROME"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chrome"),
        shutil.which("msedge"),
    ]
    for root in _windows_application_roots():
        raw_candidates.extend(
            [
                root / "Google" / "Chrome" / "Application" / "chrome.exe",
                root / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        )
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        local_root = _configured_path(local_app_data)
        raw_candidates.extend(
            [
                local_root / "Programs" / "Google" / "Chrome" / "Application" / "chrome.exe",
                local_root / "Programs" / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        )
    candidates: list[list[str]] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not raw_candidate:
            continue
        candidate = _configured_path(raw_candidate)
        if not candidate.exists():
            continue
        key = _path_identity(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append([str(candidate)])
    return candidates


def _hwp_pdf_candidates(output_dir: Path, source_path: Path) -> list[Path]:
    expected = output_dir / f"{source_path.stem}.pdf"
    nested_expected = output_dir / source_path.stem / f"{source_path.stem}.pdf"
    candidates = [expected]
    if nested_expected not in candidates:
        candidates.append(nested_expected)
    for path in sorted(
        output_dir.glob("*.pdf"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    ):
        if path not in candidates:
            candidates.append(path)
    return candidates


def _stage_airun_hwp_source(source_path: Path, target_dir: Path) -> Path:
    staging_dir = target_dir / source_path.stem
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged_path = staging_dir / source_path.name
    try:
        same_file = source_path.resolve() == staged_path.resolve()
    except OSError:
        same_file = False
    if not same_file:
        shutil.copyfile(source_path, staged_path)
    return staged_path


def _airun_hwp_env(target_dir: Path) -> dict[str, str] | None:
    if sys.platform.startswith("win"):
        # airun-hwp invokes the literal ``libreoffice`` command with
        # subprocess and shell=False. A .cmd shim is therefore not a valid
        # CreateProcess target on Windows. Standard Windows LibreOffice
        # installations are handled earlier by the direct soffice.exe
        # converter; leave airun's own fallback selection unchanged here.
        return None

    libreoffice = shutil.which("libreoffice")
    if libreoffice:
        return None

    soffice = shutil.which("soffice")
    installations = _iter_libreoffice_executables() if not soffice else []
    if not soffice and not installations:
        return None
    if not soffice:
        soffice = str(installations[0])

    shim_dir = target_dir / "_airun_bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_path = shim_dir / "libreoffice"
    if not shim_path.exists():
        soffice_path = Path(str(soffice))
        if soffice_path.exists():
            try:
                shim_path.symlink_to(soffice)
            except OSError:
                shim_path.write_text(
                    f"#!/bin/sh\nexec {json.dumps(str(soffice))} \"$@\"\n",
                    encoding="utf-8",
                )
                shim_path.chmod(0o755)
        else:
            shim_path.write_text(
                f"#!/bin/sh\nexec {json.dumps(str(soffice))} \"$@\"\n",
                encoding="utf-8",
            )
            shim_path.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def _hwp_property_flags(properties: int) -> dict[str, bool]:
    return {
        "compressed": bool(properties & 0x01),
        "password": bool(properties & 0x02),
        "distribution": bool(properties & 0x04),
        "script": bool(properties & 0x08),
        "drm": bool(properties & 0x10),
        "xml_template": bool(properties & 0x20),
        "history": bool(properties & 0x40),
        "signed": bool(properties & 0x80),
        "encrypted_cert": bool(properties & 0x100),
        "copy_protection": bool(properties & 0x200),
    }


def _extract_hwp_text_with_pyhwp(source: str | Path, timeout_seconds: int = 15) -> str:
    source_path = Path(source)
    for command_prefix in _iter_pyhwp_text_converter_commands():
        command = [*command_prefix, str(source_path)]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = (result.stdout or "").replace("\x00", "").strip()
        if result.returncode == 0 and text:
            return text
    return ""


def _extract_hwp_text_with_unhwp(source: str | Path, timeout_seconds: int = 15) -> str:
    source_path = Path(source)
    for command_prefix in _iter_unhwp_text_converter_commands():
        command = [*command_prefix, str(source_path)]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = (result.stdout or "").replace("\x00", "").strip()
        if result.returncode == 0 and text:
            return text
    return ""


def _extract_hwp_text_with_hwp_hwpx_parser(source: str | Path, timeout_seconds: int = 15) -> str:
    source_path = Path(source)
    for command_prefix in _iter_hwp_hwpx_parser_text_converter_commands():
        command = [*command_prefix, str(source_path)]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = (result.stdout or "").replace("\x00", "").strip()
        if result.returncode == 0 and text:
            return text
    return ""


def _extract_hwp_text_with_rhwp_python(source: str | Path, timeout_seconds: int = 15) -> str:
    source_path = Path(source)
    for command_prefix in _iter_rhwp_python_text_converter_commands():
        command = [*command_prefix, str(source_path)]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = (result.stdout or "").replace("\x00", "").strip()
        if result.returncode == 0 and text:
            return text
    return ""


def _extract_hwp_text_with_rhwp(source: str | Path, timeout_seconds: int = 15) -> str:
    source_path = Path(source)
    for command_prefix in _iter_rhwp_converter_commands():
        with tempfile.TemporaryDirectory(prefix="edb-rhwp-text-") as raw_temp_dir:
            output_dir = Path(raw_temp_dir)
            command = [*command_prefix, "export-text", str(source_path), "-o", str(output_dir)]
            try:
                subprocess.run(
                    command,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            chunks: list[str] = []
            for txt_path in sorted(output_dir.glob("*.txt")):
                try:
                    text = txt_path.read_text(encoding="utf-8", errors="ignore").replace("\x00", "").strip()
                except OSError:
                    continue
                if text:
                    chunks.append(text)
            extracted = "\n".join(chunks).strip()
            if extracted:
                return extracted
    return ""


def _extract_hwp_markdown_with_rhwp(source: str | Path, timeout_seconds: int = 15) -> str:
    source_path = Path(source)
    for command_prefix in _iter_rhwp_converter_commands():
        with tempfile.TemporaryDirectory(prefix="edb-rhwp-markdown-") as raw_temp_dir:
            output_dir = Path(raw_temp_dir)
            command = [*command_prefix, "export-markdown", str(source_path), "-o", str(output_dir)]
            try:
                result = subprocess.run(
                    command,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode != 0:
                continue
            chunks: list[str] = []
            for md_path in sorted(output_dir.glob("*.md")):
                try:
                    text = md_path.read_text(encoding="utf-8", errors="ignore").replace("\x00", "").strip()
                except OSError:
                    continue
                if text:
                    chunks.append(text)
            extracted = "\n".join(chunks).strip()
            if extracted:
                return extracted
    return ""


def _extract_hwp_text_with_hwpilot(source: str | Path, timeout_seconds: int = 15) -> str:
    source_path = Path(source)
    env = {**os.environ, "HWPILOT_NO_DAEMON": "1"}
    for command_prefix in _iter_hwpilot_text_converter_commands():
        command = [*command_prefix, "text", str(source_path)]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        raw_text = (result.stdout or "").replace("\x00", "").strip()
        if result.returncode != 0 or not raw_text:
            continue
        text = raw_text
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            text = str(payload.get("text") or "").replace("\x00", "").strip()
        if text:
            return text
    return ""


def _extract_hwp_text_with_kordoc(source: str | Path, timeout_seconds: int = 30) -> str:
    source_path = Path(source)
    for command_prefix in _iter_kordoc_text_converter_commands():
        command = [*command_prefix, str(source_path)]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = (result.stdout or "").replace("\x00", "").strip()
        if result.returncode == 0 and text:
            return text
    return ""


def _extract_hwp_image_summary_with_hwpilot(source: str | Path, timeout_seconds: int = 15) -> dict[str, Any]:
    source_path = Path(source)
    env = {**os.environ, "HWPILOT_NO_DAEMON": "1"}
    for command_prefix in _iter_hwpilot_text_converter_commands():
        command = [*command_prefix, "image", "list", str(source_path)]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        raw_text = (result.stdout or "").replace("\x00", "").strip()
        if result.returncode != 0 or not raw_text:
            continue
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, list):
            continue
        images = [item for item in payload if isinstance(item, dict)]
        formats = sorted(
            {
                str(item.get("format") or "").strip().lower()
                for item in images
                if str(item.get("format") or "").strip()
            }
        )
        widths = [int(item.get("width") or 0) for item in images if isinstance(item.get("width"), (int, float))]
        heights = [int(item.get("height") or 0) for item in images if isinstance(item.get("height"), (int, float))]
        return {
            "hwp_image_extractor": "hwpilot",
            "hwp_image_count": len(images),
            "hwp_image_formats": formats,
            "hwp_image_max_width": max(widths) if widths else 0,
            "hwp_image_max_height": max(heights) if heights else 0,
        }
    return {}


def _iter_render_tree_objects(node: Any):
    if not isinstance(node, dict):
        return
    yield node
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            yield from _iter_render_tree_objects(child)


def _render_tree_text_line_text(node: dict[str, Any]) -> str:
    raw_text = node.get("text")
    if isinstance(raw_text, str) and raw_text:
        return raw_text
    chunks: list[str] = []
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            if not isinstance(child, dict):
                continue
            if child.get("type") == "TextRun" and isinstance(child.get("text"), str):
                chunks.append(str(child.get("text") or ""))
    return "".join(chunks)


def _summarize_rhwp_render_tree_pages(pages: list[dict[str, Any]]) -> dict[str, Any]:
    problem_markers: list[dict[str, Any]] = []
    text_line_count = 0
    text_run_count = 0
    problem_numbers: list[int] = []
    for page_index, page in enumerate(pages):
        for node in _iter_render_tree_objects(page):
            node_type = node.get("type")
            if node_type == "TextRun" and str(node.get("text") or "").strip():
                text_run_count += 1
            if node_type != "TextLine":
                continue
            line = re.sub(r"\s+", " ", _render_tree_text_line_text(node)).strip()
            if not line or _looks_like_pdf_print_date_header(line):
                continue
            text_line_count += 1
            match = re.match(r"^([1-9][0-9]?)\s*[\.)]\s*", line)
            if not match:
                continue
            number = int(match.group(1))
            problem_numbers.append(number)
            marker = {
                "pageIndex": page_index,
                "number": number,
                "text": line[:120],
            }
            bbox = node.get("bbox")
            if isinstance(bbox, dict):
                marker["bbox"] = {
                    key: float(bbox.get(key) or 0.0)
                    for key in ("x", "y", "w", "h")
                    if key in bbox
                }
            problem_markers.append(marker)
    return {
        "hwp_layout_extractor": "rhwp-render-tree",
        "hwp_layout_page_count": len(pages),
        "hwp_layout_problem_marker_count": len(problem_markers),
        "hwp_layout_text_line_count": text_line_count,
        "hwp_layout_text_run_count": text_run_count,
        "hwp_layout_problem_numbers": problem_numbers,
        "hwp_layout_problem_markers": problem_markers[:100],
    }


def _hwp_layout_problem_markers_for_page(
    conversion_quality: dict[str, Any],
    *,
    page_index: int,
    dpi: int,
) -> list[dict[str, Any]]:
    raw_markers = conversion_quality.get("hwp_layout_problem_markers")
    if not isinstance(raw_markers, list):
        return []

    scale = float(dpi) / HWP_RENDER_TREE_BASE_DPI
    markers: list[dict[str, Any]] = []
    for raw_marker in raw_markers:
        if not isinstance(raw_marker, dict):
            continue
        if int(raw_marker.get("pageIndex", -1)) != int(page_index):
            continue
        bbox = raw_marker.get("bbox")
        if not isinstance(bbox, dict):
            continue
        try:
            number = int(raw_marker.get("number"))
            left = float(bbox.get("x", bbox.get("left", 0.0))) * scale
            top = float(bbox.get("y", bbox.get("top", 0.0))) * scale
            width = float(bbox.get("w", bbox.get("width", 0.0))) * scale
            height = float(bbox.get("h", bbox.get("height", 0.0))) * scale
        except (TypeError, ValueError):
            continue
        if number < 1 or width <= 0.0 or height <= 0.0:
            continue
        right = left + width
        bottom = top + height
        markers.append(
            {
                "number": number,
                "text": str(raw_marker.get("text") or f"{number}.")[:120],
                "bbox": {
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                    "width": width,
                    "height": height,
                },
                "marker_kind": "hwp_layout_number",
                "source": "hwp_layout_marker",
                "source_page_index": int(page_index),
            }
        )
    return markers


def _attach_hwp_layout_problem_markers(
    metadata: dict[str, Any],
    *,
    conversion_quality: dict[str, Any],
    page_index: int,
    dpi: int,
) -> None:
    markers = _hwp_layout_problem_markers_for_page(
        conversion_quality,
        page_index=page_index,
        dpi=dpi,
    )
    if not markers:
        return
    metadata["pdf_problem_markers"] = markers
    metadata["hwp_layout_problem_markers_as_pdf_markers"] = True
    metadata["hwp_layout_problem_marker_count_on_page"] = len(markers)


def _extract_hwp_render_tree_summary_with_rhwp(source: str | Path, timeout_seconds: int = 30) -> dict[str, Any]:
    source_path = Path(source)
    for command_prefix in _iter_rhwp_converter_commands():
        with tempfile.TemporaryDirectory(prefix="edb-rhwp-render-tree-") as raw_temp_dir:
            output_dir = Path(raw_temp_dir)
            command = [*command_prefix, "export-render-tree", str(source_path), "-o", str(output_dir)]
            try:
                result = subprocess.run(
                    command,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode != 0:
                continue
            pages: list[dict[str, Any]] = []
            for json_path in sorted(output_dir.glob("*.json")):
                try:
                    payload = json.loads(json_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict):
                    pages.append(payload)
            if pages:
                return _summarize_rhwp_render_tree_pages(pages)
    return {}


def _summarize_hwp_text_problem_signals(text: str) -> dict[str, int]:
    numbered_count = 0
    stem_count = 0
    for raw_line in str(text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if re.match(r"^[1-9][0-9]?\s*[\.)]", line):
            numbered_count += 1
        elif _looks_like_pdf_problem_stem_line(line):
            stem_count += 1
    return {
        "hwp_text_numbered_problem_count": numbered_count,
        "hwp_text_stem_problem_count": stem_count,
    }


HWP_TEXT_PROBLEM_SNIPPET_MAX_CHARS = 2400
HWP_TEXT_PROBLEM_SNIPPET_MAX_COUNT = 120
HWP_TEXT_PROBLEM_START_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?P<number>[1-9][0-9]?)\s*(?:[\.)][\s\-.]*|[-–—]\s*)"
)
HWP_TEXT_PASSAGE_RANGE_MAX_COUNT = 120
HWP_TEXT_PASSAGE_RANGE_BRACKET_RE = re.compile(
    r"^\s*[\[（(<]"
    r"(?P<start>[0-9]{1,3})\s*[~\-]\s*(?P<end>[0-9]{1,3})\s*(?:번)?"
    r"[\]）)>]"
)
HWP_TEXT_PASSAGE_RANGE_KOREAN_RE = re.compile(
    r"^\s*(?:제\s*)?(?P<start>[0-9]{1,3})\s*(?:번\s*)?"
    r"(?:[~\-]|부터|에서)\s*"
    r"(?:제\s*)?(?P<end>[0-9]{1,3})\s*번(?:까지)?"
)
HWP_TEXT_PASSAGE_RANGE_COMPACT_RE = re.compile(
    r"^\s*(?:(?:문항|문제|questions?)\s*)?"
    r"(?P<start>[0-9]{1,3})\s*[~\-\u2010-\u2015]\s*(?P<end>[0-9]{1,3})\s*(?:번)?",
    re.IGNORECASE,
)
HWP_TEXT_PASSAGE_RANGE_CUES = (
    "다음",
    "글",
    "자료",
    "지문",
    "대화",
    "담화",
    "발표",
    "작품",
    "도표",
    "그림",
    "실험",
    "보기",
    "읽고",
    "보고",
    "물음",
    "답하시오",
    "following",
    "read",
    "passage",
    "text",
    "questions",
    "conversation",
    "dialogue",
    "article",
    "chart",
    "graph",
)


def _extract_hwp_numbered_problem_snippets(
    text: str,
    *,
    max_chars: int = HWP_TEXT_PROBLEM_SNIPPET_MAX_CHARS,
    max_count: int = HWP_TEXT_PROBLEM_SNIPPET_MAX_COUNT,
) -> list[dict[str, Any]]:
    snippets: list[dict[str, Any]] = []
    current_number: int | None = None
    current_lines: list[str] = []

    def flush_current() -> None:
        nonlocal current_number, current_lines
        if current_number is None:
            return
        snippet = "\n".join(line for line in current_lines if line).strip()
        if snippet:
            snippets.append(
                {
                    "number": current_number,
                    "text": snippet[: max(1, int(max_chars))],
                    "char_count": len(snippet),
                    "truncated": len(snippet) > int(max_chars),
                }
            )
        current_number = None
        current_lines = []

    for raw_line in str(text or "").replace("\r", "\n").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        match = HWP_TEXT_PROBLEM_START_RE.match(line)
        if match:
            try:
                number = int(match.group("number"))
            except (TypeError, ValueError):
                number = 0
            if 1 <= number <= 99:
                flush_current()
                if len(snippets) >= int(max_count):
                    return snippets
                current_number = number
                current_lines = [line]
                continue
        if current_number is not None:
            current_lines.append(line)

    flush_current()
    return snippets[: max(0, int(max_count))]


def _extract_hwp_passage_ranges(
    text: str,
    *,
    max_count: int = HWP_TEXT_PASSAGE_RANGE_MAX_COUNT,
) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for raw_line in str(text or "").replace("\r", "\n").splitlines():
        line = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw_line)).strip()
        if not line:
            continue
        header = parse_shared_passage_range_header(line)
        if header is None:
            continue
        start, end = header.start, header.end
        key = (start, end)
        if key in seen:
            continue
        seen.add(key)
        ranges.append({"start": start, "end": end, "text": line[:180]})
        if len(ranges) >= int(max_count):
            break
    return ranges


def _hwp_text_problem_signal_score(text: str) -> int:
    signals = _summarize_hwp_text_problem_signals(text)
    numbered = int(signals.get("hwp_text_numbered_problem_count") or 0)
    stem = int(signals.get("hwp_text_stem_problem_count") or 0)
    return numbered if numbered > 0 else stem


def inspect_hwp_document(source: str | Path) -> dict[str, Any]:
    source_path = Path(source)
    inspection: dict[str, Any] = {
        "ole_file": False,
        "hwp_signature": None,
        "hwp_flags": {},
        "hwp_section_count": 0,
        "hwp_preview_text_length": 0,
    }
    if olefile is None:
        inspection["inspection_error"] = "olefile_unavailable"
        return inspection

    try:
        is_ole = bool(olefile.isOleFile(source_path))
    except OSError as exc:
        inspection["inspection_error"] = str(exc)
        return inspection
    inspection["ole_file"] = is_ole
    if not is_ole:
        return inspection

    ole = None
    try:
        ole = olefile.OleFileIO(source_path)
        streams = ["/".join(parts) for parts in ole.listdir(streams=True, storages=False)]
        inspection["hwp_section_count"] = sum(1 for stream in streams if stream.startswith("BodyText/Section"))

        full_preview_text = ""
        if ole.exists("FileHeader"):
            header = ole.openstream("FileHeader").read(256)
            signature = header[:32].rstrip(b"\0").decode("latin1", errors="replace").strip()
            inspection["hwp_signature"] = signature
            if len(header) >= 40:
                inspection["hwp_version_raw"] = struct.unpack("<I", header[32:36])[0]
                properties = struct.unpack("<I", header[36:40])[0]
                inspection["hwp_properties"] = properties
                inspection["hwp_flags"] = _hwp_property_flags(properties)

        if ole.exists("PrvText"):
            data = ole.openstream("PrvText").read()
            for encoding in ("utf-16le", "utf-8", "cp949"):
                text = data.decode(encoding, errors="ignore").replace("\x00", "").strip()
                if text:
                    inspection["hwp_preview_text_length"] = len(text)
                    inspection["hwp_preview_text"] = text[:4000]
                    full_preview_text = text
                    break
    except Exception as exc:
        inspection["inspection_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if ole is not None:
            ole.close()
    signal_text = full_preview_text if "full_preview_text" in locals() else ""
    if inspection.get("hwp_signature") == "HWP Document File":
        fast_extractors = [
            ("hwp5txt", _extract_hwp_text_with_pyhwp),
            ("unhwp", _extract_hwp_text_with_unhwp),
            ("hwp-hwpx-parser", _extract_hwp_text_with_hwp_hwpx_parser),
            ("rhwp-python", _extract_hwp_text_with_rhwp_python),
        ]
        slow_extractors = [
            ("rhwp", _extract_hwp_text_with_rhwp),
            ("rhwp-markdown", _extract_hwp_markdown_with_rhwp),
            ("hwpilot", _extract_hwp_text_with_hwpilot),
            ("kordoc", _extract_hwp_text_with_kordoc),
        ]
        inspection.update(_extract_hwp_render_tree_summary_with_rhwp(source_path))
        inspection.update(_extract_hwp_image_summary_with_hwpilot(source_path))
        existing_length = int(inspection.get("hwp_preview_text_length") or 0)
        best_name = ""
        best_text = ""
        best_score = -1
        scored_candidates: list[tuple[str, str, int]] = []
        for name, extractor in fast_extractors:
            text = extractor(source_path)
            if not text:
                continue
            score = _hwp_text_problem_signal_score(text)
            scored_candidates.append((name, text, score))

        fast_best_score = max((score for _name, _text, score in scored_candidates), default=0)
        if fast_best_score < HWP_FAST_TEXT_SIGNAL_GOOD_ENOUGH:
            for name, extractor in slow_extractors:
                text = extractor(source_path)
                if not text:
                    continue
                score = _hwp_text_problem_signal_score(text)
                scored_candidates.append((name, text, score))

        non_rhwp_candidates = [row for row in scored_candidates if row[0] != "rhwp"]
        rhwp_candidates = [row for row in scored_candidates if row[0] == "rhwp"]
        if non_rhwp_candidates and rhwp_candidates:
            non_rhwp_best = max(non_rhwp_candidates, key=lambda row: (row[2], len(row[1])))
            rhwp_best = max(rhwp_candidates, key=lambda row: (row[2], len(row[1])))
            if non_rhwp_best[2] >= 10 and rhwp_best[2] > int(non_rhwp_best[2] * 1.25):
                scored_candidates = non_rhwp_candidates

        for name, text, score in scored_candidates:
            if (score, len(text)) > (best_score, len(best_text)):
                best_name = name
                best_text = text
                best_score = score
        if best_text and len(best_text) > existing_length:
            inspection["hwp_preview_text_length"] = len(best_text)
            inspection["hwp_preview_text"] = best_text[:4000]
            inspection["hwp_text_extractor"] = best_name
            signal_text = best_text
    if not signal_text:
        signal_text = str(inspection.get("hwp_preview_text") or "")
    if signal_text:
        inspection.update(_summarize_hwp_text_problem_signals(signal_text))
        problem_snippets = _extract_hwp_numbered_problem_snippets(signal_text)
        if problem_snippets:
            inspection["hwp_text_problem_snippet_count"] = len(problem_snippets)
            inspection["hwp_text_problem_snippets"] = problem_snippets
        passage_ranges = _extract_hwp_passage_ranges(signal_text)
        if passage_ranges:
            inspection["hwp_text_passage_range_count"] = len(passage_ranges)
            inspection["hwp_text_passage_ranges"] = passage_ranges
    return inspection


def inspect_hwpx_document(source: str | Path) -> dict[str, Any]:
    source_path = Path(source)
    inspection: dict[str, Any] = {
        "hwpx_zip_file": False,
        "hwpx_xml_file_count": 0,
        "hwp_preview_text_length": 0,
    }
    try:
        is_zip = zipfile.is_zipfile(source_path)
    except OSError as exc:
        inspection["inspection_error"] = str(exc)
        return inspection
    inspection["hwpx_zip_file"] = bool(is_zip)
    if not is_zip:
        return inspection

    text_chunks: list[str] = []
    try:
        with zipfile.ZipFile(source_path) as archive:
            xml_names = [
                name
                for name in archive.namelist()
                if name.lower().endswith(".xml") and not name.endswith("/")
            ]
            inspection["hwpx_xml_file_count"] = len(xml_names)

            def xml_sort_key(name: str) -> tuple[int, str]:
                lowered = name.lower()
                if lowered.startswith("contents/section"):
                    return (0, lowered)
                if lowered.startswith("contents/"):
                    return (1, lowered)
                return (2, lowered)

            for name in sorted(xml_names, key=xml_sort_key):
                try:
                    data = archive.read(name)
                except (KeyError, OSError):
                    continue
                try:
                    root = ET.fromstring(data)
                except ET.ParseError:
                    continue
                for part in root.itertext():
                    text = re.sub(r"\s+", " ", str(part or "")).strip()
                    if text:
                        text_chunks.append(text)
    except (OSError, zipfile.BadZipFile) as exc:
        inspection["inspection_error"] = str(exc)
        return inspection

    extracted_text = "\n".join(text_chunks).strip()
    if extracted_text:
        inspection["hwp_preview_text_length"] = len(extracted_text)
        inspection["hwp_preview_text"] = extracted_text[:4000]
        inspection["hwp_text_extractor"] = "hwpx-xml"
        inspection.update(_summarize_hwp_text_problem_signals(extracted_text))
    return inspection


def inspect_hangul_document(source: str | Path) -> dict[str, Any]:
    source_path = Path(source)
    suffix = source_path.suffix.lower()
    if suffix == ".hwp":
        return inspect_hwp_document(source_path)
    if suffix == ".hwpx":
        return inspect_hwpx_document(source_path)
    return {}


def _format_hwp_conversion_diagnosis(inspection: dict[str, Any]) -> str:
    if not inspection:
        return ""
    if inspection.get("hwpx_zip_file") is True:
        xml_count = int(inspection.get("hwpx_xml_file_count") or 0)
        preview_length = int(inspection.get("hwp_preview_text_length") or 0)
        return (
            "Input is a valid HWPX ZIP document "
            f"(xml_files={xml_count}, preview_text_length={preview_length}), "
            "but the installed PDF converter could not load this HWPX. "
            "Export it from Hancom Office as PDF or install a converter with HWPX support. "
            "한컴오피스에서 PDF로 내보낸 뒤 다시 업로드하거나 HWPX 지원 변환기를 설치해 주세요."
        )
    if inspection.get("hwp_signature") == "HWP Document File":
        flags = dict(inspection.get("hwp_flags") or {})
        protected_flags = [
            name
            for name in ("password", "distribution", "drm", "encrypted_cert", "copy_protection")
            if flags.get(name)
        ]
        if protected_flags:
            return (
                "Input is a valid HWP document, but protection flags are set "
                f"({', '.join(protected_flags)}). Export it from Hancom Office as PDF or use an authorized converter."
            )
        section_count = int(inspection.get("hwp_section_count") or 0)
        preview_length = int(inspection.get("hwp_preview_text_length") or 0)
        compressed = bool(flags.get("compressed"))
        return (
            "Input is a valid HWP document "
            f"(compressed={compressed}, sections={section_count}, preview_text_length={preview_length}), "
            "but the installed PDF converter could not load this HWP. "
            "Use Hancom Office/native HWP export to PDF or install a converter with HWP binary support. "
            "한컴오피스에서 PDF로 내보낸 뒤 다시 업로드하거나 HWP 지원 변환기를 설치해 주세요."
        )
    if inspection.get("ole_file") is False:
        return "Input does not look like an OLE-based HWP document."
    if inspection.get("inspection_error"):
        return f"HWP preflight inspection was incomplete: {inspection['inspection_error']}"
    return ""


def _looks_like_pdf_print_date_header(text: str) -> bool:
    normalized = str(text or "").strip()
    if re.match(r"^[0-9]{2,4}\.\s*[0-9]{1,2}\.\s*[0-9]{1,2}\.", normalized):
        return True
    if any(marker in normalized for marker in ("오전", "오후", "AM", "PM")):
        return normalized[:1].isdigit() and len(re.findall(r"[0-9]+", normalized)) >= 3
    return False


def _looks_like_decimal_continuation(text: str, match: Any) -> bool:
    after_dot_index = match.end(1) + 1
    return after_dot_index < len(text) and text[after_dot_index].isdigit()


def _file_sha1(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _hwp_conversion_cache_path(target_dir: Path, source_path: Path) -> Path:
    return target_dir / f".{source_path.stem}.conversion.json"


def _load_cached_hwp_pdf(source_path: Path, target_dir: Path) -> Path | None:
    cache_path = _hwp_conversion_cache_path(target_dir, source_path)
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    try:
        source_sha1 = _file_sha1(source_path)
    except OSError:
        return None
    if payload.get("source_sha1") != source_sha1:
        return None
    if payload.get("source_suffix") != source_path.suffix.lower():
        return None

    pdf_name = payload.get("pdf_name")
    if not isinstance(pdf_name, str) or not pdf_name:
        return None
    pdf_path = target_dir / pdf_name
    if not pdf_path.exists() or pdf_path.stat().st_size <= 0:
        return None
    return pdf_path


def _save_hwp_pdf_cache(source_path: Path, target_dir: Path, pdf_path: Path) -> None:
    try:
        source_sha1 = _file_sha1(source_path)
        pdf_name = pdf_path.relative_to(target_dir).as_posix()
    except (OSError, ValueError):
        return
    payload = {
        "version": 1,
        "source_name": source_path.name,
        "source_suffix": source_path.suffix.lower(),
        "source_sha1": source_sha1,
        "pdf_name": pdf_name,
        "pdf_size": pdf_path.stat().st_size,
    }
    _hwp_conversion_cache_path(target_dir, source_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _hwp_core_render_cache_path(
    target_dir: Path,
    source_path: Path,
    source_sha1: str | None = None,
    *,
    renderer: str = "rhwp-core",
) -> Path:
    cache_key = source_sha1 or source_path.stem
    renderer_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", renderer).strip("-") or "renderer"
    return target_dir / f".{cache_key}.{renderer_key}-render.json"


def _load_cached_hwp_core_pages(
    source_path: Path,
    target_dir: Path,
    dpi: int,
    *,
    renderer: str = "rhwp-core",
) -> list[NormalizedPageImage]:
    target_root = target_dir.resolve()
    try:
        source_sha1 = _file_sha1(source_path)
    except OSError:
        return []

    cache_paths = [_hwp_core_render_cache_path(target_dir, source_path, source_sha1, renderer=renderer)]
    legacy_cache_path = _hwp_core_render_cache_path(target_dir, source_path, renderer=renderer)
    if renderer == "rhwp-core" and legacy_cache_path not in cache_paths:
        cache_paths.append(legacy_cache_path)

    for cache_path in cache_paths:
        if not cache_path.exists():
            continue
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        if payload.get("source_sha1") != source_sha1:
            continue
        if payload.get("source_suffix") != source_path.suffix.lower():
            continue
        if int(payload.get("dpi") or 0) != int(dpi):
            continue
        if payload.get("renderer") and payload.get("renderer") != renderer:
            continue

        pages: list[NormalizedPageImage] = []
        for index, item in enumerate(payload.get("pages") or []):
            if not isinstance(item, dict):
                pages = []
                break
            normalized_name = str(item.get("normalized_name") or "")
            if not normalized_name:
                pages = []
                break
            normalized_path = target_root / normalized_name
            try:
                if not normalized_path.exists() or normalized_path.stat().st_size <= 0:
                    pages = []
                    break
            except OSError:
                pages = []
                break
            metadata = dict(item.get("metadata") or {})
            metadata.setdefault("source_type", "hwp")
            metadata.setdefault("document_like", True)
            metadata.setdefault("hwp_renderer", renderer)
            metadata["hwp_renderer_cache_hit"] = True
            pages.append(
                NormalizedPageImage(
                    page_id=str(item.get("page_id") or f"{source_path.stem}-page-{index + 1:03d}"),
                    source_path=str(source_path),
                    normalized_path=str(normalized_path),
                    page_index=int(item.get("page_index") if item.get("page_index") is not None else index),
                    width_px=int(item.get("width_px") or 0),
                    height_px=int(item.get("height_px") or 0),
                    metadata=metadata,
                )
            )
        if pages:
            return pages
    return []


def _save_hwp_core_render_cache(
    source_path: Path,
    target_dir: Path,
    dpi: int,
    pages: list[NormalizedPageImage],
    *,
    renderer: str = "rhwp-core",
) -> None:
    if not pages:
        return

    cache_pages: list[dict[str, Any]] = []
    try:
        source_sha1 = _file_sha1(source_path)
        target_root = target_dir.resolve()
        for page in pages:
            page_path = Path(page.normalized_path).resolve()
            normalized_name = page_path.relative_to(target_root).as_posix()
            metadata = dict(page.metadata)
            metadata.pop("hwp_renderer_cache_hit", None)
            cache_pages.append(
                {
                    "page_id": page.page_id,
                    "normalized_name": normalized_name,
                    "page_index": page.page_index,
                    "width_px": page.width_px,
                    "height_px": page.height_px,
                    "metadata": metadata,
                }
            )
    except (OSError, ValueError):
        return

    payload = {
        "version": 1,
        "renderer": renderer,
        "source_name": source_path.name,
        "source_suffix": source_path.suffix.lower(),
        "source_sha1": source_sha1,
        "dpi": int(dpi),
        "pages": cache_pages,
    }
    try:
        _hwp_core_render_cache_path(target_dir, source_path, source_sha1, renderer=renderer).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return


def _hwp_normalized_cache_options(
    *,
    dpi: int,
    enable_deskew: bool,
    enable_margin_crop: bool,
    max_dimension: int | None,
) -> dict[str, Any]:
    return {
        "dpi": int(dpi),
        "enable_perspective": False,
        "enable_deskew": bool(enable_deskew),
        "enable_margin_crop": bool(enable_margin_crop),
        "max_dimension": int(max_dimension) if max_dimension is not None else None,
    }


def _hwp_normalized_pages_cache_path(
    target_dir: Path,
    source_path: Path,
    source_sha1: str | None = None,
) -> Path:
    cache_key = source_sha1 or source_path.stem
    return target_dir / f".{cache_key}.hwp-normalized-pages.json"


def _load_cached_hwp_normalized_pages(
    source_path: Path,
    target_dir: Path,
    *,
    dpi: int,
    enable_deskew: bool,
    enable_margin_crop: bool,
    max_dimension: int | None,
) -> list[NormalizedPageImage]:
    target_root = target_dir.resolve()
    try:
        source_sha1 = _file_sha1(source_path)
    except OSError:
        return []
    cache_path = _hwp_normalized_pages_cache_path(target_dir, source_path, source_sha1)
    if not cache_path.exists():
        return []
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    expected_options = _hwp_normalized_cache_options(
        dpi=dpi,
        enable_deskew=enable_deskew,
        enable_margin_crop=enable_margin_crop,
        max_dimension=max_dimension,
    )
    if payload.get("source_sha1") != source_sha1:
        return []
    if payload.get("version") != HWP_NORMALIZED_CACHE_VERSION:
        return []
    if payload.get("source_suffix") != source_path.suffix.lower():
        return []
    if payload.get("options") != expected_options:
        return []

    pages: list[NormalizedPageImage] = []
    for index, item in enumerate(payload.get("pages") or []):
        if not isinstance(item, dict):
            return []
        normalized_name = str(item.get("normalized_name") or "")
        if not normalized_name:
            return []
        normalized_path = target_root / normalized_name
        try:
            if not normalized_path.exists() or normalized_path.stat().st_size <= 0:
                return []
        except OSError:
            return []

        source_name = str(item.get("source_name") or "")
        page_source_path = str(target_root / source_name) if source_name else str(source_path)
        metadata = dict(item.get("metadata") or {})
        metadata["hwp_normalized_cache_hit"] = True
        metadata["source_hwp_path"] = str(source_path)
        metadata.setdefault("source_type", "hwp")
        metadata.setdefault("document_like", True)
        pages.append(
            NormalizedPageImage(
                page_id=str(item.get("page_id") or f"{source_path.stem}-page-{index + 1:03d}"),
                source_path=page_source_path,
                normalized_path=str(normalized_path),
                page_index=int(item.get("page_index") if item.get("page_index") is not None else index),
                width_px=int(item.get("width_px") or 0),
                height_px=int(item.get("height_px") or 0),
                metadata=metadata,
            )
        )
    return pages


def _save_hwp_normalized_pages_cache(
    source_path: Path,
    target_dir: Path,
    *,
    dpi: int,
    enable_deskew: bool,
    enable_margin_crop: bool,
    max_dimension: int | None,
    pages: list[NormalizedPageImage],
) -> None:
    if not pages:
        return

    cache_pages: list[dict[str, Any]] = []
    try:
        source_sha1 = _file_sha1(source_path)
        target_root = target_dir.resolve()
        for page in pages:
            normalized_name = Path(page.normalized_path).resolve().relative_to(target_root).as_posix()
            try:
                source_name = Path(page.source_path).resolve().relative_to(target_root).as_posix()
            except ValueError:
                source_name = ""
            metadata = dict(page.metadata)
            metadata.pop("hwp_normalized_cache_hit", None)
            cache_pages.append(
                {
                    "page_id": page.page_id,
                    "source_name": source_name,
                    "normalized_name": normalized_name,
                    "page_index": page.page_index,
                    "width_px": page.width_px,
                    "height_px": page.height_px,
                    "metadata": metadata,
                }
            )
    except (OSError, ValueError):
        return

    payload = {
        "version": HWP_NORMALIZED_CACHE_VERSION,
        "source_name": source_path.name,
        "source_suffix": source_path.suffix.lower(),
        "source_sha1": source_sha1,
        "options": _hwp_normalized_cache_options(
            dpi=dpi,
            enable_deskew=enable_deskew,
            enable_margin_crop=enable_margin_crop,
            max_dimension=max_dimension,
        ),
        "pages": cache_pages,
    }
    try:
        _hwp_normalized_pages_cache_path(target_dir, source_path, source_sha1).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return


def _pdf_normalized_cache_options(
    *,
    dpi: int,
    enable_deskew: bool,
    enable_margin_crop: bool,
    max_dimension: int | None,
) -> dict[str, Any]:
    return {
        "dpi": int(dpi),
        "enable_perspective": False,
        "enable_deskew": bool(enable_deskew),
        "enable_margin_crop": bool(enable_margin_crop),
        "max_dimension": int(max_dimension) if max_dimension is not None else None,
    }


def _pdf_normalized_pages_cache_path(
    target_dir: Path,
    source_path: Path,
    source_sha1: str | None = None,
) -> Path:
    cache_key = source_sha1 or source_path.stem
    return target_dir / f".{cache_key}.pdf-normalized-pages.json"


def _pdf_normalized_output_dir(
    target_dir: Path,
    source_path: Path,
    *,
    dpi: int,
    enable_deskew: bool,
    enable_margin_crop: bool,
    max_dimension: int | None,
) -> Path:
    try:
        source_key = _file_sha1(source_path)[:16]
    except OSError:
        source_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_path.stem).strip("-") or "source"
    options = _pdf_normalized_cache_options(
        dpi=dpi,
        enable_deskew=enable_deskew,
        enable_margin_crop=enable_margin_crop,
        max_dimension=max_dimension,
    )
    option_key = hashlib.sha1(
        json.dumps(options, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return target_dir / "normalized" / f"{source_key}-{option_key}"


def _image_normalized_cache_options(
    *,
    enable_perspective: bool,
    enable_deskew: bool,
    enable_margin_crop: bool,
    max_dimension: int | None,
) -> dict[str, Any]:
    return {
        "enable_perspective": bool(enable_perspective),
        "enable_deskew": bool(enable_deskew),
        "enable_margin_crop": bool(enable_margin_crop),
        "max_dimension": int(max_dimension) if max_dimension is not None else None,
    }


def _image_normalized_pages_cache_path(
    target_dir: Path,
    source_path: Path,
    source_sha1: str | None = None,
) -> Path:
    cache_key = source_sha1 or source_path.stem
    return target_dir / f".{cache_key}.image-normalized-pages.json"


def _image_normalized_output_dir(
    target_dir: Path,
    source_path: Path,
    *,
    enable_perspective: bool,
    enable_deskew: bool,
    enable_margin_crop: bool,
    max_dimension: int | None,
    source_sha1: str | None = None,
) -> Path:
    if source_sha1 is None:
        try:
            source_sha1 = _file_sha1(source_path)
        except OSError:
            source_sha1 = None
    if source_sha1:
        source_key = source_sha1[:16]
    else:
        source_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_path.stem).strip("-") or "source"
    options = _image_normalized_cache_options(
        enable_perspective=enable_perspective,
        enable_deskew=enable_deskew,
        enable_margin_crop=enable_margin_crop,
        max_dimension=max_dimension,
    )
    option_key = hashlib.sha1(
        json.dumps(options, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return target_dir / "normalized" / f"{source_key}-{option_key}"


def _load_cached_image_normalized_pages(
    source_path: Path,
    target_dir: Path,
    *,
    enable_perspective: bool,
    enable_deskew: bool,
    enable_margin_crop: bool,
    max_dimension: int | None,
    source_sha1: str | None = None,
) -> list[NormalizedPageImage]:
    target_root = target_dir.resolve()
    if source_sha1 is None:
        try:
            source_sha1 = _file_sha1(source_path)
        except OSError:
            return []
    cache_path = _image_normalized_pages_cache_path(target_dir, source_path, source_sha1)
    if not cache_path.exists():
        return []
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    expected_options = _image_normalized_cache_options(
        enable_perspective=enable_perspective,
        enable_deskew=enable_deskew,
        enable_margin_crop=enable_margin_crop,
        max_dimension=max_dimension,
    )
    if payload.get("source_sha1") != source_sha1:
        return []
    if payload.get("version") != IMAGE_NORMALIZED_CACHE_VERSION:
        return []
    if payload.get("source_suffix") != source_path.suffix.lower():
        return []
    if payload.get("options") != expected_options:
        return []

    pages: list[NormalizedPageImage] = []
    for index, item in enumerate(payload.get("pages") or []):
        if not isinstance(item, dict):
            return []
        normalized_name = str(item.get("normalized_name") or "")
        if not normalized_name:
            return []
        normalized_path = target_root / normalized_name
        try:
            if not normalized_path.exists() or normalized_path.stat().st_size <= 0:
                return []
        except OSError:
            return []

        metadata = dict(item.get("metadata") or {})
        metadata["image_normalized_cache_hit"] = True
        metadata.setdefault("source_type", "image")
        pages.append(
            NormalizedPageImage(
                page_id=str(item.get("page_id") or f"{source_path.stem}-page-{index + 1:03d}"),
                source_path=str(source_path),
                normalized_path=str(normalized_path),
                page_index=int(item.get("page_index") if item.get("page_index") is not None else index),
                width_px=int(item.get("width_px") or 0),
                height_px=int(item.get("height_px") or 0),
                metadata=metadata,
            )
        )
    return pages


def _save_image_normalized_pages_cache(
    source_path: Path,
    target_dir: Path,
    *,
    enable_perspective: bool,
    enable_deskew: bool,
    enable_margin_crop: bool,
    max_dimension: int | None,
    pages: list[NormalizedPageImage],
    source_sha1: str | None = None,
) -> None:
    if not pages:
        return

    cache_pages: list[dict[str, Any]] = []
    try:
        if source_sha1 is None:
            source_sha1 = _file_sha1(source_path)
        target_root = target_dir.resolve()
        for page in pages:
            normalized_name = Path(page.normalized_path).resolve().relative_to(target_root).as_posix()
            metadata = dict(page.metadata)
            metadata.pop("image_normalized_cache_hit", None)
            cache_pages.append(
                {
                    "page_id": page.page_id,
                    "normalized_name": normalized_name,
                    "page_index": page.page_index,
                    "width_px": page.width_px,
                    "height_px": page.height_px,
                    "metadata": metadata,
                }
            )
    except (OSError, ValueError):
        return

    payload = {
        "version": IMAGE_NORMALIZED_CACHE_VERSION,
        "source_name": source_path.name,
        "source_suffix": source_path.suffix.lower(),
        "source_sha1": source_sha1,
        "options": _image_normalized_cache_options(
            enable_perspective=enable_perspective,
            enable_deskew=enable_deskew,
            enable_margin_crop=enable_margin_crop,
            max_dimension=max_dimension,
        ),
        "pages": cache_pages,
    }
    try:
        _image_normalized_pages_cache_path(target_dir, source_path, source_sha1).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return


def _load_cached_pdf_normalized_pages(
    source_path: Path,
    target_dir: Path,
    *,
    dpi: int,
    enable_deskew: bool,
    enable_margin_crop: bool,
    max_dimension: int | None,
) -> list[NormalizedPageImage]:
    target_root = target_dir.resolve()
    try:
        source_sha1 = _file_sha1(source_path)
    except OSError:
        return []
    cache_path = _pdf_normalized_pages_cache_path(target_dir, source_path, source_sha1)
    if not cache_path.exists():
        return []
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    expected_options = _pdf_normalized_cache_options(
        dpi=dpi,
        enable_deskew=enable_deskew,
        enable_margin_crop=enable_margin_crop,
        max_dimension=max_dimension,
    )
    if payload.get("source_sha1") != source_sha1:
        return []
    if payload.get("version") != PDF_NORMALIZED_CACHE_VERSION:
        return []
    if payload.get("source_suffix") != source_path.suffix.lower():
        return []
    if payload.get("options") != expected_options:
        return []

    pages: list[NormalizedPageImage] = []
    for index, item in enumerate(payload.get("pages") or []):
        if not isinstance(item, dict):
            return []
        normalized_name = str(item.get("normalized_name") or "")
        if not normalized_name:
            return []
        normalized_path = target_root / normalized_name
        try:
            if not normalized_path.exists() or normalized_path.stat().st_size <= 0:
                return []
        except OSError:
            return []

        source_name = str(item.get("source_name") or "")
        candidate_source_path = target_root / source_name if source_name else source_path
        page_source_path = str(candidate_source_path) if candidate_source_path.exists() else str(source_path)
        metadata = dict(item.get("metadata") or {})
        metadata["pdf_normalized_cache_hit"] = True
        metadata["source_pdf_path"] = str(source_path)
        metadata.setdefault("source_type", "pdf")
        metadata.setdefault("document_like", True)
        pages.append(
            NormalizedPageImage(
                page_id=str(item.get("page_id") or f"{source_path.stem}-page-{index + 1:03d}"),
                source_path=page_source_path,
                normalized_path=str(normalized_path),
                page_index=int(item.get("page_index") if item.get("page_index") is not None else index),
                width_px=int(item.get("width_px") or 0),
                height_px=int(item.get("height_px") or 0),
                metadata=metadata,
            )
        )
    return pages


def _save_pdf_normalized_pages_cache(
    source_path: Path,
    target_dir: Path,
    *,
    dpi: int,
    enable_deskew: bool,
    enable_margin_crop: bool,
    max_dimension: int | None,
    pages: list[NormalizedPageImage],
) -> None:
    if not pages:
        return

    cache_pages: list[dict[str, Any]] = []
    try:
        source_sha1 = _file_sha1(source_path)
        target_root = target_dir.resolve()
        for page in pages:
            normalized_name = Path(page.normalized_path).resolve().relative_to(target_root).as_posix()
            try:
                source_name = Path(page.source_path).resolve().relative_to(target_root).as_posix()
            except ValueError:
                source_name = ""
            metadata = dict(page.metadata)
            metadata.pop("pdf_normalized_cache_hit", None)
            cache_pages.append(
                {
                    "page_id": page.page_id,
                    "source_name": source_name,
                    "normalized_name": normalized_name,
                    "page_index": page.page_index,
                    "width_px": page.width_px,
                    "height_px": page.height_px,
                    "metadata": metadata,
                }
            )
    except (OSError, ValueError):
        return

    payload = {
        "version": PDF_NORMALIZED_CACHE_VERSION,
        "source_name": source_path.name,
        "source_suffix": source_path.suffix.lower(),
        "source_sha1": source_sha1,
        "options": _pdf_normalized_cache_options(
            dpi=dpi,
            enable_deskew=enable_deskew,
            enable_margin_crop=enable_margin_crop,
            max_dimension=max_dimension,
        ),
        "pages": cache_pages,
    }
    try:
        _pdf_normalized_pages_cache_path(target_dir, source_path, source_sha1).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return


def _resolve_preprocess_page_worker_count(page_count: int) -> int:
    if page_count <= 1:
        return 1
    max_workers = max(1, min(4, page_count, os.cpu_count() or 2))
    raw_worker_count = os.environ.get("EDB_PREPROCESS_PAGE_WORKERS", "").strip()
    if raw_worker_count:
        try:
            requested_workers = int(raw_worker_count)
        except ValueError:
            requested_workers = max_workers
        if requested_workers <= 0:
            return 1
        return max(1, min(max_workers, requested_workers))
    return max_workers


def _normalize_pdf_rendered_pages(
    source_path: Path,
    rendered: list[NormalizedPageImage],
    normalized_output_dir: Path,
    *,
    enable_deskew: bool,
    enable_margin_crop: bool,
    max_dimension: int | None,
) -> list[NormalizedPageImage]:
    def _normalize(page: NormalizedPageImage) -> NormalizedPageImage:
        normalized = normalize_image(
            page.normalized_path,
            normalized_output_dir,
            page_id=page.page_id,
            page_index=page.page_index,
            enable_perspective=False,
            enable_deskew=enable_deskew,
            enable_margin_crop=enable_margin_crop,
            max_dimension=max_dimension,
            base_metadata=dict(page.metadata),
        )
        normalized.metadata.setdefault("source_pdf_path", str(source_path))
        normalized.metadata["source_type"] = "pdf"
        normalized.metadata["document_like"] = True
        return normalized

    worker_count = _resolve_preprocess_page_worker_count(len(rendered))
    if worker_count <= 1:
        normalized_pages = [_normalize(page) for page in rendered]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            normalized_pages = list(executor.map(_normalize, rendered))
    for page in normalized_pages:
        page.metadata["pdf_preprocess_page_worker_count"] = worker_count
    return normalized_pages


def _run_hwp_pdf_converter_commands(
    source_path: Path,
    target_dir: Path,
    commands: list[list[str]],
    timeout_seconds: int,
) -> tuple[Path | None, list[str]]:
    errors: list[str] = []
    for command_prefix in commands:
        tool_name = Path(command_prefix[0]).name.lower()
        expected_pdf = target_dir / f"{source_path.stem}.pdf"
        command_env: dict[str, str] | None = None
        if "hwp5pdf" in tool_name:
            command = [*command_prefix, str(source_path), str(expected_pdf)]
        elif tool_name == "rhwp":
            command = [*command_prefix, "export-pdf", str(source_path), "-o", str(expected_pdf)]
        elif "airun-hwp" in tool_name:
            if source_path.suffix.lower() != ".hwpx":
                errors.append(f"{command_prefix[0]} skipped: airun-hwp supports HWPX input; HWP needs a HWPX bridge first")
                continue
            try:
                airun_source_path = _stage_airun_hwp_source(source_path, target_dir)
            except OSError as exc:
                errors.append(f"{command_prefix[0]} staging failed: {exc}")
                continue
            command = [
                *command_prefix,
                str(airun_source_path),
                "--format",
                "pdf",
                "--output",
                str(target_dir),
                "--pdf-engine",
                "auto",
            ]
            command_env = _airun_hwp_env(target_dir)
        else:
            command = [
                *command_prefix,
                "--convert-to",
                "pdf",
                "--outdir",
                str(target_dir),
                str(source_path),
            ]
        before_signature = {
            candidate: (
                candidate.stat().st_mtime_ns,
                candidate.stat().st_size,
                _file_sha1(candidate),
            )
            for candidate in _hwp_pdf_candidates(target_dir, source_path)
            if candidate.exists()
        }
        try:
            run_kwargs: dict[str, Any] = {
                "check": False,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "timeout": timeout_seconds,
            }
            if command_env is not None:
                run_kwargs["env"] = command_env
            result = subprocess.run(command, **run_kwargs)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{command[0]}: {exc}")
            continue

        for pdf_path in _hwp_pdf_candidates(target_dir, source_path):
            if not pdf_path.exists():
                continue
            current_signature = (
                pdf_path.stat().st_mtime_ns,
                pdf_path.stat().st_size,
                _file_sha1(pdf_path),
            )
            previous_signature = before_signature.get(pdf_path)
            if previous_signature is None or current_signature != previous_signature:
                return pdf_path, errors
        output = " ".join(
            part for part in [result.stdout.strip(), result.stderr.strip()] if part
        )
        errors.append(
            f"{command[0]} exited {result.returncode}: {output or 'no PDF output'}"
        )
    return None, errors


def _convert_hwp_to_hwpx_with_hwpilot(
    source_path: Path,
    output_dir: Path,
    timeout_seconds: int,
) -> tuple[Path | None, list[str]]:
    if source_path.suffix.lower() != ".hwp":
        return None, []

    commands = _iter_hwp_hwpx_converter_commands()
    if not commands:
        return None, []

    output_dir.mkdir(parents=True, exist_ok=True)
    target_path = output_dir / f"{source_path.stem}.hwpx"
    errors: list[str] = []
    for command_prefix in commands:
        command = [*command_prefix, "convert", str(source_path), str(target_path)]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{command[0]}: {exc}")
            continue

        if target_path.exists():
            return target_path, errors
        output = " ".join(
            part for part in [result.stdout.strip(), result.stderr.strip()] if part
        )
        errors.append(f"{command[0]} exited {result.returncode}: {output or 'no HWPX output'}")
    return None, errors


def _terminate_process(process: Any) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _convert_hwp_to_pdf_with_pyhwp_html(
    source_path: Path,
    output_dir: Path,
    timeout_seconds: int,
) -> tuple[Path | None, list[str]]:
    hwp5html_commands = _iter_pyhwp_html_converter_commands()
    chrome_commands = _iter_chrome_pdf_commands()
    if not hwp5html_commands or not chrome_commands:
        return None, []

    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = output_dir.resolve()
    errors: list[str] = []
    for hwp5html_prefix in hwp5html_commands:
        html_root = output_dir / f"{source_path.stem}.html"
        if html_root.exists():
            shutil.rmtree(html_root) if html_root.is_dir() else html_root.unlink()
        command = [*hwp5html_prefix, "--output", str(html_root), str(source_path)]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{command[0]}: {exc}")
            continue
        index_candidates = [
            html_root / "index.xhtml",
            html_root / "index.html",
            html_root if html_root.is_file() else None,
        ]
        index_path = next((path for path in index_candidates if path and path.exists()), None)
        if not index_path:
            output = " ".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
            errors.append(f"{command[0]} exited {result.returncode}: {output or 'no HTML output'}")
            continue

        for chrome_prefix in chrome_commands:
            pdf_path = output_dir / f"{source_path.stem}.pdf"
            if pdf_path.exists():
                pdf_path.unlink()
            user_data_dir = output_dir / "_chrome_profile"
            chrome_command = [
                *chrome_prefix,
                "--headless=new",
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-breakpad",
                "--disable-crash-reporter",
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={user_data_dir}",
                "--virtual-time-budget=10000",
                "--print-to-pdf-no-header",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}",
                index_path.as_uri(),
            ]
            try:
                process = subprocess.Popen(
                    chrome_command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except OSError as exc:
                errors.append(f"{chrome_command[0]}: {exc}")
                continue

            deadline = time.monotonic() + timeout_seconds
            try:
                while time.monotonic() < deadline:
                    if pdf_path.exists() and pdf_path.stat().st_size > 0:
                        _terminate_process(process)
                        return pdf_path, errors
                    if process.poll() is not None:
                        break
                    time.sleep(0.25)
                if pdf_path.exists() and pdf_path.stat().st_size > 0:
                    _terminate_process(process)
                    return pdf_path, errors
                errors.append(f"{chrome_command[0]} did not produce PDF output")
            finally:
                _terminate_process(process)
    return None, errors


def convert_hwp_to_pdf(source: str | Path, output_dir: str | Path, timeout_seconds: int = 90) -> Path:
    source_path = Path(source)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    cached_pdf = _load_cached_hwp_pdf(source_path, target_dir)
    if cached_pdf:
        return cached_pdf

    hwp_inspection = inspect_hangul_document(source_path)
    commands = _iter_hwp_pdf_converter_commands()
    if not commands:
        diagnosis = _format_hwp_conversion_diagnosis(hwp_inspection)
        suffix = f" Diagnosis: {diagnosis}" if diagnosis else ""
        raise ValueError(
            "HWP/HWPX input requires a local converter such as LibreOffice with HWP support "
            "or hwp5pdf. HWPilot can only help normalize HWP to HWPX and still needs a PDF "
            f"converter. Install one, or convert the HWP file to PDF first.{suffix}"
        )

    has_html_pdf_fallback = bool(_iter_pyhwp_html_converter_commands() and _iter_chrome_pdf_commands())
    direct_timeout_seconds = timeout_seconds
    if source_path.suffix.lower() == ".hwp" and has_html_pdf_fallback:
        direct_timeout_seconds = min(timeout_seconds, 15)

    pdf_path, errors = _run_hwp_pdf_converter_commands(
        source_path,
        target_dir,
        commands,
        direct_timeout_seconds,
    )
    if pdf_path:
        _save_hwp_pdf_cache(source_path, target_dir, pdf_path)
        return pdf_path

    hwpx_path, hwpilot_errors = _convert_hwp_to_hwpx_with_hwpilot(source_path, target_dir / "_hwpilot", timeout_seconds)
    errors.extend(hwpilot_errors)
    if hwpx_path:
        pdf_path, hwpx_pdf_errors = _run_hwp_pdf_converter_commands(hwpx_path, target_dir, commands, timeout_seconds)
        if pdf_path:
            _save_hwp_pdf_cache(source_path, target_dir, pdf_path)
            return pdf_path
        errors.extend(f"after HWPilot bridge: {error}" for error in hwpx_pdf_errors)

    html_pdf_path, html_pdf_errors = _convert_hwp_to_pdf_with_pyhwp_html(
        source_path,
        target_dir / "_pyhwp_html",
        timeout_seconds,
    )
    if html_pdf_path:
        final_pdf = target_dir / f"{source_path.stem}.pdf"
        final_pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(html_pdf_path, final_pdf)
        _save_hwp_pdf_cache(source_path, target_dir, final_pdf)
        return final_pdf
    errors.extend(f"after pyhwp HTML bridge: {error}" for error in html_pdf_errors)

    detail = "; ".join(errors) if errors else "no converter produced a PDF"
    diagnosis = _format_hwp_conversion_diagnosis(hwp_inspection)
    suffix = f" Diagnosis: {diagnosis}" if diagnosis else ""
    raise ValueError(
        "HWP/HWPX conversion failed. Install LibreOffice with HWP support, "
        f"or convert the HWP file to PDF first. Details: {detail}{suffix}"
    )


def _extract_pdf_problem_markers(
    page: Any,
    scale: float,
    data: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Extract problem-number line anchors from a PDF text layer.

    Coordinates are returned in rendered-pixel space so downstream image
    segmentation can create page crops without calling OCR.
    """
    import re

    markers: list[dict[str, Any]] = []
    if data is None:
        try:
            data = page.get_text("dict")
        except Exception:
            return markers

    for block in data.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for line in block.get("lines") or []:
            if not isinstance(line, dict):
                continue
            text = "".join(
                str(span.get("text") or "")
                for span in line.get("spans") or []
                if isinstance(span, dict)
            ).strip()
            if _looks_like_pdf_print_date_header(text):
                continue
            match = re.match(r"^([1-9][0-9]?)\.\s*", text)
            if not match:
                continue
            if _looks_like_decimal_continuation(text, match):
                continue
            number = int(match.group(1))
            if not 1 <= number <= 99:
                continue
            bbox = line.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            left, top, right, bottom = [float(value) * scale for value in bbox]
            markers.append(
                {
                    "number": number,
                    "text": text[:120],
                    "bbox": {
                        "left": left,
                        "top": top,
                        "right": right,
                        "bottom": bottom,
                        "width": max(0.0, right - left),
                        "height": max(0.0, bottom - top),
                    },
                }
            )

    return markers


def _looks_like_pdf_problem_stem_line(text: str) -> bool:
    import re

    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(normalized) < 10:
        return False
    if _looks_like_pdf_print_date_header(normalized):
        return False
    if re.match(r"^([1-9][0-9]?)\.\s*", normalized):
        return False
    if normalized[:1] in {"①", "②", "③", "④", "⑤", "ㄱ", "ㄴ", "ㄷ", "ㄹ", "◦", "○", "*"}:
        return False
    if normalized.startswith(("<보기>", "보기", "확인 사항", "제4교시")):
        return False
    stem_phrases = (
        "옳은 것은",
        "옳은 설명",
        "가장 적절한 것은",
        "고른 것은",
        "분석으로 옳은",
        "설명으로 옳은",
    )
    return any(phrase in normalized for phrase in stem_phrases)


def _extract_pdf_text_stem_markers(
    page: Any,
    scale: float,
    data: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    if data is None:
        try:
            data = page.get_text("dict")
        except Exception:
            return markers

    for block in data.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for line in block.get("lines") or []:
            if not isinstance(line, dict):
                continue
            text = "".join(
                str(span.get("text") or "")
                for span in line.get("spans") or []
                if isinstance(span, dict)
            ).strip()
            if not _looks_like_pdf_problem_stem_line(text):
                continue
            bbox = line.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            left, top, right, bottom = [float(value) * scale for value in bbox]
            markers.append(
                {
                    "marker_kind": "text_stem",
                    "text": text[:120],
                    "bbox": {
                        "left": left,
                        "top": top,
                        "right": right,
                        "bottom": bottom,
                        "width": max(0.0, right - left),
                        "height": max(0.0, bottom - top),
                    },
                }
            )

    return markers


def _extract_pdf_text_lines(
    page: Any,
    scale: float,
    data: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    if data is None:
        try:
            data = page.get_text("dict")
        except Exception:
            return lines

    line_index = 0
    for block in data.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for line in block.get("lines") or []:
            if not isinstance(line, dict):
                continue
            text = "".join(
                str(span.get("text") or "")
                for span in line.get("spans") or []
                if isinstance(span, dict)
            ).strip()
            if not text:
                continue
            bbox = line.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            left, top, right, bottom = [float(value) * scale for value in bbox]
            lines.append(
                {
                    "line_index": line_index,
                    "text": text[:400],
                    "bbox": {
                        "left": left,
                        "top": top,
                        "right": right,
                        "bottom": bottom,
                        "width": max(0.0, right - left),
                        "height": max(0.0, bottom - top),
                    },
                }
            )
            line_index += 1

    return lines


def _pdf_bbox_payload(raw_bbox: Sequence[Any], scale: float) -> dict[str, float]:
    left, top, right, bottom = [float(value) * scale for value in raw_bbox]
    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "width": max(0.0, right - left),
        "height": max(0.0, bottom - top),
    }


def _extract_pdf_media_regions(
    page: Any,
    scale: float,
    data: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return only high-confidence embedded images and real table grids.

    Full-page raster scans, tiny layout ornaments, and large bordered passage
    boxes are deliberately excluded. Those regions must continue through the
    ordinary Stage-2 chalk conversion instead of restoring the whole page.
    """

    regions: list[dict[str, Any]] = []
    page_area = max(1.0, float(page.rect.width) * float(page.rect.height))
    if data is None:
        try:
            data = page.get_text("dict")
        except Exception:
            data = {}
    for block in data.get("blocks") or []:
        if not isinstance(block, dict) or block.get("type") != 1:
            continue
        bbox = block.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        width = max(0.0, float(bbox[2]) - float(bbox[0]))
        height = max(0.0, float(bbox[3]) - float(bbox[1]))
        area_ratio = width * height / page_area
        if width < 12.0 or height < 12.0 or area_ratio < 0.0005 or area_ratio > 0.25:
            continue
        regions.append(
            {
                "kind": "image",
                "source": "pdf_image_block",
                "confidence": 0.99,
                "bbox": _pdf_bbox_payload(bbox, scale),
            }
        )

    try:
        tables = list(page.find_tables().tables)
    except Exception:
        tables = []
    for table in tables:
        bbox = table.bbox
        width = max(0.0, float(bbox[2]) - float(bbox[0]))
        height = max(0.0, float(bbox[3]) - float(bbox[1]))
        area_ratio = width * height / page_area
        row_count = int(table.row_count)
        column_count = int(table.col_count)
        if (
            row_count < 2
            or column_count < 2
            or row_count * column_count < 6
            or width < 24.0
            or height < 24.0
            or area_ratio < 0.002
            or area_ratio > 0.15
        ):
            continue
        regions.append(
            {
                "kind": "table",
                "source": "pymupdf_table",
                "confidence": 0.94,
                "row_count": row_count,
                "column_count": column_count,
                "bbox": _pdf_bbox_payload(bbox, scale),
            }
        )
    return regions


def load_image(source: str | Path) -> Image.Image:
    return Image.open(source).convert("RGB")


def _read_image_dimensions(source: str | Path) -> tuple[int, int] | None:
    try:
        with Image.open(source) as image:
            return image.size
    except OSError:
        return None


def _passthrough_image_page_if_possible(
    source_path: Path,
    *,
    page_index: int,
    enable_perspective: bool,
    enable_deskew: bool,
    enable_margin_crop: bool,
    max_dimension: int | None,
) -> NormalizedPageImage | None:
    if enable_perspective or enable_deskew or enable_margin_crop:
        return None
    size = _read_image_dimensions(source_path)
    if size is None:
        return None
    width, height = size
    if max_dimension and max(width, height) > max_dimension:
        return None
    page_id = f"{source_path.stem}-page-{page_index + 1:03d}"
    metadata: dict[str, Any] = {
        "source_type": "image",
        "image_passthrough": True,
        "perspective_corrected": False,
        "deskewed": False,
        "margin_cropped": False,
    }
    if max_dimension:
        metadata["max_dimension"] = int(max_dimension)
        metadata["max_dimension_passthrough"] = True
    return NormalizedPageImage(
        page_id=page_id,
        source_path=str(source_path),
        normalized_path=str(source_path),
        page_index=page_index,
        width_px=width,
        height_px=height,
        metadata=metadata,
    )


def _crop_uniform_margin_with_box(
    image: Image.Image,
    background_threshold: int = 245,
    padding: int = 12,
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    gray = ImageOps.grayscale(image)
    mask = gray.point(lambda px: 255 if px < background_threshold else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return image, (0, 0, image.width, image.height)
    left = max(0, bbox[0] - padding)
    top = max(0, bbox[1] - padding)
    right = min(image.width, bbox[2] + padding)
    bottom = min(image.height, bbox[3] + padding)
    return image.crop((left, top, right, bottom)), (left, top, right, bottom)


def crop_uniform_margin(image: Image.Image, background_threshold: int = 245, padding: int = 12) -> Image.Image:
    cropped, _ = _crop_uniform_margin_with_box(
        image,
        background_threshold=background_threshold,
        padding=padding,
    )
    return cropped


def _transform_pdf_bbox_list(
    metadata: dict[str, Any],
    key: str,
    *,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    scale: float = 1.0,
) -> None:
    markers = metadata.get(key)
    if not isinstance(markers, list):
        return

    transformed: list[dict[str, Any]] = []
    for marker in markers:
        if not isinstance(marker, dict):
            continue
        bbox = marker.get("bbox")
        if not isinstance(bbox, dict):
            continue
        try:
            left = (float(bbox.get("left", 0.0)) - offset_x) * scale
            top = (float(bbox.get("top", 0.0)) - offset_y) * scale
            right = (float(bbox.get("right", bbox.get("left", 0.0))) - offset_x) * scale
            bottom = (float(bbox.get("bottom", bbox.get("top", 0.0))) - offset_y) * scale
        except (TypeError, ValueError):
            continue
        updated = dict(marker)
        updated["bbox"] = {
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "width": max(0.0, right - left),
            "height": max(0.0, bottom - top),
        }
        transformed.append(updated)
    metadata[key] = transformed


def _transform_pdf_text_geometry(
    metadata: dict[str, Any],
    *,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    scale: float = 1.0,
) -> None:
    for key in (
        "pdf_problem_markers",
        "pdf_text_stem_markers",
        "pdf_text_lines",
        "pdf_media_regions",
    ):
        _transform_pdf_bbox_list(metadata, key, offset_x=offset_x, offset_y=offset_y, scale=scale)


def _transform_pdf_problem_markers(
    metadata: dict[str, Any],
    *,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    scale: float = 1.0,
) -> None:
    _transform_pdf_bbox_list(metadata, "pdf_problem_markers", offset_x=offset_x, offset_y=offset_y, scale=scale)


def _deskew_skip_reason_for_text_markers(metadata: dict[str, Any]) -> str | None:
    """Text markers are tied to the original rendered page coordinates."""
    markers = metadata.get("pdf_problem_markers")
    if not isinstance(markers, list) or not markers:
        return None
    source_type = metadata.get("source_type")
    if source_type == "pdf":
        return "pdf_text_layer"
    if source_type == "hwp" and metadata.get("hwp_layout_problem_markers_as_pdf_markers"):
        return "hwp_layout_problem_markers"
    return None


def _is_blank_rendered_page(path: str | Path, *, dark_threshold: int = 245, min_dark_ratio: float = 0.001) -> bool:
    try:
        image = Image.open(path).convert("L")
    except OSError:
        return False
    image.thumbnail((256, 256))
    histogram = image.histogram()
    dark_pixels = sum(histogram[:dark_threshold])
    total_pixels = max(1, sum(histogram))
    return (dark_pixels / total_pixels) < min_dark_ratio


def _summarize_pdf_render_quality(pages: list[NormalizedPageImage]) -> dict[str, Any]:
    marker_counts: list[int] = []
    stem_marker_counts: list[int] = []
    blank_page_count = 0
    for page in pages:
        markers = page.metadata.get("pdf_problem_markers")
        marker_counts.append(len(markers) if isinstance(markers, list) else 0)
        stem_markers = page.metadata.get("pdf_text_stem_markers")
        stem_marker_counts.append(len(stem_markers) if isinstance(stem_markers, list) else 0)
        if _is_blank_rendered_page(page.normalized_path):
            blank_page_count += 1

    marker_count = sum(marker_counts)
    stem_marker_count = sum(stem_marker_counts)
    pages_with_markers = sum(1 for count in marker_counts if count > 0)
    nonblank_page_count = max(0, len(pages) - blank_page_count)
    marker_coverage_denominator = nonblank_page_count if nonblank_page_count else len(pages)
    marker_coverage_ratio = (
        pages_with_markers / marker_coverage_denominator
        if marker_coverage_denominator
        else 0.0
    )
    marker_reliable = marker_count > 0 and marker_coverage_ratio >= 0.5
    warnings: list[str] = []
    if not pages:
        warnings.append("no_rendered_pages")
    if pages and marker_count == 0:
        warnings.append("no_pdf_text_markers")
    if pages_with_markers and pages_with_markers < len(pages):
        warnings.append("some_pages_without_text_markers")
    if marker_count > 0 and not marker_reliable:
        warnings.append("low_pdf_text_marker_coverage")
    if blank_page_count:
        warnings.append("blank_pages_detected")

    return {
        "page_count": len(pages),
        "pdf_text_marker_count": marker_count,
        "pdf_text_stem_marker_count": stem_marker_count,
        "pdf_pages_with_text_markers": pages_with_markers,
        "pdf_pages_without_text_markers": max(0, len(pages) - pages_with_markers),
        "pdf_text_marker_coverage_ratio": round(marker_coverage_ratio, 4),
        "pdf_text_markers_reliable": marker_reliable,
        "blank_page_count": blank_page_count,
        "has_pdf_text_markers": marker_count > 0,
        "preferred_segmentation_path": "pdf_text_markers" if marker_reliable else "ocr_fallback",
        "warnings": warnings,
    }


def _prefer_pdf_text_stem_markers_when_numeric_sparse(
    pages: list[NormalizedPageImage],
    quality: dict[str, Any],
) -> dict[str, Any]:
    if bool(quality.get("pdf_text_markers_reliable")):
        return quality

    stem_counts: list[int] = []
    for page in pages:
        markers = page.metadata.get("pdf_text_stem_markers")
        stem_counts.append(len(markers) if isinstance(markers, list) else 0)
    stem_marker_count = sum(stem_counts)
    numeric_marker_count = int(quality.get("pdf_text_marker_count") or 0)
    if stem_marker_count <= max(1, numeric_marker_count):
        return quality

    for page in pages:
        numeric_markers = page.metadata.get("pdf_problem_markers")
        if isinstance(numeric_markers, list):
            page.metadata["pdf_numeric_problem_markers"] = list(numeric_markers)
        stem_markers = page.metadata.get("pdf_text_stem_markers")
        page.metadata["pdf_problem_markers"] = list(stem_markers) if isinstance(stem_markers, list) else []

    pages_with_stems = sum(1 for count in stem_counts if count > 0)
    blank_page_count = int(quality.get("blank_page_count") or 0)
    page_count = int(quality.get("page_count") or len(pages))
    marker_coverage_denominator = max(0, page_count - blank_page_count) or page_count
    marker_coverage_ratio = (
        pages_with_stems / marker_coverage_denominator
        if marker_coverage_denominator
        else 0.0
    )
    warnings = list(quality.get("warnings") or [])
    if "using_pdf_text_stem_markers" not in warnings:
        warnings.append("using_pdf_text_stem_markers")

    updated = dict(quality)
    updated["pdf_numeric_text_marker_count"] = numeric_marker_count
    updated["pdf_text_marker_count"] = stem_marker_count
    updated["pdf_pages_with_text_markers"] = pages_with_stems
    updated["pdf_pages_without_text_markers"] = max(0, page_count - pages_with_stems)
    updated["pdf_text_marker_coverage_ratio"] = round(marker_coverage_ratio, 4)
    updated["pdf_text_markers_reliable"] = True
    updated["has_pdf_text_markers"] = stem_marker_count > 0
    updated["preferred_segmentation_path"] = "pdf_text_stem_markers"
    updated["warnings"] = warnings
    return updated


def deskew_image(image: Image.Image) -> Image.Image:
    if cv2 is None or np is None:
        return image

    image_bgr = _pil_to_bgr(image)
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(thresh > 0))
    if len(coords) < 50:
        return image

    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    if abs(angle) < 0.2:
        return image

    center = (image_bgr.shape[1] // 2, image_bgr.shape[0] // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(
        image_bgr,
        matrix,
        (image_bgr.shape[1], image_bgr.shape[0]),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return _bgr_to_pil(rotated)


def _order_quad_points(points):
    _require_cv2_numpy()
    pts = np.array(points, dtype="float32")
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1)
    top_left = pts[np.argmin(s)]
    bottom_right = pts[np.argmax(s)]
    top_right = pts[np.argmin(diff)]
    bottom_left = pts[np.argmax(diff)]
    return np.array([top_left, top_right, bottom_right, bottom_left], dtype="float32")


def detect_document_quad(image: Image.Image):
    if cv2 is None or np is None:
        return None

    image_bgr = _pil_to_bgr(image)
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)

    min_area = image.width * image.height * 0.2
    for contour in contours[:20]:
        area = cv2.contourArea(contour)
        if area < min_area:
            continue
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(polygon) == 4:
            return _order_quad_points(polygon.reshape(4, 2))
    return None


def perspective_correct(image: Image.Image):
    if cv2 is None or np is None:
        return image, False

    quad = detect_document_quad(image)
    if quad is None:
        return image, False

    width_top = math.dist(quad[0], quad[1])
    width_bottom = math.dist(quad[3], quad[2])
    height_left = math.dist(quad[0], quad[3])
    height_right = math.dist(quad[1], quad[2])
    target_width = int(max(width_top, width_bottom))
    target_height = int(max(height_left, height_right))
    if target_width < 100 or target_height < 100:
        return image, False

    destination = np.array(
        [
            [0, 0],
            [target_width - 1, 0],
            [target_width - 1, target_height - 1],
            [0, target_height - 1],
        ],
        dtype="float32",
    )
    image_bgr = _pil_to_bgr(image)
    matrix = cv2.getPerspectiveTransform(quad, destination)
    warped = cv2.warpPerspective(image_bgr, matrix, (target_width, target_height))
    return _bgr_to_pil(warped), True


def normalize_image(
    source: str | Path,
    output_dir: str | Path,
    *,
    page_id: str | None = None,
    page_index: int = 0,
    enable_perspective: bool = True,
    enable_deskew: bool = True,
    enable_margin_crop: bool = True,
    max_dimension: int | None = None,
    base_metadata: dict[str, Any] | None = None,
) -> NormalizedPageImage:
    source_path = Path(source)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    image = load_image(source_path)
    metadata: dict[str, Any] = dict(base_metadata or {})
    metadata.setdefault("source_type", "image")

    if enable_perspective:
        image, changed = perspective_correct(image)
        metadata["perspective_corrected"] = changed
    if enable_deskew:
        deskew_skip_reason = _deskew_skip_reason_for_text_markers(metadata)
        if deskew_skip_reason:
            metadata["deskewed"] = False
            metadata["deskew_skipped_reason"] = deskew_skip_reason
        else:
            image = deskew_image(image)
            metadata["deskewed"] = True
    if enable_margin_crop:
        image, crop_box = _crop_uniform_margin_with_box(image)
        metadata["margin_crop_box"] = {
            "left": crop_box[0],
            "top": crop_box[1],
            "right": crop_box[2],
            "bottom": crop_box[3],
        }
        _transform_pdf_text_geometry(metadata, offset_x=float(crop_box[0]), offset_y=float(crop_box[1]))
        metadata["margin_cropped"] = True

    if max_dimension:
        width, height = image.size
        scale = min(max_dimension / max(width, height), 1.0)
        if scale < 1.0:
            new_size = (int(round(width * scale)), int(round(height * scale)))
            image = image.resize(new_size, Image.Resampling.LANCZOS)
            _transform_pdf_text_geometry(metadata, scale=scale)
            metadata["resized_to_max_dimension"] = max_dimension

    resolved_page_id = page_id or f"{source_path.stem}-page-{page_index + 1:03d}"
    out_path = out_dir / f"{resolved_page_id}.png"
    image.save(out_path)
    return NormalizedPageImage(
        page_id=resolved_page_id,
        source_path=str(source_path),
        normalized_path=str(out_path),
        page_index=page_index,
        width_px=image.width,
        height_px=image.height,
        metadata=metadata,
    )


def prepare_pages(
    source: str | Path,
    output_dir: str | Path,
    *,
    dpi: int = 160,
    enable_perspective: bool = True,
    enable_deskew: bool = True,
    enable_margin_crop: bool = True,
    max_dimension: int | None = None,
) -> list[NormalizedPageImage]:
    source_path = Path(source)
    suffix = source_path.suffix.lower()
    normalized_dir = Path(output_dir)
    normalized_dir.mkdir(parents=True, exist_ok=True)

    if suffix == ".pdf":
        cached_pdf_pages = _load_cached_pdf_normalized_pages(
            source_path,
            normalized_dir,
            dpi=dpi,
            enable_deskew=enable_deskew,
            enable_margin_crop=enable_margin_crop,
            max_dimension=max_dimension,
        )
        if cached_pdf_pages:
            return cached_pdf_pages

        rendered = render_pdf_pages(source_path, normalized_dir / "rendered", dpi=dpi)
        normalized_pages = _normalize_pdf_rendered_pages(
            source_path,
            rendered,
            _pdf_normalized_output_dir(
                normalized_dir,
                source_path,
                dpi=dpi,
                enable_deskew=enable_deskew,
                enable_margin_crop=enable_margin_crop,
                max_dimension=max_dimension,
            ),
            enable_deskew=enable_deskew,
            enable_margin_crop=enable_margin_crop,
            max_dimension=max_dimension,
        )
        _save_pdf_normalized_pages_cache(
            source_path,
            normalized_dir,
            dpi=dpi,
            enable_deskew=enable_deskew,
            enable_margin_crop=enable_margin_crop,
            max_dimension=max_dimension,
            pages=normalized_pages,
        )
        return normalized_pages

    if suffix in HWP_DOCUMENT_EXTENSIONS:
        cached_hwp_pages = _load_cached_hwp_normalized_pages(
            source_path,
            normalized_dir,
            dpi=dpi,
            enable_deskew=enable_deskew,
            enable_margin_crop=enable_margin_crop,
            max_dimension=max_dimension,
        )
        if cached_hwp_pages:
            return cached_hwp_pages

        hwp_inspection = inspect_hangul_document(source_path)
        can_try_direct_hwp_renderer = (
            hwp_inspection.get("hwp_signature") == "HWP Document File"
            or bool(hwp_inspection.get("hwpx_zip_file"))
        )
        if can_try_direct_hwp_renderer:
            rendered = _render_hwp_pages_with_rhwp_core(source_path, normalized_dir / "rhwp_core_rendered", dpi=dpi)
            if not rendered:
                rendered = _render_hwp_pages_with_rhwp_python(source_path, normalized_dir / "rhwp_python_rendered", dpi=dpi)
            if rendered:
                hwp_renderer = str(rendered[0].metadata.get("hwp_renderer") or "rhwp-core")
                conversion_quality: dict[str, Any] = {
                    "page_count": len(rendered),
                    "hwp_renderer": hwp_renderer,
                    "hwp_renderer_page_count": int(rendered[0].metadata.get("hwp_renderer_page_count") or len(rendered)),
                    "warnings": [],
                }
                for key in ("hwp_renderer_version", "hwp_renderer_core_version", "hwp_renderer_document_info"):
                    if key in rendered[0].metadata:
                        conversion_quality[key] = rendered[0].metadata[key]
                for key in (
                    "hwp_preview_text_length",
                    "hwp_text_extractor",
                    "hwp_text_numbered_problem_count",
                    "hwp_text_stem_problem_count",
                    "hwp_text_problem_snippet_count",
                    "hwp_text_problem_snippets",
                    "hwp_text_passage_range_count",
                    "hwp_text_passage_ranges",
                    "hwp_layout_extractor",
                    "hwp_layout_page_count",
                    "hwp_layout_problem_marker_count",
                    "hwp_layout_text_line_count",
                    "hwp_layout_text_run_count",
                    "hwp_layout_problem_numbers",
                    "hwp_layout_problem_markers",
                ):
                    if key in hwp_inspection:
                        conversion_quality[key] = hwp_inspection[key]
                hwp_preview_text = hwp_inspection.get("hwp_preview_text")
                normalized_pages: list[NormalizedPageImage] = []
                for page in rendered:
                    base_metadata = dict(page.metadata)
                    _attach_hwp_layout_problem_markers(
                        base_metadata,
                        conversion_quality=conversion_quality,
                        page_index=page.page_index,
                        dpi=dpi,
                    )
                    normalized = normalize_image(
                        page.normalized_path,
                        normalized_dir / "normalized",
                        page_id=page.page_id,
                        page_index=page.page_index,
                        enable_perspective=False,
                        enable_deskew=enable_deskew,
                        enable_margin_crop=enable_margin_crop,
                        max_dimension=max_dimension,
                        base_metadata=base_metadata,
                    )
                    normalized.metadata["source_type"] = "hwp"
                    normalized.metadata["document_like"] = True
                    normalized.metadata["source_hwp_path"] = str(source_path)
                    if isinstance(hwp_preview_text, str) and hwp_preview_text.strip():
                        normalized.metadata["hwp_preview_text"] = hwp_preview_text.strip()
                    normalized.metadata["hwp_conversion_quality"] = dict(conversion_quality)
                    normalized_pages.append(normalized)
                _save_hwp_normalized_pages_cache(
                    source_path,
                    normalized_dir,
                    dpi=dpi,
                    enable_deskew=enable_deskew,
                    enable_margin_crop=enable_margin_crop,
                    max_dimension=max_dimension,
                    pages=normalized_pages,
                )
                return normalized_pages
        converted_pdf = convert_hwp_to_pdf(source_path, normalized_dir / "converted")
        rendered = render_pdf_pages(converted_pdf, normalized_dir / "rendered", dpi=dpi)
        conversion_quality = _summarize_pdf_render_quality(rendered)
        conversion_quality = _prefer_pdf_text_stem_markers_when_numeric_sparse(rendered, conversion_quality)
        for key in (
            "hwp_preview_text_length",
            "hwp_text_extractor",
            "hwp_text_numbered_problem_count",
            "hwp_text_stem_problem_count",
            "hwp_text_problem_snippet_count",
            "hwp_text_problem_snippets",
            "hwp_text_passage_range_count",
            "hwp_text_passage_ranges",
            "hwp_layout_extractor",
            "hwp_layout_page_count",
            "hwp_layout_problem_marker_count",
            "hwp_layout_text_line_count",
            "hwp_layout_text_run_count",
            "hwp_layout_problem_numbers",
            "hwp_layout_problem_markers",
        ):
            if key in hwp_inspection:
                conversion_quality[key] = hwp_inspection[key]
        hwp_preview_text = hwp_inspection.get("hwp_preview_text")
        normalized_pages: list[NormalizedPageImage] = []
        for page in rendered:
            normalized = normalize_image(
                page.normalized_path,
                normalized_dir / "normalized",
                page_id=page.page_id,
                page_index=page.page_index,
                enable_perspective=False,
                enable_deskew=enable_deskew,
                enable_margin_crop=enable_margin_crop,
                max_dimension=max_dimension,
                base_metadata=dict(page.metadata),
            )
            normalized.metadata.setdefault("source_pdf_path", str(converted_pdf))
            normalized.metadata["source_type"] = "hwp"
            normalized.metadata["document_like"] = True
            normalized.metadata["source_hwp_path"] = str(source_path)
            normalized.metadata["converted_pdf_path"] = str(converted_pdf)
            if isinstance(hwp_preview_text, str) and hwp_preview_text.strip():
                normalized.metadata["hwp_preview_text"] = hwp_preview_text.strip()
            normalized.metadata["hwp_conversion_quality"] = dict(conversion_quality)
            normalized_pages.append(normalized)
        _save_hwp_normalized_pages_cache(
            source_path,
            normalized_dir,
            dpi=dpi,
            enable_deskew=enable_deskew,
            enable_margin_crop=enable_margin_crop,
            max_dimension=max_dimension,
            pages=normalized_pages,
        )
        return normalized_pages

    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        passthrough = _passthrough_image_page_if_possible(
            source_path,
            page_index=0,
            enable_perspective=enable_perspective,
            enable_deskew=enable_deskew,
            enable_margin_crop=enable_margin_crop,
            max_dimension=max_dimension,
        )
        if passthrough is not None:
            return [passthrough]

        try:
            image_source_sha1 = _file_sha1(source_path)
        except OSError:
            image_source_sha1 = None
        cached_image_pages = _load_cached_image_normalized_pages(
            source_path,
            normalized_dir,
            enable_perspective=enable_perspective,
            enable_deskew=enable_deskew,
            enable_margin_crop=enable_margin_crop,
            max_dimension=max_dimension,
            source_sha1=image_source_sha1,
        )
        if cached_image_pages:
            return cached_image_pages

        normalized_pages = [
            normalize_image(
                source_path,
                _image_normalized_output_dir(
                    normalized_dir,
                    source_path,
                    enable_perspective=enable_perspective,
                    enable_deskew=enable_deskew,
                    enable_margin_crop=enable_margin_crop,
                    max_dimension=max_dimension,
                    source_sha1=image_source_sha1,
                ),
                page_index=0,
                enable_perspective=enable_perspective,
                enable_deskew=enable_deskew,
                enable_margin_crop=enable_margin_crop,
                max_dimension=max_dimension,
            )
        ]
        _save_image_normalized_pages_cache(
            source_path,
            normalized_dir,
            enable_perspective=enable_perspective,
            enable_deskew=enable_deskew,
            enable_margin_crop=enable_margin_crop,
            max_dimension=max_dimension,
            pages=normalized_pages,
            source_sha1=image_source_sha1,
        )
        return normalized_pages

    raise ValueError(f"Unsupported input type: {source_path.suffix}")


def prepare_source_pages(
    path: str | Path,
    pdf_dpi: int = 200,
    detect_perspective: bool = False,
    deskew: bool = True,
    crop_margins: bool = True,
    max_dimension: int | None = None,
) -> list[PreparedPage]:
    normalized_pages = prepare_pages(
        path,
        Path(path).parent / ".pipeline_cache",
        dpi=pdf_dpi,
        enable_perspective=detect_perspective,
        enable_deskew=deskew,
        enable_margin_crop=crop_margins,
        max_dimension=max_dimension,
    )
    prepared: list[PreparedPage] = []
    for page in normalized_pages:
        image = Image.open(page.normalized_path).convert("RGB")
        original_source_path = page.source_path
        if page.metadata.get("source_type") == "pdf" and page.metadata.get("source_pdf_path"):
            original_source_path = str(page.metadata["source_pdf_path"])
        if max_dimension:
            width, height = image.size
            scale = min(max_dimension / max(width, height), 1.0)
            if scale < 1.0:
                new_size = (int(round(width * scale)), int(round(height * scale)))
                image = image.resize(new_size, Image.Resampling.LANCZOS)
        prepared.append(
            PreparedPage(
                page_id=page.page_id,
                source_path=str(Path(page.normalized_path).resolve()),
                page_number=page.page_index + 1,
                image=image,
                original_size=(page.width_px, page.height_px),
                metadata={
                    **dict(page.metadata),
                    "original_source_path": str(Path(original_source_path).resolve()),
                    "normalized_path": str(Path(page.normalized_path).resolve()),
                },
            )
    )
    return prepared


def prepare_source_pages_batch(
    paths: Sequence[str | Path],
    pdf_dpi: int = 200,
    detect_perspective: bool = False,
    deskew: bool = True,
    crop_margins: bool = True,
    max_dimension: int | None = None,
) -> list[PreparedPage]:
    source_paths = [Path(path) for path in paths]
    if not source_paths:
        return []
    if len(source_paths) == 1:
        return prepare_source_pages(
            source_paths[0],
            pdf_dpi=pdf_dpi,
            detect_perspective=detect_perspective,
            deskew=deskew,
            crop_margins=crop_margins,
            max_dimension=max_dimension,
        )

    prepared_pages: list[PreparedPage] = []
    page_counter = 0
    for source_index, source_path in enumerate(source_paths, start=1):
        cache_dir = source_path.parent / ".pipeline_cache" / f"batch_{source_index:03d}_{source_path.stem}"
        normalized_pages = prepare_pages(
            source_path,
            cache_dir,
            dpi=pdf_dpi,
            enable_perspective=detect_perspective,
            enable_deskew=deskew,
            enable_margin_crop=crop_margins,
            max_dimension=max_dimension,
        )

        for local_page_index, page in enumerate(normalized_pages, start=1):
            page_counter += 1
            image = Image.open(page.normalized_path).convert("RGB")
            if max_dimension:
                width, height = image.size
                scale = min(max_dimension / max(width, height), 1.0)
                if scale < 1.0:
                    new_size = (int(round(width * scale)), int(round(height * scale)))
                    image = image.resize(new_size, Image.Resampling.LANCZOS)

            prepared_pages.append(
                PreparedPage(
                    page_id=f"{source_path.stem}-{source_index:02d}-page-{local_page_index:03d}",
                    source_path=str(Path(page.normalized_path).resolve()),
                    page_number=page_counter,
                    image=image,
                    original_size=(page.width_px, page.height_px),
                    metadata={
                        **dict(page.metadata),
                        "original_source_path": str(source_path.resolve()),
                        "normalized_path": str(Path(page.normalized_path).resolve()),
                        "batch_source_index": source_index,
                        "batch_total_sources": len(source_paths),
                        "original_page_index": page.page_index + 1,
                    },
                )
            )
    return prepared_pages


def load_pages(source: str | Path, options: PreprocessOptions) -> list[NormalizedPageImage]:
    normalized_pages = prepare_pages(
        source,
        Path(source).parent / ".pipeline_cache",
        dpi=options.dpi,
        enable_perspective=options.enable_perspective,
        enable_deskew=options.enable_deskew,
        enable_margin_crop=options.enable_margin_crop,
        max_dimension=options.max_dimension,
    )
    return normalized_pages
