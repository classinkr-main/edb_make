import pytest


def test_problem_asset_rendering_deduplicates_identical_tasks(monkeypatch, tmp_path):
    from PIL import Image

    import build_problem_board_edb as pipeline
    from structured_schema import Box, Subject

    source = Image.new("RGB", (320, 480), "white")
    bounds = Box(left=20.0, top=30.0, width=220.0, height=260.0)
    tasks = [
        pipeline._ProblemAssetTask(
            source_image=source,
            bounds=bounds,
            crop_path=tmp_path / f"crop-{index}.png",
            board_render_path=tmp_path / f"board-{index}.png",
            chalk_color=(238, 238, 226),
            subject=Subject.UNKNOWN,
        )
        for index in range(4)
    ]
    tasks[1].source_hints = ("question 2",)
    tasks[-1].text_priority = True
    render_calls = []

    def fake_render(task):
        render_calls.append(task.crop_path)
        task.crop_path.write_bytes(b"same-crop")
        task.board_render_path.write_bytes(b"same-board")
        task.rendered_media_regions = [{"kind": "image", "bbox": {"left": 1}}]
        return 220, 260

    monkeypatch.setattr(pipeline, "_render_problem_asset", fake_render)

    sizes = pipeline._render_problem_assets(tasks)

    assert sizes == [(220, 260)] * 4
    assert len(render_calls) == 2
    assert [task.crop_path.read_bytes() for task in tasks] == [b"same-crop"] * 4
    assert [task.board_render_path.read_bytes() for task in tasks] == [b"same-board"] * 4
    assert all(task.rendered_media_regions for task in tasks)


def setup_function(_function):
    # Runtime failure backoff is intentionally process-wide; isolate tests that
    # reuse the same synthetic API key.
    import ocr_backend

    ocr_backend.clear_ocr_runtime_cache()


def teardown_function(_function):
    import ocr_backend

    ocr_backend.clear_ocr_runtime_cache()


def test_pdf_text_marker_blocks_skip_backend_ocr(monkeypatch, tmp_path):
    from PIL import Image

    import build_structured_page_json as pipeline
    from page_repair import build_ai_fallback_config
    from preprocess import PreparedPage
    from structured_schema import Subject

    backend_factory_calls = []

    source = tmp_path / "page.png"
    Image.new("RGB", (600, 800), "white").save(source)
    prepared = PreparedPage(
        page_id="pdf-page-001",
        source_path=str(source),
        page_number=1,
        image=Image.open(source).convert("RGB"),
        original_size=(600, 800),
        metadata={
            "source_type": "pdf",
            "pdf_problem_markers": [
                {
                    "number": 1,
                    "text": "1. problem stem",
                    "bbox": {"left": 60, "top": 120, "right": 90, "bottom": 140},
                }
            ],
        },
    )
    def fail_if_backend_is_created(mode):
        backend_factory_calls.append(mode)
        raise AssertionError("trusted PDF marker page should not create OCR backend")

    monkeypatch.setattr(pipeline, "create_ocr_backend", fail_if_backend_is_created)

    page = pipeline.build_page_model(
        prepared,
        subject=Subject.MATH,
        ocr_mode="gemini",
        ai_config=build_ai_fallback_config(mode="off"),
    )

    assert len(page.problems) == 1
    assert page.blocks[0].text == "1."
    assert page.blocks[0].metadata["ocr_skipped_reason"] == "trusted_pdf_text_marker"
    assert page.blocks[0].metadata["ocr_backend"] == "pdf_text_marker"
    assert backend_factory_calls == []
    assert page.metadata["ai_stages"]["ocr"]["status"] == "skipped"
    assert page.metadata["ai_stages"]["ocr"]["skipped_block_count"] == 1


def test_marker_like_block_uses_backend_when_page_segmenter_is_not_pdf_markers(monkeypatch, tmp_path):
    from PIL import Image

    import build_structured_page_json as pipeline
    from ocr_backend import OCRResult
    from page_repair import build_ai_fallback_config
    from preprocess import PreparedPage
    from structured_schema import BlockType, Box, ContentBlock, PageModel, Subject

    class CountingBackend:
        engine_name = "fake_marker_guardrail"

        def __init__(self):
            self.calls = 0

        def recognize(self, image):
            self.calls += 1
            text = "recognized by backend"
            return OCRResult(
                text=text,
                confidence=0.91,
                backend_name=self.engine_name,
                metadata={
                    "empty_text": False,
                    "line_count": 0,
                    "text_length": len(text),
                },
            )

    source = tmp_path / "page.png"
    image = Image.new("RGB", (600, 800), "white")
    image.save(source)
    prepared = PreparedPage(
        page_id="marker-like-page-001",
        source_path=str(source),
        page_number=1,
        image=image,
        original_size=image.size,
        metadata={},
    )
    marker_like_block = ContentBlock(
        block_id="marker-like-page-001-block-001",
        block_type=BlockType.TITLE,
        bbox=Box.from_points(60, 120, 300, 240),
        reading_order=0,
        text="1.",
        confidence=1.0,
        metadata={
            "segmenter": "pdf-text-markers",
            "force_image_record": True,
            "force_problem_start": True,
            "problem_number": 1,
            "problem_number_source": "pdf_text_marker",
        },
    )
    segmented_page = PageModel(
        page_id=prepared.page_id,
        width_px=image.width,
        height_px=image.height,
        subject=Subject.MATH,
        source_path=str(source),
        blocks=[marker_like_block],
        metadata={"segmenter": "document"},
    )
    backend = CountingBackend()

    monkeypatch.setattr(pipeline, "create_ocr_backend", lambda _mode: backend)
    monkeypatch.setattr(
        pipeline,
        "segment_page",
        lambda prepared_page, page_id, subject: segmented_page,
    )

    page = pipeline.build_page_model(
        prepared,
        subject=Subject.MATH,
        ocr_mode="fake_marker_guardrail",
        ai_config=build_ai_fallback_config(mode="off"),
    )

    assert backend.calls == 1
    assert page.blocks[0].text == "recognized by backend"
    assert page.blocks[0].metadata["ocr_backend"] == "fake_marker_guardrail"


def test_uploaded_image_visual_problem_markers_skip_backend_ocr(monkeypatch, tmp_path):
    from PIL import Image, ImageDraw

    import build_structured_page_json as pipeline
    from page_repair import build_ai_fallback_config
    from preprocess import PreparedPage
    from structured_schema import Subject

    backend_factory_calls = []
    source = tmp_path / "uploaded.png"
    image = Image.new("RGB", (420, 800), "white")
    draw = ImageDraw.Draw(image)
    for top, number in [(60, 1), (430, 2)]:
        draw.text((32, top), f"{number}.", fill=(20, 20, 20))
        draw.text((72, top + 4), "problem stem text", fill=(20, 20, 20))
        for y in (top + 38, top + 62, top + 86):
            draw.rectangle((72, y, 350, y + 4), fill=(20, 20, 20))
        draw.text((52, top + 220), "① a        ② b", fill=(20, 20, 20))
        draw.text((52, top + 244), "③ c        ④ d", fill=(20, 20, 20))
    image.save(source)
    prepared = PreparedPage(
        page_id="uploaded-page-001",
        source_path=str(source),
        page_number=1,
        image=image,
        original_size=image.size,
        metadata={"source_type": "image", "original_source_path": str(source)},
    )

    def fail_if_backend_is_created(mode):
        backend_factory_calls.append(mode)
        raise AssertionError("visual image marker fast path should not create OCR backend")

    monkeypatch.setattr(pipeline, "create_ocr_backend", fail_if_backend_is_created)

    page = pipeline.build_page_model(
        prepared,
        subject=Subject.MATH,
        ocr_mode="gemini",
        ai_config=build_ai_fallback_config(mode="off"),
    )

    assert backend_factory_calls == []
    assert page.metadata["segmenter"] == "visual-problem-markers"
    assert page.metadata["ai_stages"]["ocr"]["status"] == "skipped"
    assert page.metadata["ocr_eligible_block_count"] == 0
    assert page.metadata["ocr_skipped_block_count"] == 2
    assert len(page.problems) == 2


def test_two_column_uploaded_image_visual_problem_markers_skip_backend_ocr(monkeypatch, tmp_path):
    from PIL import Image, ImageDraw

    import build_structured_page_json as pipeline
    from page_repair import build_ai_fallback_config
    from preprocess import PreparedPage
    from structured_schema import Subject

    backend_factory_calls = []
    source = tmp_path / "two-column-uploaded.png"
    image = Image.new("RGB", (800, 520), "white")
    draw = ImageDraw.Draw(image)
    for left, number in [(32, 1), (432, 2)]:
        top = 60
        draw.text((left, top), f"{number}.", fill=(20, 20, 20))
        draw.text((left + 40, top + 4), "problem stem text", fill=(20, 20, 20))
        for y in (top + 38, top + 62, top + 86):
            draw.rectangle((left + 40, y, left + 330, y + 4), fill=(20, 20, 20))
        draw.text((left + 20, top + 220), "① a        ② b", fill=(20, 20, 20))
        draw.text((left + 20, top + 244), "③ c        ④ d", fill=(20, 20, 20))
    image.save(source)
    prepared = PreparedPage(
        page_id="two-column-uploaded-page-001",
        source_path=str(source),
        page_number=1,
        image=image,
        original_size=image.size,
        metadata={"source_type": "image", "original_source_path": str(source)},
    )

    def fail_if_backend_is_created(mode):
        backend_factory_calls.append(mode)
        raise AssertionError("two-column visual marker fast path should not create OCR backend")

    monkeypatch.setattr(pipeline, "create_ocr_backend", fail_if_backend_is_created)

    page = pipeline.build_page_model(
        prepared,
        subject=Subject.MATH,
        ocr_mode="gemini",
        ai_config=build_ai_fallback_config(mode="off"),
    )

    assert backend_factory_calls == []
    assert page.metadata["segmenter"] == "visual-problem-markers"
    assert page.metadata["column_count"] == 2
    assert page.metadata["ai_stages"]["ocr"]["status"] == "skipped"
    assert page.metadata["ocr_eligible_block_count"] == 0
    assert page.metadata["ocr_skipped_block_count"] == 2
    assert len(page.problems) == 2


def test_ocr_cache_uses_stable_index_and_rejects_legacy_image_hash_for_versioned_identity(tmp_path):
    from PIL import Image

    import pipeline_cache
    from ocr_backend import OCRResult
    from pipeline_cache import PipelineCache

    cache = PipelineCache(tmp_path / "cache")
    image = Image.new("RGB", (40, 20), "white")
    result = OCRResult(text="cached", confidence=0.9, backend_name="gemini")
    identity = {
        "version": "ocr_block_identity_v1",
        "source": {"fingerprint": "source-a"},
        "block": {"id": "block-1"},
    }

    def fail_image_hash(_image):
        raise AssertionError("stable OCR identity should avoid image hashing")

    original_image_hash = pipeline_cache._image_hash
    pipeline_cache._image_hash = fail_image_hash
    try:
        payload_path = cache.save_ocr_result(
            image,
            result,
            backend_name="gemini",
            cache_identity=identity,
        )
    finally:
        pipeline_cache._image_hash = original_image_hash

    assert "ocr" in payload_path.parts
    assert "ocr_index" not in payload_path.parts
    assert payload_path.name.startswith("stable-")

    changed_crop = Image.new("RGB", (41, 20), "white")
    loaded = cache.load_ocr_result(
        changed_crop,
        backend_name="gemini",
        cache_identity=identity,
    )
    assert loaded is not None
    assert loaded.text == "cached"
    assert loaded.metadata["cache_key_kind"] == "stable_index"

    legacy_cache = PipelineCache(tmp_path / "legacy")
    legacy_cache.save_ocr_result(image, result, backend_name="gemini")
    legacy_loaded = legacy_cache.load_ocr_result(
        image,
        backend_name="gemini",
        cache_identity={"version": "missing"},
    )
    assert legacy_loaded is None


def test_ocr_cache_identity_uses_original_source_normalization_and_bucketed_bbox(tmp_path):
    from PIL import Image

    import build_structured_page_json as pipeline
    from preprocess import PreparedPage
    from structured_schema import BlockType, Box, ContentBlock

    original = tmp_path / "source.pdf"
    original.write_bytes(b"%PDF-1.7\nstable-source\n")
    normalized = tmp_path / "normalized.png"
    image = Image.new("RGB", (200, 100), "white")
    image.save(normalized)

    prepared = PreparedPage(
        page_id="source-page-003",
        source_path=str(normalized),
        page_number=3,
        image=image,
        original_size=image.size,
        metadata={
            "original_source_path": str(original),
            "normalized_path": str(normalized),
            "source_type": "pdf",
            "dpi": 144,
            "deskewed": False,
            "margin_cropped": True,
            "margin_crop_box": {"left": 2, "top": 4, "right": 202, "bottom": 104},
            "backend_latency_ms": 999,
        },
    )
    block = ContentBlock(
        block_id="source-page-003-block-001",
        block_type=BlockType.STEM,
        bbox=Box.from_points(10.2, 20.1, 110.6, 60.7),
        reading_order=0,
        text="do not key from OCR text",
        confidence=0.2,
        metadata={"ocr_backend": "gemini", "ocr_latency_ms": 123},
    )
    shifted_block = ContentBlock(
        block_id=block.block_id,
        block_type=block.block_type,
        bbox=Box.from_points(10.9, 21.1, 111.4, 61.4),
        reading_order=block.reading_order,
        text="changed OCR text",
        confidence=0.99,
        metadata={"ocr_backend": "paddleocr", "ocr_latency_ms": 456},
    )

    identity = pipeline._build_ocr_cache_identity(prepared, block)
    shifted_identity = pipeline._build_ocr_cache_identity(prepared, shifted_block)
    serialized = str(identity)

    assert identity["source"]["path"] == str(original.resolve())
    assert identity["page"]["number"] == 3
    assert identity["page"]["size"] == {"width": 200, "height": 100}
    assert identity["normalization"]["dpi"] == 144
    assert identity["block"]["bbox_bucket_px"] == shifted_identity["block"]["bbox_bucket_px"]
    assert "do not key from OCR text" not in serialized
    assert "backend_latency" not in serialized
    assert "ocr_latency" not in serialized


def test_ocr_cache_identity_ignores_segmentation_labels_when_bbox_bucket_matches(tmp_path):
    from PIL import Image

    import build_structured_page_json as pipeline
    from preprocess import PreparedPage
    from structured_schema import BlockType, Box, ContentBlock

    source = tmp_path / "source.png"
    image = Image.new("RGB", (240, 160), "white")
    image.save(source)
    prepared = PreparedPage(
        page_id="page-001",
        source_path=str(source),
        page_number=1,
        image=image,
        original_size=image.size,
        metadata={"original_source_path": str(source)},
    )
    first = ContentBlock(
        block_id="page-001-block-001",
        block_type=BlockType.STEM,
        bbox=Box.from_points(20.1, 28.1, 120.2, 80.2),
        reading_order=0,
    )
    relabeled = ContentBlock(
        block_id="page-001-block-009",
        block_type=BlockType.TITLE,
        bbox=Box.from_points(20.4, 29.9, 120.1, 80.4),
        reading_order=8,
    )

    assert pipeline._build_ocr_cache_identity(prepared, first) == pipeline._build_ocr_cache_identity(prepared, relabeled)


def test_ocr_cache_identity_tracks_source_content_and_backend_configuration(tmp_path):
    import os

    from PIL import Image

    import build_structured_page_json as pipeline
    import ocr_backend
    from preprocess import PreparedPage
    from structured_schema import BlockType, Box, ContentBlock

    source = tmp_path / "source.bin"
    source.write_bytes(b"version-one")
    fixed_ns = 1_700_000_000_000_000_000
    os.utime(source, ns=(fixed_ns, fixed_ns))
    image = Image.new("RGB", (80, 60), "white")
    prepared = PreparedPage(
        page_id="page-001",
        source_path=str(source),
        page_number=1,
        image=image,
        original_size=image.size,
        metadata={"original_source_path": str(source)},
    )
    block = ContentBlock(
        block_id="block-001",
        block_type=BlockType.STEM,
        bbox=Box.from_points(0, 0, 40, 30),
        reading_order=0,
    )
    first_identity = pipeline._build_ocr_cache_identity(
        prepared,
        block,
        backend_fingerprint=ocr_backend.ocr_cache_fingerprint("gemini-2.5-pro"),
    )

    source.write_bytes(b"version-two")
    os.utime(source, ns=(fixed_ns, fixed_ns))
    changed_source_identity = pipeline._build_ocr_cache_identity(
        prepared,
        block,
        backend_fingerprint=ocr_backend.ocr_cache_fingerprint("gemini-2.5-pro"),
    )
    changed_model_identity = pipeline._build_ocr_cache_identity(
        prepared,
        block,
        backend_fingerprint=ocr_backend.ocr_cache_fingerprint("gemini-3.5-flash"),
    )

    assert first_identity["source"]["stat"] == changed_source_identity["source"]["stat"]
    assert first_identity["source"]["sha256"] != changed_source_identity["source"]["sha256"]
    assert changed_source_identity["backend_fingerprint"] != changed_model_identity["backend_fingerprint"]


def test_build_page_model_passes_stable_cache_identity_to_primary_and_escalated_ocr(
    monkeypatch,
    tmp_path,
):
    from PIL import Image

    import build_structured_page_json as pipeline
    from ocr_backend import OCRResult
    from page_repair import build_ai_fallback_config
    from preprocess import PreparedPage
    from structured_schema import BlockType, Box, ContentBlock, PageModel, Subject

    class PrimaryBackend:
        engine_name = "primary_ocr"

        def recognize(self, image):
            return OCRResult(text="", confidence=0.1, backend_name=self.engine_name)

    class EscalationBackend:
        engine_name = "gemini"

        def recognize(self, image):
            return OCRResult(text="escalated", confidence=0.95, backend_name=self.engine_name)

    class RecordingCache:
        def __init__(self):
            self.root_dir = tmp_path / "cache"
            self.load_calls = []
            self.save_calls = []

        def load_ocr_result(self, image, *, backend_name, cache_identity=None):
            self.load_calls.append((backend_name, cache_identity))
            return None

        def save_ocr_result(self, image, result, *, backend_name, cache_identity=None):
            self.save_calls.append((backend_name, cache_identity))
            return self.root_dir / "ocr" / backend_name / "payload.json"

    source = tmp_path / "page.png"
    image = Image.new("RGB", (180, 120), "white")
    image.save(source)
    prepared = PreparedPage(
        page_id="image-page-001",
        source_path=str(source),
        page_number=1,
        image=image,
        original_size=image.size,
        metadata={"original_source_path": str(source), "deskewed": False},
    )
    block = ContentBlock(
        block_id="image-page-001-block-001",
        block_type=BlockType.STEM,
        bbox=Box.from_points(10, 10, 90, 60),
        reading_order=0,
    )
    segmented_page = PageModel(
        page_id=prepared.page_id,
        width_px=image.width,
        height_px=image.height,
        subject=Subject.MATH,
        source_path=str(source),
        blocks=[block],
        metadata={"segmenter": "document"},
    )
    cache = RecordingCache()

    monkeypatch.setattr(pipeline, "create_ocr_backend", lambda _mode: PrimaryBackend())
    monkeypatch.setattr(
        pipeline,
        "_maybe_build_gemini_escalation",
        lambda **_kwargs: EscalationBackend(),
    )
    monkeypatch.setattr(
        pipeline,
        "segment_page",
        lambda prepared_page, page_id, subject: segmented_page,
    )

    page = pipeline.build_page_model(
        prepared,
        subject=Subject.MATH,
        ocr_mode="primary_ocr",
        ai_config=build_ai_fallback_config(mode="off", threshold=0.72),
        cache=cache,
    )

    assert page.blocks[0].text == "escalated"
    assert page.blocks[0].metadata["ocr_stage"] == "primary_ocr"
    assert page.blocks[0].metadata["ocr_escalation_attempted"] is True
    assert [call[0] for call in cache.load_calls] == ["primary_ocr", "gemini_escalated"]
    assert [call[0] for call in cache.save_calls] == ["primary_ocr", "gemini_escalated"]
    assert all(call[1] is not None for call in cache.load_calls + cache.save_calls)
    assert cache.load_calls[0][1] == cache.save_calls[0][1]
    assert cache.load_calls[1][1] == cache.save_calls[1][1]
    assert page.metadata["ai_stages"]["ocr"]["status"] == "used"
    assert page.metadata["ai_stages"]["ocr"]["api_call_block_count"] == 1
    assert page.metadata["ai_stages"]["ocr_escalation"]["attempted_block_count"] == 1
    assert page.metadata["ai_stages"]["ocr_escalation"]["applied_block_count"] == 1


def test_economy_ocr_latex_normalization_triggers_exactness_escalation():
    import build_structured_page_json as pipeline
    from ocr_backend import ECONOMY_GEMINI_OCR_MODEL, OCRResult

    result = OCRResult(
        text=r"3. 0<x<\pi에서 \sin x = 1/2",
        confidence=0.99,
        backend_name="gemini",
        metadata={"model": ECONOMY_GEMINI_OCR_MODEL},
    )

    assert pipeline._ocr_escalation_reason(result, threshold=0.72) == "exactness_risk"
    assert pipeline._ocr_needs_escalation(result, threshold=0.72) is True


def test_economy_ocr_truncated_stem_triggers_exactness_escalation():
    import build_structured_page_json as pipeline
    from ocr_backend import ECONOMY_GEMINI_OCR_MODEL, OCRResult

    result = OCRResult(
        text="3. 모든 x의 값을 구하시으",
        confidence=0.98,
        backend_name="gemini",
        metadata={
            "model": ECONOMY_GEMINI_OCR_MODEL,
            "block_type_hint": "stem",
        },
    )

    assert pipeline._ocr_escalation_reason(result, threshold=0.72) == "exactness_risk"


def test_exactness_escalation_accepts_balanced_text_despite_lower_self_confidence():
    import build_structured_page_json as pipeline
    from ocr_backend import ECONOMY_GEMINI_OCR_MODEL, OCRResult

    primary = OCRResult(
        text=r"3. 0<x<\pi에서 \sin x = 1/2",
        confidence=0.99,
        backend_name="gemini",
        metadata={"model": ECONOMY_GEMINI_OCR_MODEL},
    )
    balanced = OCRResult(
        text="3. 0<x<π에서 sin x = 1/2",
        confidence=0.98,
        backend_name="gemini",
        metadata={"model": "gemini-3.5-flash"},
    )

    assert pipeline._should_accept_ocr_escalation(
        primary,
        balanced,
        reason="exactness_risk",
    ) is True


def test_economy_ocr_escalates_to_balanced_quality_model(monkeypatch):
    import build_structured_page_json as pipeline
    from ocr_backend import DEFAULT_GEMINI_OCR_MODEL, ECONOMY_GEMINI_OCR_MODEL
    from page_repair import build_ai_fallback_config

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    backend = pipeline._maybe_build_gemini_escalation(
        ai_config=build_ai_fallback_config(mode="auto"),
        primary_backend_name="gemini",
        primary_model=ECONOMY_GEMINI_OCR_MODEL,
    )

    assert backend is not None
    assert backend.model == DEFAULT_GEMINI_OCR_MODEL


def test_explicit_backend_cache_hit_defers_backend_creation(monkeypatch, tmp_path):
    from PIL import Image

    import build_structured_page_json as pipeline
    from ocr_backend import OCRResult
    from page_repair import build_ai_fallback_config
    from preprocess import PreparedPage
    from structured_schema import BlockType, Box, ContentBlock, PageModel, Subject

    class HitCache:
        def __init__(self):
            self.root_dir = tmp_path / "cache"
            self.load_calls = []
            self.save_calls = []

        def load_ocr_result(self, image, *, backend_name, cache_identity=None):
            self.load_calls.append((backend_name, cache_identity))
            return OCRResult(
                text="cached text",
                confidence=0.96,
                backend_name="paddleocr",
                metadata={
                    "empty_text": False,
                    "line_count": 0,
                    "text_length": len("cached text"),
                },
            )

        def save_ocr_result(self, image, result, *, backend_name, cache_identity=None):
            self.save_calls.append((backend_name, cache_identity))
            raise AssertionError("cache hit should not write OCR result")

    source = tmp_path / "page.png"
    image = Image.new("RGB", (240, 160), "white")
    image.save(source)
    prepared = PreparedPage(
        page_id="cache-hit-page-001",
        source_path=str(source),
        page_number=1,
        image=image,
        original_size=image.size,
        metadata={"original_source_path": str(source)},
    )
    block = ContentBlock(
        block_id="cache-hit-page-001-block-001",
        block_type=BlockType.STEM,
        bbox=Box.from_points(20, 20, 180, 100),
        reading_order=0,
    )
    segmented_page = PageModel(
        page_id=prepared.page_id,
        width_px=image.width,
        height_px=image.height,
        subject=Subject.MATH,
        source_path=str(source),
        blocks=[block],
        metadata={"segmenter": "document"},
    )
    backend_factory_calls = []

    def fail_if_backend_is_created(mode):
        backend_factory_calls.append(mode)
        raise AssertionError("explicit OCR cache hit should avoid backend initialization")

    monkeypatch.setattr(pipeline, "create_ocr_backend", fail_if_backend_is_created)
    monkeypatch.setattr(
        pipeline,
        "segment_page",
        lambda prepared_page, page_id, subject: segmented_page,
    )

    cache = HitCache()
    page = pipeline.build_page_model(
        prepared,
        subject=Subject.MATH,
        ocr_mode="paddleocr",
        ai_config=build_ai_fallback_config(mode="off"),
        cache=cache,
    )

    assert backend_factory_calls == []
    assert [call[0] for call in cache.load_calls] == ["paddleocr"]
    assert cache.save_calls == []
    assert page.blocks[0].text == "cached text"
    assert page.metadata["ocr_cache_hit_count"] == 1
    assert page.metadata["ocr_cache_miss_count"] == 0
    assert page.metadata["ai_stages"]["ocr"]["api_call_block_count"] == 0


def test_page_worker_count_defaults_scale_for_basic_recognition(monkeypatch):
    import build_structured_page_json as pipeline

    monkeypatch.delenv("EDB_RECOGNITION_PAGE_WORKERS", raising=False)
    monkeypatch.delenv("EDB_RECOGNITION_WORKERS", raising=False)
    monkeypatch.delenv("EDB_AI_MAX_WORKERS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 16)

    assert pipeline.resolve_recognition_worker_count(8, ocr_mode="auto") == 4
    assert pipeline.resolve_recognition_worker_count(3, ocr_mode="noop") == 3
    assert pipeline.resolve_recognition_worker_count(1, ocr_mode="auto") == 1


def test_page_worker_count_defaults_respect_network_ai_cap(monkeypatch):
    import build_structured_page_json as pipeline

    monkeypatch.delenv("EDB_RECOGNITION_PAGE_WORKERS", raising=False)
    monkeypatch.delenv("EDB_RECOGNITION_WORKERS", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.delenv("EDB_AI_MAX_WORKERS", raising=False)

    assert pipeline.resolve_recognition_worker_count(8, ocr_mode="auto") == 3

    monkeypatch.setenv("EDB_AI_MAX_WORKERS", "5")
    assert pipeline.resolve_recognition_worker_count(8, ocr_mode="auto") == 3

    monkeypatch.setenv("EDB_RECOGNITION_PAGE_WORKERS", "8")
    assert pipeline.resolve_recognition_worker_count(8, ocr_mode="auto") == 5


def test_source_worker_count_uses_basic_recognition_default(monkeypatch):
    import build_problem_board_edb as builder
    import build_structured_page_json as pipeline

    monkeypatch.delenv("EDB_RECOGNITION_PAGE_WORKERS", raising=False)
    monkeypatch.delenv("EDB_RECOGNITION_WORKERS", raising=False)
    monkeypatch.delenv("EDB_AI_MAX_WORKERS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 16)

    assert (
        builder.resolve_source_build_worker_count(
            8,
            input_intent="multi-problem",
            ocr_mode="auto",
            ai_fallback_config={"mode": "off"},
        )
        == 4
    )


def test_block_worker_count_defaults_and_env_override(monkeypatch):
    import build_structured_page_json as pipeline

    monkeypatch.delenv("EDB_RECOGNITION_BLOCK_WORKERS", raising=False)
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 16)

    assert (
        pipeline.resolve_block_ocr_worker_count(
            5,
            ocr_mode="none",
            backend_name="none",
        )
        == 1
    )
    assert (
        pipeline.resolve_block_ocr_worker_count(
            10,
            ocr_mode="gemini",
            backend_name="gemini",
        )
        == 3
    )
    assert (
        pipeline.resolve_block_ocr_worker_count(
            10,
            ocr_mode="auto",
            backend_name="gemini",
        )
        == 3
    )
    assert (
        pipeline.resolve_block_ocr_worker_count(
            20,
            ocr_mode="paddleocr",
            backend_name="paddleocr",
        )
        == 1
    )
    assert (
        pipeline.resolve_block_ocr_worker_count(
            4,
            ocr_mode="paddleocr",
            backend_name="paddleocr",
        )
        == 1
    )

    monkeypatch.setenv("EDB_RECOGNITION_BLOCK_WORKERS", "6")
    assert (
        pipeline.resolve_block_ocr_worker_count(
            10,
            ocr_mode="gemini",
            backend_name="gemini",
        )
        == 3
    )

    monkeypatch.setenv("EDB_RECOGNITION_BLOCK_WORKERS", "100000")
    assert (
        pipeline.resolve_block_ocr_worker_count(
            2,
            ocr_mode="gemini",
            backend_name="gemini",
        )
        == 2
    )

    monkeypatch.setenv("EDB_RECOGNITION_BLOCK_WORKERS", "bogus")
    assert (
        pipeline.resolve_block_ocr_worker_count(
            10,
            ocr_mode="gemini",
            backend_name="gemini",
        )
        == 3
    )

    monkeypatch.setenv("EDB_RECOGNITION_BLOCK_WORKERS", "0")
    assert (
        pipeline.resolve_block_ocr_worker_count(
            0,
            ocr_mode="gemini",
            backend_name="gemini",
        )
        == 1
    )


def test_multipage_block_ocr_respects_one_global_ai_concurrency_cap(monkeypatch, tmp_path):
    import concurrent.futures
    import threading
    import time

    from PIL import Image

    import build_structured_page_json as pipeline
    from ocr_backend import OCRResult
    from page_repair import build_ai_fallback_config
    from preprocess import PreparedPage
    from structured_schema import BlockType, Box, ContentBlock, PageModel, Subject

    activity = {"active": 0, "maximum": 0}
    activity_lock = threading.Lock()

    class CountingBackend:
        engine_name = "gemini"

        def recognize(self, image):
            with activity_lock:
                activity["active"] += 1
                activity["maximum"] = max(activity["maximum"], activity["active"])
            try:
                time.sleep(0.025)
                return OCRResult(
                    text="recognized",
                    confidence=0.95,
                    backend_name=self.engine_name,
                )
            finally:
                with activity_lock:
                    activity["active"] -= 1

    def fake_segment(prepared_page, page_id, subject):
        blocks = [
            ContentBlock(
                block_id=f"{page_id}-block-{index}",
                block_type=BlockType.STEM,
                bbox=Box.from_points(0, index * 30, 100, index * 30 + 25),
                reading_order=index,
            )
            for index in range(4)
        ]
        return PageModel(
            page_id=page_id,
            width_px=prepared_page.image.width,
            height_px=prepared_page.image.height,
            subject=subject,
            source_path=prepared_page.source_path,
            blocks=blocks,
            metadata={"segmenter": "document"},
        )

    prepared_pages = []
    for page_index in range(3):
        source = tmp_path / f"page-{page_index}.png"
        image = Image.new("RGB", (120, 160), "white")
        image.save(source)
        prepared_pages.append(
            PreparedPage(
                page_id=f"page-{page_index}",
                source_path=str(source),
                page_number=page_index + 1,
                image=image,
                original_size=image.size,
                metadata={"original_source_path": str(source)},
            )
        )

    monkeypatch.setenv("GEMINI_API_KEY", "global-cap-test")
    monkeypatch.setenv("EDB_AI_MAX_WORKERS", "2")
    monkeypatch.setenv("EDB_RECOGNITION_PAGE_WORKERS", "3")
    monkeypatch.setenv("EDB_RECOGNITION_BLOCK_WORKERS", "4")
    monkeypatch.setattr(pipeline, "create_ocr_backend", lambda _mode: CountingBackend())
    monkeypatch.setattr(pipeline, "segment_page", fake_segment)
    monkeypatch.setattr(
        pipeline,
        "repair_page_model",
        lambda prepared_page, page, **kwargs: page,
    )

    shared_semaphore = threading.BoundedSemaphore(2)

    def build_one_source(prepared_page):
        return pipeline.build_page_models_for_prepared_pages(
            [prepared_page],
            subject=Subject.MATH,
            ocr_mode="gemini",
            ai_config=build_ai_fallback_config(mode="off"),
            ocr_semaphore=shared_semaphore,
            global_ocr_worker_limit=2,
        )[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        pages = list(executor.map(build_one_source, prepared_pages))

    assert len(pages) == 3
    assert activity["maximum"] == 2
    assert {page.metadata["global_ocr_worker_limit"] for page in pages} == {2}


def test_problem_export_shares_global_ocr_budget_across_sources():
    import inspect

    import build_problem_board_edb as builder

    source = inspect.getsource(builder.run_problem_export)
    assert "shared_ocr_semaphore = threading.BoundedSemaphore" in source
    assert "ocr_semaphore=shared_ocr_semaphore" in source
    assert "global_ocr_worker_limit=global_ocr_worker_limit" in source


def test_multipage_source_identity_hashes_original_once_and_refreshes_after_change(
    monkeypatch,
    tmp_path,
):
    from PIL import Image

    import build_structured_page_json as pipeline
    from preprocess import PreparedPage
    from structured_schema import PageModel, Subject

    original = tmp_path / "large-source.pdf"
    original.write_bytes(b"source-version-one")
    prepared_pages = [
        PreparedPage(
            page_id=f"page-{index + 1}",
            source_path=str(tmp_path / f"rendered-page-{index + 1}.png"),
            page_number=index + 1,
            image=Image.new("RGB", (80, 100), "white"),
            original_size=(80, 100),
            metadata={"original_source_path": str(original)},
        )
        for index in range(3)
    ]

    real_file_sha256 = pipeline._file_sha256
    hash_calls = []
    observed_hashes = []

    def counted_file_sha256(path):
        hash_calls.append(path)
        return real_file_sha256(path)

    def fake_build_page_model(prepared_page, subject, ocr_mode, **kwargs):
        observed_hashes.append(kwargs["source_identity"]["sha256"])
        return PageModel(
            page_id=prepared_page.page_id,
            width_px=80,
            height_px=100,
            subject=subject,
            source_path=prepared_page.source_path,
            blocks=[],
            metadata={},
        )

    monkeypatch.setenv("EDB_RECOGNITION_PAGE_WORKERS", "3")
    monkeypatch.setattr(pipeline, "_file_sha256", counted_file_sha256)
    monkeypatch.setattr(pipeline, "build_page_model", fake_build_page_model)

    first_pages = pipeline.build_page_models_for_prepared_pages(
        prepared_pages,
        subject=Subject.MATH,
        ocr_mode="none",
    )
    first_hash = observed_hashes[0]
    assert len(first_pages) == 3
    assert len(hash_calls) == 1
    assert set(observed_hashes) == {first_hash}

    original.write_bytes(b"source-version-two")
    observed_hashes.clear()
    second_pages = pipeline.build_page_models_for_prepared_pages(
        prepared_pages,
        subject=Subject.MATH,
        ocr_mode="none",
    )

    assert len(second_pages) == 3
    assert len(hash_calls) == 2
    assert len(set(observed_hashes)) == 1
    assert observed_hashes[0] != first_hash


def test_gemini_ocr_model_not_found_falls_back_without_retry_sleep(monkeypatch):
    import json
    from io import BytesIO
    from urllib.error import HTTPError

    from PIL import Image

    import ocr_backend

    urls = []
    sleep_calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            response_text = json.dumps(
                {
                    "text": "recognized",
                    "block_type": "stem",
                    "confidence": 0.96,
                    "lines": ["recognized"],
                }
            )
            return json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": response_text}],
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(req, timeout):
        urls.append(req.full_url)
        if "gemini-3.1-pro-preview" in req.full_url:
            raise HTTPError(
                req.full_url,
                404,
                "model not found",
                hdrs=None,
                fp=BytesIO(b"model not found"),
            )
        return FakeResponse()

    monkeypatch.setattr(ocr_backend.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        ocr_backend.time,
        "sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    monkeypatch.delenv(ocr_backend.GEMINI_OCR_MODEL_ENV, raising=False)
    backend = ocr_backend.GeminiOCRBackend(
        model="gemini-3.1-pro-preview",
        api_key="test-key",
        max_retries=1,
    )
    result = backend.recognize(Image.new("RGB", (240, 120), "white"))

    assert result.text == "recognized"
    assert sleep_calls == []
    assert sum("gemini-3.1-pro-preview" in url for url in urls) == 1
    assert any("gemini-3.6-flash" in url for url in urls)
    assert result.metadata["model"] == "gemini-3.6-flash"


def test_gemini_ocr_quota_error_does_not_retry_or_fallback(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError

    from PIL import Image

    import ocr_backend

    urls = []
    sleep_calls = []
    error_body = (
        b'{"error":{"status":"RESOURCE_EXHAUSTED",'
        b'"message":"Your prepayment credits are depleted."}}'
    )

    def fake_urlopen(req, timeout):
        urls.append(req.full_url)
        raise HTTPError(
            req.full_url,
            429,
            "quota exhausted",
            hdrs=None,
            fp=BytesIO(error_body),
        )

    monkeypatch.setattr(ocr_backend.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        ocr_backend.time,
        "sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    monkeypatch.delenv(ocr_backend.GEMINI_OCR_MODEL_ENV, raising=False)
    backend = ocr_backend.GeminiOCRBackend(api_key="test-key", max_retries=1)
    result = backend.recognize(Image.new("RGB", (240, 120), "white"))

    assert result.text == ""
    assert sleep_calls == []
    assert sum("gemini-3.5-flash" in url for url in urls) == 1
    assert not any("gemini-2.5-pro" in url for url in urls)
    assert "RESOURCE_EXHAUSTED" in result.metadata["error"]
    assert result.metadata["model_attempts"] == [
        {
            "model": "gemini-3.5-flash",
            "status": "error",
            "error": (
                'Gemini request failed with HTTP 429: {"error":{"status":"RESOURCE_EXHAUSTED",'
                '"message":"Your prepayment credits are depleted."}}'
            ),
        }
    ]


def test_gemini_ocr_model_can_be_overridden_with_flash(monkeypatch):
    import json
    from PIL import Image

    import ocr_backend

    urls: list[str] = []
    payloads: list[dict] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            response_text = json.dumps(
                {
                    "text": "빠른 인식",
                    "block_type": "stem",
                    "confidence": 0.91,
                },
                ensure_ascii=False,
            )
            return json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": response_text}],
                            }
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode("utf-8")

    def fake_urlopen(req, timeout):
        urls.append(req.full_url)
        payloads.append(json.loads(req.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setenv(ocr_backend.GEMINI_OCR_MODEL_ENV, "gemini-3.5-flash")
    monkeypatch.setattr(ocr_backend.urllib.request, "urlopen", fake_urlopen)

    backend = ocr_backend.GeminiOCRBackend(api_key="test-key", max_retries=0)
    result = backend.recognize(Image.new("RGB", (240, 120), "white"))

    assert backend.model == "gemini-3.5-flash"
    assert result.text == "빠른 인식"
    assert [line.text for line in result.lines] == ["빠른 인식"]
    assert result.metadata["model"] == "gemini-3.5-flash"
    assert result.metadata["thinking_level"] == "minimal"
    assert any("gemini-3.5-flash" in url for url in urls)
    assert not any("gemini-2.5-pro" in url for url in urls)
    assert payloads[0]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
    prompt = payloads[0]["contents"][0]["parts"][1]["text"]
    assert "Use visible Unicode/plain text only" in prompt
    assert "keep π as π and sin as sin" in prompt
    assert "lines" not in payloads[0]["generationConfig"]["responseSchema"]["properties"]


def test_default_gemini_ocr_model_is_flash_with_minimal_thinking(monkeypatch):
    import ocr_backend

    monkeypatch.delenv(ocr_backend.GEMINI_OCR_MODEL_ENV, raising=False)
    monkeypatch.delenv(ocr_backend.GEMINI_OCR_THINKING_LEVEL_ENV, raising=False)
    backend = ocr_backend.GeminiOCRBackend(api_key="test-key")

    assert backend.model == "gemini-3.5-flash"
    assert backend.thinking_level == "minimal"


def test_gemini_38_flash_ocr_model_contract(monkeypatch):
    import ocr_backend

    monkeypatch.setenv(ocr_backend.GEMINI_OCR_PROFILE_ENV, "latest")
    assert ocr_backend.resolve_gemini_ocr_model() == "gemini-3.8-flash"
    assert ocr_backend.resolve_gemini_ocr_thinking_level("gemini-3.8-flash") == "low"

    backend = ocr_backend.GeminiOCRBackend(model="gemini-3.8-flash", api_key="test-key")
    assert backend.model == "gemini-3.8-flash"
    assert backend.thinking_level == "low"


def test_gemini_ocr_flash_fallback_uses_current_flash_contract(monkeypatch):
    import json
    from io import BytesIO
    from urllib.error import HTTPError

    from PIL import Image

    import ocr_backend

    urls: list[str] = []
    payloads: list[dict] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            response_text = json.dumps(
                {
                    "text": "fallback ok",
                    "block_type": "stem",
                    "confidence": 0.88,
                    "lines": ["fallback ok"],
                }
            )
            return json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": response_text}],
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(req, timeout):
        urls.append(req.full_url)
        payloads.append(json.loads(req.data.decode("utf-8")))
        if "gemini-3.5-flash" in req.full_url:
            raise HTTPError(req.full_url, 503, "temporary", hdrs=None, fp=BytesIO(b"temporary"))
        return FakeResponse()

    monkeypatch.delenv(ocr_backend.GEMINI_OCR_MODEL_ENV, raising=False)
    monkeypatch.delenv(ocr_backend.GEMINI_OCR_THINKING_LEVEL_ENV, raising=False)
    monkeypatch.setattr(ocr_backend.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ocr_backend.time, "sleep", lambda seconds: None)

    backend = ocr_backend.GeminiOCRBackend(api_key="test-key", max_retries=0)
    result = backend.recognize(Image.new("RGB", (240, 120), "white"))

    assert result.text == "fallback ok"
    assert result.metadata["model"] == "gemini-3.6-flash"
    assert result.metadata["thinking_level"] == "low"
    assert any("gemini-3.5-flash" in url for url in urls)
    assert any("gemini-3.6-flash" in url for url in urls)
    assert payloads[0]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
    assert payloads[1]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    assert "temperature" not in payloads[1]["generationConfig"]
    fallback_prompt = payloads[1]["contents"][0]["parts"][1]["text"]
    assert "Return exactly one schema JSON object" in fallback_prompt


def test_explicit_gemini_model_name_builds_matching_backend(monkeypatch):
    import ocr_backend

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    backend = ocr_backend.create_ocr_backend("gemini-3.5-flash")

    assert isinstance(backend, ocr_backend.GeminiOCRBackend)
    assert backend.model == "gemini-3.5-flash"
    assert backend.thinking_level == "minimal"


def test_page_model_records_recognition_timing_metadata_before_repair(
    monkeypatch,
    tmp_path,
):
    from PIL import Image

    import build_structured_page_json as pipeline
    from ocr_backend import OCRResult
    from page_repair import build_ai_fallback_config
    from preprocess import PreparedPage
    from structured_schema import BlockType, Box, ContentBlock, PageModel, Subject

    class EmptyBackend:
        engine_name = "none"

        def recognize(self, image):
            return OCRResult(text="", confidence=None, backend_name="none")

    class EmptyCache:
        root_dir = tmp_path / "cache"

        def load_ocr_result(self, image, *, backend_name, cache_identity=None):
            return None

        def save_ocr_result(self, image, result, *, backend_name, cache_identity=None):
            return self.root_dir / "ocr" / backend_name / "payload.json"

    source = tmp_path / "page.png"
    image = Image.new("RGB", (240, 160), "white")
    image.save(source)
    prepared = PreparedPage(
        page_id="image-page-001",
        source_path=str(source),
        page_number=1,
        image=image,
        original_size=image.size,
        metadata={},
    )
    ocr_block = ContentBlock(
        block_id="image-page-001-block-001",
        block_type=BlockType.STEM,
        bbox=Box.from_points(10, 10, 100, 60),
        reading_order=0,
    )
    skipped_block = ContentBlock(
        block_id="image-page-001-block-002",
        block_type=BlockType.IMAGE,
        bbox=Box.from_points(120, 10, 220, 80),
        reading_order=1,
    )
    segmented_page = PageModel(
        page_id=prepared.page_id,
        width_px=image.width,
        height_px=image.height,
        subject=Subject.UNKNOWN,
        source_path=str(source),
        blocks=[ocr_block, skipped_block],
        metadata={"segmenter": "document"},
    )
    captured = {}

    def capture_repair(prepared_page, page, *, ocr_mode, config, cache, request_semaphore=None):
        captured["metadata"] = dict(page.metadata)
        return page

    monkeypatch.setattr(pipeline, "create_ocr_backend", lambda _mode: EmptyBackend())
    monkeypatch.setattr(
        pipeline,
        "segment_page",
        lambda prepared_page, page_id, subject: segmented_page,
    )
    monkeypatch.setattr(pipeline, "repair_page_model", capture_repair)

    page = pipeline.build_page_model(
        prepared,
        subject=Subject.UNKNOWN,
        ocr_mode="none",
        ai_config=build_ai_fallback_config(mode="off"),
        cache=EmptyCache(),
    )

    metadata = captured["metadata"]
    timing = metadata["recognition_timing_ms"]
    assert timing["segmentation"] >= 0
    assert timing["block_ocr"] >= 0
    assert timing["total_before_repair"] >= timing["segmentation"]
    assert page.metadata["ocr_block_worker_count"] == 1
    assert metadata["ocr_eligible_block_count"] == 1
    assert metadata["ocr_skipped_block_count"] == 1
    assert metadata["ocr_cache_hit_count"] == 0
    assert metadata["ocr_cache_miss_count"] == 1
    assert metadata["ocr_escalated_block_count"] == 0
    assert metadata["ai_stages"]["ocr"]["label"] == "1단계 OCR 인식"
    assert metadata["ai_stages"]["ocr_escalation"]["label"] == "2단계 블록 보강"


def test_gemini_failure_backoff_suppresses_repeated_quota_requests(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError

    from PIL import Image

    import ocr_backend

    calls = []
    error_body = b'{"error":{"status":"RESOURCE_EXHAUSTED","message":"quota depleted"}}'

    def fake_urlopen(req, timeout):
        calls.append(req.full_url)
        raise HTTPError(req.full_url, 429, "quota", hdrs=None, fp=BytesIO(error_body))

    monkeypatch.setattr(ocr_backend.urllib.request, "urlopen", fake_urlopen)
    backend = ocr_backend.GeminiOCRBackend(api_key="backoff-test-key", max_retries=1)
    image = Image.new("RGB", (240, 120), "white")

    first = backend.recognize(image)
    second = backend.recognize(image)

    assert len(calls) == 1
    assert first.metadata["cacheable"] is False
    assert first.metadata["fallback_reason"] == "authentication_or_quota_unavailable"
    assert first.metadata["retry_after_ms"] > 0
    assert second.metadata["circuit_open"] is True
    assert second.metadata["cacheable"] is False
    assert "image-based for review" in second.metadata["fallback_message"]


@pytest.mark.parametrize("structured_payload", [[], {}, {"text": ""}])
def test_gemini_empty_structured_response_is_uncacheable_failure(monkeypatch, structured_payload):
    import json

    from PIL import Image

    import ocr_backend

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            response_text = json.dumps(structured_payload)
            return json.dumps(
                {
                    "candidates": [
                        {"content": {"parts": [{"text": response_text}]}}
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr(ocr_backend.urllib.request, "urlopen", lambda req, timeout: FakeResponse())
    backend = ocr_backend.GeminiOCRBackend(
        api_key="empty-response-test-key",
        max_retries=0,
        failure_backoff=False,
    )

    result = backend.recognize(Image.new("RGB", (240, 120), "white"))

    assert result.text == ""
    assert result.lines == []
    assert result.confidence is None
    assert result.metadata["fallback_reason"] == "empty_response"
    assert result.metadata["cacheable"] is False
    assert "no usable text" in result.metadata["error"]


def test_local_ocr_backend_exceptions_are_uncacheable(monkeypatch):
    from PIL import Image

    import ocr_backend

    class BrokenTesseract:
        class Output:
            DICT = object()

        @staticmethod
        def image_to_data(*args, **kwargs):
            raise RuntimeError("missing language pack")

    class BrokenPaddleEngine:
        def ocr(self, *args, **kwargs):
            raise RuntimeError("engine crashed")

    monkeypatch.setattr(ocr_backend, "pytesseract", BrokenTesseract())
    monkeypatch.setattr(ocr_backend, "PaddleOCR", lambda **kwargs: BrokenPaddleEngine())
    image = Image.new("RGB", (240, 120), "white")

    tesseract_result = ocr_backend.TesseractOCRBackend().recognize(image)
    paddle_result = ocr_backend.PaddleOCRBackend().recognize(image)

    assert tesseract_result.metadata["cacheable"] is False
    assert paddle_result.metadata["cacheable"] is False
    assert "missing language pack" in tesseract_result.metadata["error"]
    assert "engine crashed" in paddle_result.metadata["error"]


def test_gemini_failure_backoff_coalesces_same_incident_across_workers(monkeypatch):
    import ocr_backend

    clock = {"now": 100.0}
    monkeypatch.setattr(ocr_backend.time, "monotonic", lambda: clock["now"])
    monkeypatch.setenv("EDB_GEMINI_OCR_FATAL_BACKOFF_SECONDS", "120")
    ocr_backend.clear_gemini_failure_backoff()
    key = ("api-key-fingerprint", "gemini-model")

    delays = [
        ocr_backend._record_gemini_failure(
            key,
            "authentication_or_quota_unavailable",
            fatal=True,
        )
        for _ in range(9)
    ]

    assert delays == [120_000] * 9
    assert ocr_backend._GEMINI_FAILURES[key].failure_count == 1

    # A genuinely later failed probe still increases the cooldown.
    clock["now"] = 221.0
    assert ocr_backend._record_gemini_failure(
        key,
        "authentication_or_quota_unavailable",
        fatal=True,
    ) == 240_000
    assert ocr_backend._GEMINI_FAILURES[key].failure_count == 2


def test_tesseract_capability_probe_is_cached_by_environment_and_refreshable(monkeypatch):
    import ocr_backend

    class FakePytesseractModule:
        class pytesseract:
            tesseract_cmd = "tesseract"

        def __init__(self):
            self.calls = 0

        def get_tesseract_version(self):
            self.calls += 1
            return "5.0"

    fake = FakePytesseractModule()
    monkeypatch.setattr(ocr_backend, "pytesseract", fake)
    monkeypatch.setenv("PATH", "/first")
    ocr_backend.clear_ocr_capability_cache()

    assert ocr_backend._tesseract_binary_available()
    assert ocr_backend._tesseract_binary_available()
    assert fake.calls == 1

    monkeypatch.setenv("PATH", "/second")
    assert ocr_backend._tesseract_binary_available()
    assert fake.calls == 2
    assert ocr_backend._tesseract_binary_available(refresh=True)
    assert fake.calls == 3


def test_negative_tesseract_probe_expires_after_runtime_install(monkeypatch):
    import ocr_backend

    class FakePytesseractModule:
        class pytesseract:
            tesseract_cmd = "tesseract"

        def __init__(self):
            self.calls = 0
            self.available = False

        def get_tesseract_version(self):
            self.calls += 1
            if not self.available:
                raise RuntimeError("not installed")
            return "5.0"

    fake = FakePytesseractModule()
    clock = {"now": 100.0}
    monkeypatch.setattr(ocr_backend, "pytesseract", fake)
    monkeypatch.setattr(ocr_backend.time, "monotonic", lambda: clock["now"])
    monkeypatch.setenv("EDB_OCR_NEGATIVE_CAPABILITY_TTL_SECONDS", "30")
    ocr_backend.clear_ocr_capability_cache()

    assert not ocr_backend._tesseract_binary_available()
    fake.available = True
    clock["now"] = 110.0
    assert not ocr_backend._tesseract_binary_available()
    assert fake.calls == 1
    clock["now"] = 131.0
    assert ocr_backend._tesseract_binary_available()
    assert fake.calls == 2


def test_uncacheable_ocr_failure_is_not_persisted_and_exposes_fallback(monkeypatch, tmp_path):
    from PIL import Image

    import build_structured_page_json as pipeline
    from ocr_backend import OCRResult
    from page_repair import build_ai_fallback_config
    from preprocess import PreparedPage
    from structured_schema import BlockType, Box, ContentBlock, PageModel, Subject

    class FailureBackend:
        engine_name = "gemini"

        def recognize(self, image):
            return OCRResult(
                text="",
                confidence=None,
                backend_name=self.engine_name,
                metadata={
                    "cacheable": False,
                    "empty_text": True,
                    "fallback_reason": "network_or_timeout",
                    "fallback_message": "Gemini OCR is temporarily unavailable. The block remains image-based for review.",
                    "retry_after_ms": 8000,
                    "circuit_open": True,
                },
            )

    class RecordingCache:
        root_dir = tmp_path / "cache"

        def __init__(self):
            self.save_calls = []

        def load_ocr_result(self, image, *, backend_name, cache_identity=None):
            return None

        def save_ocr_result(self, image, result, *, backend_name, cache_identity=None):
            self.save_calls.append((backend_name, cache_identity))
            return self.root_dir / "ocr" / backend_name / "payload.json"

    source = tmp_path / "page.png"
    image = Image.new("RGB", (240, 160), "white")
    image.save(source)
    prepared = PreparedPage(
        page_id="failure-page",
        source_path=str(source),
        page_number=1,
        image=image,
        original_size=image.size,
        metadata={},
    )
    block = ContentBlock(
        block_id="failure-block",
        block_type=BlockType.STEM,
        bbox=Box.from_points(10, 10, 200, 100),
        reading_order=0,
    )
    segmented = PageModel(
        page_id=prepared.page_id,
        width_px=image.width,
        height_px=image.height,
        subject=Subject.UNKNOWN,
        source_path=str(source),
        blocks=[block],
        metadata={"segmenter": "document"},
    )
    cache = RecordingCache()

    monkeypatch.setattr(pipeline, "create_ocr_backend", lambda _mode: FailureBackend())
    monkeypatch.setattr(
        pipeline,
        "segment_page",
        lambda prepared_page, page_id, subject: segmented,
    )
    monkeypatch.setattr(
        pipeline,
        "repair_page_model",
        lambda prepared_page, page, **kwargs: page,
    )

    page = pipeline.build_page_model(
        prepared,
        subject=Subject.UNKNOWN,
        ocr_mode="gemini",
        ai_config=build_ai_fallback_config(mode="off"),
        cache=cache,
    )

    assert cache.save_calls == []
    metadata = page.blocks[0].metadata
    assert metadata["ocr_cache_write_skipped"] is True
    assert metadata["ocr_fallback_reason"] == "network_or_timeout"
    assert metadata["ocr_retry_after_ms"] == 8000
    assert metadata["ocr_circuit_open"] is True
