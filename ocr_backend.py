#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import io
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from PIL import Image

from ai_usage import normalize_gemini_token_usage
from structured_schema import Box

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_GEMINI_OCR_MODEL = "gemini-3.5-flash"
ECONOMY_GEMINI_OCR_MODEL = "gemini-3.5-flash-lite"
LATEST_GEMINI_OCR_MODEL = "gemini-3.8-flash"
GEMINI_OCR_PROFILE_ENV = "EDB_GEMINI_OCR_PROFILE"
GEMINI_OCR_MODEL_ENV = "EDB_GEMINI_OCR_MODEL"
GEMINI_OCR_THINKING_LEVEL_ENV = "EDB_GEMINI_OCR_THINKING_LEVEL"
DEFAULT_GEMINI_FLASH_OCR_THINKING_LEVEL = "minimal"
DEFAULT_GEMINI_FALLBACK_OCR_THINKING_LEVEL = "low"
FALLBACK_GEMINI_OCR_MODEL = "gemini-3.6-flash"
DEPRECATED_GEMINI_OCR_MODELS = {"gemini-3-pro-preview", "gemini-3.1-pro-preview"}
DEFAULT_GEMINI_FAILURE_BACKOFF_SECONDS = 8.0
DEFAULT_GEMINI_FATAL_BACKOFF_SECONDS = 120.0
MAX_GEMINI_FAILURE_BACKOFF_SECONDS = 300.0
DEFAULT_OCR_NEGATIVE_CAPABILITY_TTL_SECONDS = 30.0
OCR_CACHE_SCHEMA_VERSION = "ocr_result_v4"
GEMINI_OCR_PROMPT_VERSION = "korean_exam_exact_text_v4_compact"

_OCR_CAPABILITY_LOCK = threading.RLock()
_GEMINI_FAILURE_LOCK = threading.RLock()


@dataclass(slots=True)
class _GeminiFailureState:
    reason: str
    failure_count: int
    retry_at: float


_GEMINI_FAILURES: dict[tuple[str, str], _GeminiFailureState] = {}
_TESSERACT_NEGATIVE_PROBE_AT: dict[tuple[str, str], float] = {}

# Gemini's responseSchema follows an OpenAPI 3.0 subset: no `additionalProperties`,
# no `$ref`, no `oneOf`. Keep the shape simple and rely on `required` instead.
_OCR_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "block_type": {
            "type": "string",
            "enum": ["stem", "choice", "figure", "formula", "title", "explanation", "unknown"],
        },
        "confidence": {"type": "number"},
    },
    "required": ["text", "block_type", "confidence"],
}

GEMINI_OCR_PROMPT = (
    "Transcribe this Korean exam crop exactly. Return every visible character in reading order "
    "and preserve visual line breaks. Include all numbered questions/options and text above or "
    "below figures. Preserve Korean, Latin, digits, punctuation, math symbols, ①–⑤, and ㄱ/ㄴ/ㄷ. "
    "Use visible Unicode/plain text only: keep π as π and sin as sin; never emit LaTeX or "
    "backslash commands. Do not summarize, solve, correct, infer, or omit. Classify block_type "
    "as title, stem, choice, formula, figure, explanation, or unknown; use figure only when "
    "there is no useful text. Set confidence from 0 to 1 by legibility."
)
GEMINI_OCR_FALLBACK_PROMPT = (
    "This is a cropped block from a Korean exam paper. Extract ALL visible text exactly as "
    "written; do not summarize, paraphrase, solve, correct, repeat, or skip anything. Read the "
    "entire crop from top to bottom, including text above and below tables, diagrams, and "
    "figures. Include every visible problem number and all following answer choices. Preserve "
    "Korean characters, line breaks, math symbols, circled numbers ①②③④⑤, ㄱ/ㄴ/ㄷ markers, "
    "digits, parentheses, and punctuation. Return visible Unicode/plain text, never LaTeX or "
    "backslash commands: π stays π and sin stays sin. Classify block_type as title for a problem "
    "heading, stem for the question body, choice for standalone final options, formula for an "
    "equation, figure for a diagram/table/photo with no useful text, explanation for solution "
    "text, or unknown. Set confidence from 0 to 1 by legibility. Return exactly one schema JSON "
    "object and stop immediately after it."
)

try:
    from paddleocr import PaddleOCR  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    PaddleOCR = None

try:
    import pytesseract  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    pytesseract = None


# Tiny crops (height below this) get upscaled before OCR. Korean OCR engines
# need ~28–40 px x-height to hit decent accuracy; cropped exam blocks are
# often half that.
_OCR_MIN_HEIGHT_PX = 64
_OCR_UPSCALE_CAP = 3.0
_OCR_CROP_PADDING_PX = 6


def resolve_gemini_ocr_model(model: str | None = None) -> str:
    requested = (model or "").strip()
    if requested:
        return requested
    env_model = os.environ.get(GEMINI_OCR_MODEL_ENV, "").strip()
    if env_model:
        return env_model
    profile = os.environ.get(GEMINI_OCR_PROFILE_ENV, "balanced").strip().lower()
    if profile in {"economy", "economic", "low-cost", "lite"}:
        return ECONOMY_GEMINI_OCR_MODEL
    if profile in {"latest", "flagship", "high", "upgrade"}:
        return LATEST_GEMINI_OCR_MODEL
    return DEFAULT_GEMINI_OCR_MODEL


def resolve_gemini_ocr_thinking_level(model: str, thinking_level: str | None = None) -> str:
    requested = (thinking_level or "").strip().lower()
    if requested:
        return requested
    env_level = os.environ.get(GEMINI_OCR_THINKING_LEVEL_ENV, "").strip().lower()
    if env_level:
        return env_level
    normalized_model = (model or "").strip().lower()
    if normalized_model == ECONOMY_GEMINI_OCR_MODEL:
        return "minimal"
    if normalized_model == "gemini-3.5-flash":
        return DEFAULT_GEMINI_FLASH_OCR_THINKING_LEVEL
    if normalized_model in {"gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"}:
        return DEFAULT_GEMINI_FALLBACK_OCR_THINKING_LEVEL
    return ""


def _gemini_model_supports_thinking_level(model: str) -> bool:
    normalized_model = (model or "").strip().lower()
    return normalized_model.startswith("gemini-3")


def _gemini_model_candidates(primary: str) -> list[str]:
    primary = resolve_gemini_ocr_model(primary)
    candidates = [primary]
    if (
        primary == DEFAULT_GEMINI_OCR_MODEL
        or primary in DEPRECATED_GEMINI_OCR_MODELS
        or primary.startswith("gemini-3")
        or "preview" in primary
    ):
        candidates.append(FALLBACK_GEMINI_OCR_MODEL)
    return list(dict.fromkeys(model for model in candidates if model))


def _is_retryable_gemini_error(exc: Exception) -> bool:
    if _is_fatal_gemini_error(exc):
        return False
    if not isinstance(exc, urllib.error.HTTPError):
        return True
    status_code = int(exc.code)
    return status_code in {408, 409, 425, 429} or status_code >= 500


def _format_gemini_error(exc: Exception) -> str:
    if not isinstance(exc, urllib.error.HTTPError):
        return str(exc)
    body = getattr(exc, "_edb_response_body", None)
    if body is None:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        try:
            setattr(exc, "_edb_response_body", body)
        except Exception:
            pass
    if body:
        return f"Gemini request failed with HTTP {exc.code}: {body}"
    return str(exc)


def _is_fatal_gemini_error(exc: Exception) -> bool:
    if not isinstance(exc, urllib.error.HTTPError):
        return False
    status_code = int(exc.code)
    message = _format_gemini_error(exc).lower()
    if status_code in {400, 401, 403}:
        return True
    if status_code == 429 and any(
        marker in message
        for marker in (
            "api_key_invalid",
            "api key not found",
            "billing",
            "credits are depleted",
            "prepayment",
            "quota",
            "resource_exhausted",
        )
    ):
        return True
    return False


def _env_enabled(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _gemini_failure_key(api_key: str, model: str) -> tuple[str, str]:
    # Never store the API key itself in process-wide diagnostic state.
    fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
    return fingerprint, model


def _gemini_failure_reason(exc: Exception | None) -> str:
    if exc is None:
        return "empty_response"
    if _is_fatal_gemini_error(exc):
        return "authentication_or_quota_unavailable"
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{int(exc.code)}"
    if isinstance(exc, (TimeoutError, urllib.error.URLError)):
        return "network_or_timeout"
    return "upstream_unavailable"


def _gemini_fallback_message(reason: str) -> str:
    if reason == "authentication_or_quota_unavailable":
        return (
            "Gemini OCR authentication or quota is unavailable. "
            "The block remains image-based for review."
        )
    if reason in {"invalid_json_response", "empty_response"}:
        return (
            "Gemini OCR returned an unreadable response. "
            "The block remains image-based for review."
        )
    return (
        "Gemini OCR is temporarily unavailable. "
        "The block remains image-based for review."
    )


def _gemini_backoff_status(
    key: tuple[str, str],
) -> tuple[_GeminiFailureState | None, int]:
    now = time.monotonic()
    with _GEMINI_FAILURE_LOCK:
        state = _GEMINI_FAILURES.get(key)
        if state is None or state.retry_at <= now:
            return state, 0
        remaining_ms = max(1, int(round((state.retry_at - now) * 1000.0)))
        return state, remaining_ms


def _record_gemini_failure(
    key: tuple[str, str],
    reason: str,
    *,
    fatal: bool,
) -> int:
    env_name = (
        "EDB_GEMINI_OCR_FATAL_BACKOFF_SECONDS"
        if fatal
        else "EDB_GEMINI_OCR_FAILURE_BACKOFF_SECONDS"
    )
    default_seconds = (
        DEFAULT_GEMINI_FATAL_BACKOFF_SECONDS
        if fatal
        else DEFAULT_GEMINI_FAILURE_BACKOFF_SECONDS
    )
    base_seconds = _env_float(
        env_name,
        default_seconds,
        minimum=0.0,
        maximum=MAX_GEMINI_FAILURE_BACKOFF_SECONDS,
    )
    now = time.monotonic()
    with _GEMINI_FAILURE_LOCK:
        previous = _GEMINI_FAILURES.get(key)
        if previous is not None and previous.retry_at > now:
            # Several OCR blocks can already be in flight when one upstream
            # quota/auth incident occurs. Treat their near-simultaneous
            # failures as one circuit event instead of exponentiating the
            # cooldown once per block (120s -> 240s -> 300s).
            remaining_seconds = previous.retry_at - now
            if fatal and remaining_seconds < base_seconds:
                previous = _GeminiFailureState(
                    reason=reason,
                    failure_count=previous.failure_count,
                    retry_at=now + base_seconds,
                )
                _GEMINI_FAILURES[key] = previous
                remaining_seconds = base_seconds
            return int(round(remaining_seconds * 1000.0))
        failure_count = (previous.failure_count + 1) if previous else 1
        delay_seconds = min(
            MAX_GEMINI_FAILURE_BACKOFF_SECONDS,
            base_seconds * (2 ** min(failure_count - 1, 4)),
        )
        _GEMINI_FAILURES[key] = _GeminiFailureState(
            reason=reason,
            failure_count=failure_count,
            retry_at=now + delay_seconds,
        )
    return int(round(delay_seconds * 1000.0))


def _record_gemini_success(key: tuple[str, str]) -> None:
    with _GEMINI_FAILURE_LOCK:
        _GEMINI_FAILURES.pop(key, None)


def clear_gemini_failure_backoff() -> None:
    with _GEMINI_FAILURE_LOCK:
        _GEMINI_FAILURES.clear()


def _prep_crop_for_ocr(image: Image.Image) -> Image.Image:
    """Pad and upscale a block crop so per-character pixels are within the
    range Korean OCR engines expect. Returns a new image; the input is not
    mutated."""
    if image.width <= 0 or image.height <= 0:
        return image

    padded = image
    pad = _OCR_CROP_PADDING_PX
    if pad > 0:
        new_w = image.width + pad * 2
        new_h = image.height + pad * 2
        padded = Image.new("RGB", (new_w, new_h), (255, 255, 255))
        padded.paste(image.convert("RGB"), (pad, pad))

    if padded.height >= _OCR_MIN_HEIGHT_PX:
        return padded

    scale = min(_OCR_UPSCALE_CAP, _OCR_MIN_HEIGHT_PX / max(padded.height, 1))
    if scale <= 1.0:
        return padded
    new_size = (int(round(padded.width * scale)), int(round(padded.height * scale)))
    return padded.resize(new_size, Image.Resampling.LANCZOS)


@dataclass(slots=True)
class OCRLine:
    text: str
    confidence: float
    bbox: Box


@dataclass(slots=True)
class OCRResult:
    text: str
    confidence: float | None
    lines: list[OCRLine] = field(default_factory=list)
    backend_name: str = "none"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def engine(self) -> str:
        return self.backend_name


def _line_confidence_summary(lines: list[OCRLine]) -> dict[str, Any]:
    confidences = [line.confidence for line in lines if line.confidence is not None]
    if not confidences:
        return {
            "line_confidence_count": 0,
            "line_confidence_mean": None,
            "line_confidence_min": None,
            "line_confidence_max": None,
        }
    return {
        "line_confidence_count": len(confidences),
        "line_confidence_mean": sum(confidences) / len(confidences),
        "line_confidence_min": min(confidences),
        "line_confidence_max": max(confidences),
    }


def _build_ocr_metadata(
    *,
    backend: str,
    started_at: float,
    text: str,
    confidence: float | None,
    lines: list[OCRLine],
    extra: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    normalized_text = text.strip()
    diagnostics: dict[str, Any] = {
        "backend": backend,
        "backend_latency_ms": int(max(0.0, (time.perf_counter() - started_at) * 1000.0)),
        "line_count": len(lines),
        "empty_text": not bool(normalized_text),
        "text_length": len(normalized_text),
        "confidence_available": confidence is not None,
        "result_confidence": confidence,
    }
    diagnostics.update(_line_confidence_summary(lines))
    if error:
        diagnostics["error"] = error
    if extra:
        diagnostics.update(extra)
    if error:
        # Backend/network/tool failures must never become durable blank OCR.
        diagnostics["cacheable"] = False
    return diagnostics


class OCRBackend:
    name = "base"

    @property
    def engine_name(self) -> str:
        return self.name

    @property
    def cache_fingerprint(self) -> str:
        return ocr_cache_fingerprint(self.engine_name)

    def ocr_image(self, image: Image.Image) -> OCRResult:
        raise NotImplementedError

    def ocr_box(self, image: Image.Image, box: Box) -> OCRResult:
        crop = image.crop((int(box.left), int(box.top), int(box.right), int(box.bottom)))
        return self.ocr_image(crop)

    def recognize(self, image: Image.Image) -> OCRResult:
        return self.ocr_image(image)


class NoOcrBackend(OCRBackend):
    name = "none"

    def ocr_image(self, image: Image.Image) -> OCRResult:
        started_at = time.perf_counter()
        return OCRResult(
            text="",
            confidence=None,
            backend_name=self.name,
            metadata=_build_ocr_metadata(
                backend=self.name,
                started_at=started_at,
                text="",
                confidence=None,
                lines=[],
            ),
        )


NoOpOCRBackend = NoOcrBackend


class PaddleOCRBackend(OCRBackend):
    name = "paddleocr"

    def __init__(self, *, lang: str = "korean", use_angle_cls: bool = True) -> None:
        if PaddleOCR is None:
            raise RuntimeError("paddleocr is not installed")
        self.lang = lang
        self.use_angle_cls = bool(use_angle_cls)
        self.engine = PaddleOCR(lang=lang, use_angle_cls=use_angle_cls, show_log=False)

    @property
    def cache_fingerprint(self) -> str:
        return ocr_cache_fingerprint(
            self.engine_name,
            lang=self.lang,
            use_angle_cls=self.use_angle_cls,
        )

    def ocr_image(self, image: Image.Image) -> OCRResult:
        started_at = time.perf_counter()
        prepped = _prep_crop_for_ocr(image)
        try:
            raw = self.engine.ocr(prepped.convert("RGB"), cls=True)
        except Exception as exc:  # pragma: no cover - runtime fallback
            return OCRResult(
                text="",
                confidence=None,
                lines=[],
                backend_name=self.name,
                metadata=_build_ocr_metadata(
                    backend=self.name,
                    started_at=started_at,
                    text="",
                    confidence=None,
                    lines=[],
                    error=str(exc),
                ),
            )

        entries = raw[0] if raw else []
        lines: list[OCRLine] = []
        collected: list[str] = []
        confidences: list[float] = []

        for entry in entries or []:
            polygon, payload = entry
            text = str(payload[0]).strip()
            if not text:
                continue
            confidence = float(payload[1]) if len(payload) > 1 else 0.0
            xs = [point[0] for point in polygon]
            ys = [point[1] for point in polygon]
            lines.append(
                OCRLine(
                    text=text,
                    confidence=confidence,
                    bbox=Box.from_points(min(xs), min(ys), max(xs), max(ys)),
                )
            )
            collected.append(text)
            confidences.append(confidence)

        average_confidence = sum(confidences) / len(confidences) if confidences else None
        return OCRResult(
            text="\n".join(collected),
            confidence=average_confidence,
            lines=lines,
            backend_name=self.name,
            metadata=_build_ocr_metadata(
                backend=self.name,
                started_at=started_at,
                text="\n".join(collected),
                confidence=average_confidence,
                lines=lines,
            ),
        )


class TesseractOCRBackend(OCRBackend):
    name = "tesseract"

    def __init__(self, *, lang: str = "kor+eng") -> None:
        if pytesseract is None:
            raise RuntimeError("pytesseract is not installed")
        self.lang = lang

    @property
    def cache_fingerprint(self) -> str:
        return ocr_cache_fingerprint(self.engine_name, lang=self.lang)

    def ocr_image(self, image: Image.Image) -> OCRResult:
        started_at = time.perf_counter()
        prepped = _prep_crop_for_ocr(image)
        
        try:
            data = pytesseract.image_to_data(prepped, lang=self.lang, output_type=pytesseract.Output.DICT)
        except Exception as exc:
            return OCRResult(
                text="",
                confidence=None,
                lines=[],
                backend_name=self.name,
                metadata=_build_ocr_metadata(
                    backend=self.name,
                    started_at=started_at,
                    text="",
                    confidence=None,
                    lines=[],
                    error=str(exc),
                ),
            )

        lines: list[OCRLine] = []
        collected: list[str] = []
        confidences: list[float] = []

        for idx, text in enumerate(data.get("text", [])):
            cleaned = str(text).strip()
            if not cleaned:
                continue
            raw_conf = data["conf"][idx]
            confidence = float(raw_conf) / 100.0 if raw_conf not in {"-1", -1} else 0.0
            left = float(data["left"][idx])
            top = float(data["top"][idx])
            width = float(data["width"][idx])
            height = float(data["height"][idx])
            lines.append(
                OCRLine(
                    text=cleaned,
                    confidence=confidence,
                    bbox=Box(left=left, top=top, width=width, height=height),
                )
            )
            collected.append(cleaned)
            confidences.append(confidence)

        average_confidence = sum(confidences) / len(confidences) if confidences else None
        return OCRResult(
            text="\n".join(collected),
            confidence=average_confidence,
            lines=lines,
            backend_name=self.name,
            metadata=_build_ocr_metadata(
                backend=self.name,
                started_at=started_at,
                text="\n".join(collected),
                confidence=average_confidence,
                lines=lines,
            ),
        )


class GeminiOCRBackend(OCRBackend):
    """OCR backend that uses Google Gemini vision API for text extraction and block classification."""

    name = "gemini"

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        timeout_ms: int = 15000,
        max_tokens: int = 1024,
        max_retries: int = 1,
        thinking_level: str | None = None,
        failure_backoff: bool = True,
    ) -> None:
        self.model = resolve_gemini_ocr_model(model)
        self.thinking_level = resolve_gemini_ocr_thinking_level(self.model, thinking_level)
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable is required for GeminiOCRBackend")
        self.timeout_s = timeout_ms / 1000.0
        self.max_tokens = max_tokens
        self.max_retries = max(0, max_retries)
        self.failure_backoff = bool(failure_backoff)
        self._failure_key = _gemini_failure_key(self.api_key, self.model)

    @property
    def cache_fingerprint(self) -> str:
        return ocr_cache_fingerprint(
            self.engine_name,
            model=self.model,
            thinking_level=self.thinking_level,
            max_tokens=self.max_tokens,
        )

    def _encode_image(self, image: Image.Image) -> tuple[str, str]:
        """Pick JPEG for large crops (smaller payload), PNG for small crops
        (lossless for tiny text). Returns (media_type, base64-data)."""
        rgb = image.convert("RGB")
        if rgb.height < 200 or rgb.width < 200:
            buf = io.BytesIO()
            rgb.save(buf, format="PNG", optimize=True)
            return "image/png", base64.b64encode(buf.getvalue()).decode("ascii")
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=92, optimize=True)
        return "image/jpeg", base64.b64encode(buf.getvalue()).decode("ascii")

    def _image_to_base64(self, image: Image.Image) -> str:
        # Kept for backward compatibility with external callers.
        _, data = self._encode_image(image)
        return data

    def ocr_image(self, image: Image.Image) -> OCRResult:
        started_at = time.perf_counter()
        if self.failure_backoff and _env_enabled("EDB_GEMINI_OCR_FAILURE_BACKOFF", True):
            failure_state, remaining_ms = _gemini_backoff_status(self._failure_key)
            if failure_state is not None and remaining_ms > 0:
                reason = failure_state.reason
                return OCRResult(
                    text="",
                    confidence=None,
                    lines=[],
                    backend_name=self.name,
                    metadata=_build_ocr_metadata(
                        backend=self.name,
                        started_at=started_at,
                        text="",
                        confidence=None,
                        lines=[],
                        error="Gemini OCR request paused after a recent failure",
                        extra={
                            "model": self.model,
                            "fallback_reason": reason,
                            "fallback_message": _gemini_fallback_message(reason),
                            "circuit_open": True,
                            "retry_after_ms": remaining_ms,
                            "cacheable": False,
                            "model_attempts": [],
                        },
                    ),
                )
        prepped = _prep_crop_for_ocr(image)
        media_type, image_data = self._encode_image(prepped)
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inline_data": {"mime_type": media_type, "data": image_data}},
                        {"text": GEMINI_OCR_PROMPT},
                    ],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _OCR_RESPONSE_SCHEMA,
                "maxOutputTokens": self.max_tokens,
                "temperature": 0.0,
            },
        }
        model_candidates = _gemini_model_candidates(self.model)
        used_model = model_candidates[0]
        url = f"{GEMINI_API_BASE}/{used_model}:generateContent?key={self.api_key}"
        headers = {"Content-Type": "application/json"}
        thinking_levels_by_model: dict[str, str] = {}

        def _body_for_model(model: str) -> bytes:
            generation_config = dict(payload["generationConfig"])
            generation_config.pop("thinkingConfig", None)
            if model in {ECONOMY_GEMINI_OCR_MODEL, "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"}:
                # Newer Gemini models reject or ignore legacy sampling controls.
                generation_config.pop("temperature", None)
            thinking_level = (
                self.thinking_level
                if model == self.model
                else resolve_gemini_ocr_thinking_level(model)
            )
            if thinking_level and _gemini_model_supports_thinking_level(model):
                generation_config["thinkingConfig"] = {
                    "thinkingLevel": thinking_level,
                }
                thinking_levels_by_model[model] = thinking_level
            else:
                thinking_levels_by_model[model] = ""
            model_payload = dict(payload)
            if model == FALLBACK_GEMINI_OCR_MODEL:
                model_payload["contents"] = [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "inline_data": {
                                    "mime_type": media_type,
                                    "data": image_data,
                                }
                            },
                            {"text": GEMINI_OCR_FALLBACK_PROMPT},
                        ],
                    }
                ]
            model_payload["generationConfig"] = generation_config
            return json.dumps(model_payload).encode("utf-8")

        last_exc: Exception | None = None
        response_data: dict[str, Any] | None = None
        request_attempt_count = 0
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                time.sleep(min(1.5 * attempt, 4.0))
            try:
                body = _body_for_model(used_model)
                req = urllib.request.Request(url, data=body, method="POST", headers=headers)
                request_attempt_count += 1
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    response_data = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as exc:  # network / API errors → retry
                last_exc = exc
                if not _is_retryable_gemini_error(exc):
                    break

        model_attempts: list[dict[str, str]] = []
        if response_data is None:
            model_attempts.append(
                {
                    "model": used_model,
                    "status": "error",
                    "error": _format_gemini_error(last_exc) if last_exc else "no response",
                }
            )
            fallback_models = [] if last_exc and _is_fatal_gemini_error(last_exc) else model_candidates[1:]
            for fallback_model in fallback_models:
                used_model = fallback_model
                url = f"{GEMINI_API_BASE}/{used_model}:generateContent?key={self.api_key}"
                last_exc = None
                for attempt in range(self.max_retries + 1):
                    if attempt > 0:
                        time.sleep(min(1.5 * attempt, 4.0))
                    try:
                        body = _body_for_model(used_model)
                        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
                        request_attempt_count += 1
                        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                            response_data = json.loads(resp.read().decode("utf-8"))
                        break
                    except Exception as exc:
                        last_exc = exc
                        if not _is_retryable_gemini_error(exc):
                            break
                if response_data is not None:
                    model_attempts.append({"model": used_model, "status": "ok"})
                    break
                model_attempts.append(
                    {
                        "model": used_model,
                        "status": "error",
                        "error": _format_gemini_error(last_exc) if last_exc else "no response",
                    }
                )
        else:
            model_attempts.append({"model": used_model, "status": "ok"})

        if response_data is None:
            failure_reason = _gemini_failure_reason(last_exc)
            cooldown_ms = 0
            if self.failure_backoff and _env_enabled("EDB_GEMINI_OCR_FAILURE_BACKOFF", True):
                cooldown_ms = _record_gemini_failure(
                    self._failure_key,
                    failure_reason,
                    fatal=bool(last_exc and _is_fatal_gemini_error(last_exc)),
                )
            return OCRResult(
                text="",
                confidence=None,
                lines=[],
                backend_name=self.name,
                metadata=_build_ocr_metadata(
                    backend=self.name,
                    started_at=started_at,
                    text="",
                    confidence=None,
                    lines=[],
                    error=_format_gemini_error(last_exc) if last_exc else "no response",
                    extra={
                        "retry_count": self.max_retries,
                        "request_attempt_count": request_attempt_count,
                        "model_attempts": model_attempts,
                        "fallback_reason": failure_reason,
                        "fallback_message": _gemini_fallback_message(failure_reason),
                        "circuit_open": False,
                        "retry_after_ms": cooldown_ms,
                        "cacheable": False,
                    },
                ),
            )

        token_usage = normalize_gemini_token_usage(response_data)

        # Gemini returns structured output as a JSON string inside the first
        # text part of candidate[0].
        json_text = _gemini_extract_text(response_data)
        if not json_text:
            finish_reason = _gemini_finish_reason(response_data)
            failure_reason = "empty_response"
            cooldown_ms = 0
            if self.failure_backoff and _env_enabled("EDB_GEMINI_OCR_FAILURE_BACKOFF", True):
                cooldown_ms = _record_gemini_failure(
                    self._failure_key,
                    failure_reason,
                    fatal=False,
                )
            return OCRResult(
                text="",
                confidence=None,
                lines=[],
                backend_name=self.name,
                metadata=_build_ocr_metadata(
                    backend=self.name,
                    started_at=started_at,
                    text="",
                    confidence=None,
                    lines=[],
                    error=f"no text in response (finish={finish_reason})",
                    extra={
                        "retry_count": self.max_retries,
                        "request_attempt_count": request_attempt_count,
                        "model_attempts": model_attempts,
                        "fallback_reason": failure_reason,
                        "fallback_message": _gemini_fallback_message(failure_reason),
                        "circuit_open": False,
                        "retry_after_ms": cooldown_ms,
                        "cacheable": False,
                        "token_usage": token_usage,
                    },
                ),
            )

        try:
            parsed: Any = json.loads(json_text)
        except json.JSONDecodeError as exc:
            failure_reason = "invalid_json_response"
            cooldown_ms = 0
            if self.failure_backoff and _env_enabled("EDB_GEMINI_OCR_FAILURE_BACKOFF", True):
                cooldown_ms = _record_gemini_failure(
                    self._failure_key,
                    failure_reason,
                    fatal=False,
                )
            return OCRResult(
                text="",
                confidence=None,
                lines=[],
                backend_name=self.name,
                metadata=_build_ocr_metadata(
                    backend=self.name,
                    started_at=started_at,
                    text="",
                    confidence=None,
                    lines=[],
                    error=f"json decode failed: {exc}",
                    extra={
                        "retry_count": self.max_retries,
                        "request_attempt_count": request_attempt_count,
                        "model_attempts": model_attempts,
                        "fallback_reason": failure_reason,
                        "fallback_message": _gemini_fallback_message(failure_reason),
                        "circuit_open": False,
                        "retry_after_ms": cooldown_ms,
                        "cacheable": False,
                        "token_usage": token_usage,
                    },
                ),
            )

        if not isinstance(parsed, dict):
            parsed = {}
        raw_text = str(parsed.get("text", "")).strip()
        # The API returns text once. Derive line objects locally instead of
        # paying output tokens for a duplicate `lines` array.
        raw_lines = raw_text.splitlines()
        if not raw_text:
            failure_reason = "empty_response"
            cooldown_ms = 0
            if self.failure_backoff and _env_enabled("EDB_GEMINI_OCR_FAILURE_BACKOFF", True):
                cooldown_ms = _record_gemini_failure(
                    self._failure_key,
                    failure_reason,
                    fatal=False,
                )
            return OCRResult(
                text="",
                confidence=None,
                lines=[],
                backend_name=self.name,
                metadata=_build_ocr_metadata(
                    backend=self.name,
                    started_at=started_at,
                    text="",
                    confidence=None,
                    lines=[],
                    error="structured OCR response contained no usable text",
                    extra={
                        "retry_count": self.max_retries,
                        "request_attempt_count": request_attempt_count,
                        "model_attempts": model_attempts,
                        "fallback_reason": failure_reason,
                        "fallback_message": _gemini_fallback_message(failure_reason),
                        "circuit_open": False,
                        "retry_after_ms": cooldown_ms,
                        "cacheable": False,
                        "token_usage": token_usage,
                    },
                ),
            )
        try:
            confidence = float(parsed.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.0
        if not math.isfinite(confidence):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        block_type_hint = str(parsed.get("block_type", "unknown"))

        # Build OCRLine list (Gemini does not return per-line bboxes; use even
        # vertical splits of the original crop so downstream consumers still
        # have a usable coordinate hint).
        lines: list[OCRLine] = []
        image_h = float(image.height) or 1.0
        image_w = float(image.width) or 1.0
        for idx, line_text in enumerate(raw_lines):
            cleaned = str(line_text).strip()
            if not cleaned:
                continue
            line_h = image_h / max(len(raw_lines), 1)
            lines.append(
                OCRLine(
                    text=cleaned,
                    confidence=confidence,
                    bbox=Box(left=0.0, top=idx * line_h, width=image_w, height=line_h),
                )
            )

        _record_gemini_success(self._failure_key)
        return OCRResult(
            text=raw_text,
            confidence=confidence,
            lines=lines,
            backend_name=self.name,
            metadata=_build_ocr_metadata(
                backend=self.name,
                started_at=started_at,
                text=raw_text,
                confidence=confidence,
                lines=lines,
                extra={
                    "block_type_hint": block_type_hint,
                    "model": used_model,
                    "model_attempts": model_attempts,
                    "request_attempt_count": request_attempt_count,
                    "thinking_level": thinking_levels_by_model.get(used_model, ""),
                    "token_usage": token_usage,
                    "cacheable": True,
                },
            ),
        )


def _gemini_extract_text(response: dict[str, Any]) -> str:
    """Return the concatenated text from the first candidate's parts, or ''."""
    candidates = response.get("candidates") or []
    if not candidates:
        return ""
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    collected: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            collected.append(text)
    return "".join(collected)


def _gemini_finish_reason(response: dict[str, Any]) -> str:
    candidates = response.get("candidates") or []
    if not candidates:
        return "no_candidates"
    return str(candidates[0].get("finishReason") or "unknown")


@lru_cache(maxsize=16)
def _tesseract_binary_available_cached(path_env: str, command: str) -> bool:
    if pytesseract is None:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _tesseract_binary_available(*, refresh: bool = False) -> bool:
    """Return True only if the tesseract binary is callable.

    Version probing starts a subprocess in pytesseract, so cache it by PATH and
    configured command. ``refresh=True`` handles a binary installed while the
    desktop process is already running.
    """

    command = ""
    if pytesseract is not None:
        command = str(getattr(getattr(pytesseract, "pytesseract", None), "tesseract_cmd", ""))
    with _OCR_CAPABILITY_LOCK:
        cache_key = (os.environ.get("PATH", ""), command)
        if refresh:
            _tesseract_binary_available_cached.cache_clear()
            _TESSERACT_NEGATIVE_PROBE_AT.clear()
        negative_checked_at = _TESSERACT_NEGATIVE_PROBE_AT.get(cache_key)
        if negative_checked_at is not None:
            negative_ttl = _env_float(
                "EDB_OCR_NEGATIVE_CAPABILITY_TTL_SECONDS",
                DEFAULT_OCR_NEGATIVE_CAPABILITY_TTL_SECONDS,
                minimum=0.0,
                maximum=300.0,
            )
            if time.monotonic() - negative_checked_at >= negative_ttl:
                _tesseract_binary_available_cached.cache_clear()
                _TESSERACT_NEGATIVE_PROBE_AT.pop(cache_key, None)
        available = _tesseract_binary_available_cached(*cache_key)
        if available:
            _TESSERACT_NEGATIVE_PROBE_AT.pop(cache_key, None)
        else:
            _TESSERACT_NEGATIVE_PROBE_AT.setdefault(cache_key, time.monotonic())
        return available


def clear_ocr_capability_cache() -> None:
    with _OCR_CAPABILITY_LOCK:
        _tesseract_binary_available_cached.cache_clear()
        _TESSERACT_NEGATIVE_PROBE_AT.clear()


def clear_ocr_runtime_cache() -> None:
    """Refresh local capability probes and forget temporary Gemini failures."""

    clear_ocr_capability_cache()
    clear_gemini_failure_backoff()


@lru_cache(maxsize=16)
def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


@lru_cache(maxsize=4)
def _tesseract_tool_version() -> str:
    if pytesseract is None:
        return "unavailable"
    try:
        return str(pytesseract.get_tesseract_version())
    except Exception:
        return "unavailable"


def ocr_cache_fingerprint(
    backend_name: str | None,
    *,
    model: str | None = None,
    thinking_level: str | None = None,
    max_tokens: int = 1024,
    lang: str | None = None,
    use_angle_cls: bool = True,
) -> str:
    """Return a code-owned cache namespace for OCR behavior and tool versions."""

    raw_backend_name = str(backend_name or "none").strip()
    normalized = raw_backend_name.lower()
    if normalized.startswith("gemini-") and model is None:
        model = raw_backend_name
    if normalized == "google" or normalized.startswith("gemini-"):
        normalized = "gemini"
    payload: dict[str, Any] = {
        "schema": OCR_CACHE_SCHEMA_VERSION,
        "backend": normalized,
    }
    if normalized in {"gemini", "gemini_escalated"}:
        resolved_model = resolve_gemini_ocr_model(model)
        payload.update(
            {
                "model": resolved_model,
                "thinking_level": resolve_gemini_ocr_thinking_level(
                    resolved_model,
                    thinking_level,
                ),
                "max_tokens": int(max_tokens),
                "prompt_version": GEMINI_OCR_PROMPT_VERSION,
                "response_schema": _OCR_RESPONSE_SCHEMA,
            }
        )
    elif normalized == "tesseract":
        command = str(getattr(getattr(pytesseract, "pytesseract", None), "tesseract_cmd", ""))
        payload.update(
            {
                "lang": lang or "kor+eng",
                "pytesseract_version": _distribution_version("pytesseract"),
                "tesseract_version": _tesseract_tool_version(),
                "command": command,
            }
        )
    elif normalized in {"paddle", "paddleocr"}:
        payload.update(
            {
                "backend": "paddleocr",
                "lang": lang or "korean",
                "use_angle_cls": bool(use_angle_cls),
                "paddleocr_version": _distribution_version("paddleocr"),
                "paddlepaddle_version": _distribution_version("paddlepaddle"),
            }
        )
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def preferred_ocr_backend_name(name: str = "auto") -> str:
    """Resolve the backend family without constructing heavyweight engines."""

    normalized = str(name or "auto").strip().lower()
    if normalized in {"none", "noop"}:
        return "none"
    if normalized in {"local", "offline"}:
        if PaddleOCR is not None:
            return "paddleocr"
        if _tesseract_binary_available():
            return "tesseract"
        return "none"
    if normalized in {"paddle", "paddleocr"}:
        return "paddleocr"
    if normalized == "tesseract":
        return "tesseract"
    if normalized in {"gemini", "google"} or normalized.startswith("gemini-"):
        return "gemini"
    if os.environ.get("GEMINI_API_KEY", "").strip():
        return "gemini"
    if PaddleOCR is not None:
        return "paddleocr"
    if _tesseract_binary_available():
        return "tesseract"
    return "none"


def build_ocr_backend(name: str = "auto", *, refresh_capabilities: bool = False) -> OCRBackend:
    raw_name = (name or "auto").strip()
    normalized = raw_name.lower()
    if normalized in {"none", "noop"}:
        return NoOcrBackend()
    if normalized in {"local", "offline"}:
        if PaddleOCR is not None:
            try:
                return PaddleOCRBackend()
            except Exception:
                pass
        if _tesseract_binary_available(refresh=refresh_capabilities):
            try:
                return TesseractOCRBackend()
            except Exception:
                pass
        print(
            "[ocr_backend] WARNING: local OCR requested but no PaddleOCR or "
            "Tesseract engine is available; using NoOcrBackend.",
            flush=True,
        )
        return NoOcrBackend()
    if normalized in {"paddle", "paddleocr"}:
        return PaddleOCRBackend()
    if normalized == "tesseract":
        return TesseractOCRBackend()
    if normalized in {"gemini", "google"}:
        return GeminiOCRBackend()
    if normalized.startswith("gemini-"):
        return GeminiOCRBackend(model=raw_name)

    # --- auto selection: Korean exam pages with diagrams and math need a
    # vision-language model to read reliably, so prefer Gemini when an API
    # key is configured. Local engines stay as the offline fallback.
    if os.environ.get("GEMINI_API_KEY", "").strip():
        try:
            return GeminiOCRBackend()
        except Exception:
            pass
    if PaddleOCR is not None:
        try:
            return PaddleOCRBackend()
        except Exception:
            pass
    if _tesseract_binary_available(refresh=refresh_capabilities):
        try:
            return TesseractOCRBackend()
        except Exception:
            pass
    # Last resort. Surface this loudly: a silent NoOcr fallback is the most
    # common reason problem numbers, choices, and figures get split into
    # separate fake "problems" downstream.
    print(
        "[ocr_backend] WARNING: 'auto' resolved to NoOcrBackend — no OCR engine "
        "is available. Set GEMINI_API_KEY (in .env.local or the user settings UI) "
        "or install paddleocr / tesseract. Problem-number detection will be "
        "disabled and every detected band will become its own pseudo-problem.",
        flush=True,
    )
    return NoOcrBackend()


def create_ocr_backend(name: str = "auto", *, refresh_capabilities: bool = False) -> OCRBackend:
    return build_ocr_backend(name, refresh_capabilities=refresh_capabilities)


OcrResult = OCRResult
