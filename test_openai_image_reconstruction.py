import base64
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

import app_server
import build_problem_board_edb
import image_reconstruction_backend as image_backend
from image_reconstruction_backend import (
    DEFAULT_GEMINI_IMAGE_MODEL,
    ImageReconstructionResult,
    analyze_reconstruction_content_preservation,
    build_content_safe_upscale,
    clean_problem_image_transparency,
    postprocess_reconstructed_problem_image,
)


class TestImageReconstructionMutation(unittest.TestCase):
    def setUp(self):
        self._prev_gemini_key = os.environ.get("GEMINI_API_KEY")
        os.environ["GEMINI_API_KEY"] = "test-gemini-key"

    def tearDown(self):
        self._restore_env("GEMINI_API_KEY", self._prev_gemini_key)

    def _restore_env(self, name: str, value: str | None) -> None:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def _build_session(self, root: Path) -> dict:
        crop_path = root / "problem.png"
        Image.new("RGB", (320, 180), "white").save(crop_path)
        return {
            "output_dir": str(root / "out"),
            "problems": [
                {
                    "id": "problem-1",
                    "title": "1. problem",
                    "imagePath": crop_path.resolve().as_uri(),
                    "boardRenderPath": crop_path.resolve().as_uri(),
                    "sourcePageId": "page-1",
                    "bbox": {"left": 0, "top": 0, "width": 320, "height": 180},
                    "riskFlags": [],
                    "step": "s3",
                    "processingStep": "s3",
                }
            ],
            "pages": [{"id": "page-1", "problemIds": ["problem-1"]}],
        }

    def _fake_reconstruct(self, source_path, output_path, **kwargs):
        Image.new("RGBA", (640, 360), (0, 0, 0, 0)).save(output_path)
        return ImageReconstructionResult(
            output_path=Path(output_path),
            provider=kwargs.get("provider") or "gemini",
            model=kwargs.get("model") or DEFAULT_GEMINI_IMAGE_MODEL,
            prompt=kwargs.get("prompt") or "",
            source_path=Path(source_path),
            latency_ms=12,
            postprocess={
                "status": "applied",
                "transparent_ratio": 1.0,
                "content_preservation": {
                    "status": "pass",
                    "review_required": False,
                    "severity": "none",
                    "reasons": [],
                },
            },
        )

    def test_explicit_ai_enhance_uses_gemini_and_requires_semantic_review(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = self._build_session(root)

            with patch.object(app_server, "reconstruct_problem_image", side_effect=self._fake_reconstruct) as mock_reconstruct:
                updated = app_server._mutate_enhance_image(
                    session,
                    {"problemIds": ["problem-1"], "mode": "ai"},
                )

            problem = updated["problems"][0]
            self.assertNotEqual(problem["imagePath"], problem["originalImagePath"])
            self.assertEqual(problem["boardRenderPath"], problem["imagePath"])
            self.assertEqual(problem["processingStep"], "s3")
            self.assertEqual(problem["reviewStatus"], "check_needed")
            self.assertIn("ai_image_reconstructed_check_text", problem["riskFlags"])
            self.assertEqual(problem["aiImageReconstruction"]["deliveryMode"], "ai_primary")
            self.assertFalse(problem["aiImageReconstruction"]["autoRecovered"])
            self.assertEqual(problem["aiImageReconstruction"]["provider"], "gemini")
            self.assertEqual(problem["aiImageReconstruction"]["model"], DEFAULT_GEMINI_IMAGE_MODEL)
            self.assertTrue(problem["aiImageReconstruction"]["transparent_background"])
            self.assertTrue(Path(problem["aiImageReconstruction"]["output_path"]).exists())

            called_kwargs = mock_reconstruct.call_args.kwargs
            self.assertEqual(called_kwargs["provider"], "gemini")
            self.assertEqual(called_kwargs["model"], DEFAULT_GEMINI_IMAGE_MODEL)
            self.assertEqual(called_kwargs["size"], "1k")
            self.assertTrue(called_kwargs["transparent_background"])
            self.assertTrue(called_kwargs["sharpen"])

            summary = updated.get("ai_image_reconstruction_summary") or []
            self.assertEqual(summary[0]["status"], "applied")
            self.assertEqual(summary[0]["provider"], "gemini")
            self.assertEqual(summary[0]["problemId"], "problem-1")

    def test_enhance_image_caps_explicit_2k_request_at_1k(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = self._build_session(root)

            with patch.object(app_server, "reconstruct_problem_image", side_effect=self._fake_reconstruct) as reconstruct:
                updated = app_server._mutate_enhance_image(
                    session,
                    {"problemIds": ["problem-1"], "size": "2k", "mode": "ai"},
                )

            self.assertEqual("1k", reconstruct.call_args.kwargs["size"])
            metadata = updated["problems"][0]["aiImageReconstruction"]
            self.assertEqual("2k", metadata["requestedSize"])
            self.assertEqual("1k", metadata["size"])
            self.assertTrue(metadata["highResolutionSkipped"])

    def test_gemini_nano_banana_2k_request_sets_native_image_size(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_path = root / "source.png"
            output_path = root / "output.png"
            Image.new("RGB", (48, 72), "white").save(source_path)
            generated = io.BytesIO()
            Image.new("RGBA", (96, 144), (255, 255, 255, 255)).save(generated, format="PNG")
            response_payload = {
                "candidates": [{
                    "content": {
                        "parts": [{
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": base64.b64encode(generated.getvalue()).decode("ascii"),
                            }
                        }]
                    }
                }]
            }
            captured = {}

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self):
                    return json.dumps(response_payload).encode("utf-8")

            def fake_urlopen(req, **_kwargs):
                captured["url"] = req.full_url
                captured["payload"] = json.loads(req.data.decode("utf-8"))
                return FakeResponse()

            with patch.object(image_backend.request, "urlopen", side_effect=fake_urlopen):
                result = image_backend.reconstruct_problem_image(
                    source_path,
                    output_path,
                    api_key="test-key",
                    provider="nano-banana-2",
                    model="nano-banana-2",
                    size="2k",
                    transparent_background=False,
                    sharpen=False,
                )

            self.assertEqual("gemini-3.1-flash-image", result.model)
            self.assertIn("/v1beta/models/gemini-3.1-flash-image:generateContent", captured["url"])
            self.assertEqual(
                "IMAGE_SIZE_TWO_K",
                captured["payload"]["generationConfig"]["responseFormat"]["image"]["imageSize"],
            )
            self.assertEqual("2K", result.postprocess["requested_image_size"])

    def test_enhance_image_retries_content_loss_and_silently_recovers(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = self._build_session(root)
            calls = 0

            def reconstruct_then_recover(source_path, output_path, **kwargs):
                nonlocal calls
                calls += 1
                result = self._fake_reconstruct(source_path, output_path, **kwargs)
                if calls == 1:
                    result.postprocess["content_preservation"] = {
                        "status": "review_required",
                        "review_required": True,
                        "severity": "medium",
                        "reasons": ["formula_row_loss"],
                    }
                return result

            with patch.object(app_server, "reconstruct_problem_image", side_effect=reconstruct_then_recover) as mock_reconstruct:
                updated = app_server._mutate_enhance_image(
                    session,
                    {"problemIds": ["problem-1"], "mode": "ai"},
                )

            problem = updated["problems"][0]
            self.assertEqual(2, mock_reconstruct.call_count)
            self.assertEqual("check_needed", problem["reviewStatus"])
            self.assertEqual(["ai_image_reconstructed_check_text"], problem["riskFlags"])
            self.assertEqual("ai_content_retry", problem["aiImageReconstruction"]["deliveryMode"])
            self.assertTrue(problem["aiImageReconstruction"]["autoRecovered"])
            self.assertEqual(2, len(problem["aiImageReconstruction"]["attempts"]))

    def test_missing_gemini_key_rejects_before_mutation(self):
        os.environ.pop("GEMINI_API_KEY", None)
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = self._build_session(root)
            with self.assertRaises(ValueError) as ctx:
                app_server._mutate_enhance_image(
                    session,
                    {"problemIds": ["problem-1"], "mode": "ai"},
                )
            self.assertIn("Gemini API", str(ctx.exception))
            self.assertNotIn("ai_image_reconstruction_summary", session)
            self.assertNotIn("aiImageReconstruction", session["problems"][0])

    def test_korean_auto_mode_uses_fast_content_safe_path_without_api_key(self):
        os.environ.pop("GEMINI_API_KEY", None)
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = self._build_session(root)
            session["problems"][0]["subject"] = "korean"

            with patch.object(app_server, "reconstruct_problem_image") as reconstruct:
                updated = app_server._mutate_enhance_image(session, {"problemIds": ["problem-1"]})

            reconstruct.assert_not_called()
            problem = updated["problems"][0]
            self.assertEqual("local", problem["aiImageReconstruction"]["provider"])
            self.assertEqual("preserve", problem["aiImageReconstruction"]["resolvedMode"])
            self.assertEqual("content_safe_primary", problem["aiImageReconstruction"]["deliveryMode"])
            self.assertEqual("content_safe_no_generation", problem["aiImageReconstruction"]["promptProfile"])
            self.assertEqual("normal", problem["reviewStatus"])

    def test_auto_mode_infers_korean_from_input_filename_when_subject_is_unknown(self):
        os.environ.pop("GEMINI_API_KEY", None)
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = self._build_session(root)
            session["problems"][0]["subject"] = "unknown"
            session["input_files"] = [str(root / "2026년_고2_국어_문제.pdf")]

            updated = app_server._mutate_enhance_image(session, {"problemIds": ["problem-1"]})

            metadata = updated["problems"][0]["aiImageReconstruction"]
            self.assertEqual("korean", metadata["subject"])
            self.assertEqual("preserve", metadata["resolvedMode"])

    def test_explicit_ai_for_korean_requires_semantic_text_review(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = self._build_session(root)
            session["problems"][0]["subject"] = "korean"

            with patch.object(app_server, "reconstruct_problem_image", side_effect=self._fake_reconstruct) as reconstruct:
                updated = app_server._mutate_enhance_image(
                    session,
                    {"problemIds": ["problem-1"], "mode": "ai"},
                )

            problem = updated["problems"][0]
            self.assertEqual("check_needed", problem["reviewStatus"])
            self.assertIn("ai_image_reconstructed_check_text", problem["riskFlags"])
            semantic_gate = problem["aiImageReconstruction"]["semanticTextPreservation"]
            self.assertEqual("unverified", semantic_gate["status"])
            self.assertTrue(semantic_gate["review_required"])
            self.assertEqual("text_exact_copy", problem["aiImageReconstruction"]["promptProfile"])
            sent_prompt = reconstruct.call_args.kwargs["prompt"]
            self.assertIn("document restoration, not OCR retyping", sent_prompt)
            self.assertIn("Never invent a cleaner replacement", sent_prompt)

    def test_custom_ai_prompt_overrides_text_exact_copy_profile(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = self._build_session(root)
            session["problems"][0]["subject"] = "english"

            with patch.object(app_server, "reconstruct_problem_image", side_effect=self._fake_reconstruct) as reconstruct:
                updated = app_server._mutate_enhance_image(
                    session,
                    {
                        "problemIds": ["problem-1"],
                        "mode": "ai",
                        "prompt": "custom exact restoration prompt",
                    },
                )

            self.assertEqual("custom exact restoration prompt", reconstruct.call_args.kwargs["prompt"])
            self.assertEqual(
                "custom",
                updated["problems"][0]["aiImageReconstruction"]["promptProfile"],
            )

    def test_page_as_is_old_session_recovers_normalized_page_source(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = self._build_session(root)
            normalized_page = root / "normalized-page.png"
            enhanced_page = root / "already-enhanced.png"
            Image.new("RGB", (800, 1200), "white").save(normalized_page)
            Image.new("RGB", (1600, 2400), "white").save(enhanced_page)
            problem = session["problems"][0]
            problem["imagePath"] = enhanced_page.resolve().as_uri()
            problem["inputIntent"] = "page-as-is"
            session["pages"] = [{
                "id": "page-1",
                "problemIds": ["problem-1"],
                "sourceImagePath": str(normalized_page.resolve()),
            }]

            recovered = app_server._original_problem_image_path(problem, session)

            self.assertEqual(normalized_page.resolve(), recovered)

    def test_enhance_restarts_from_original_image_instead_of_upscaling_prior_result(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = self._build_session(root)
            original_path = root / "original.png"
            Image.new("RGB", (200, 100), "white").save(original_path)
            session["problems"][0]["originalImagePath"] = original_path.resolve().as_uri()

            with patch.object(app_server, "reconstruct_problem_image", side_effect=self._fake_reconstruct) as reconstruct:
                app_server._mutate_enhance_image(
                    session,
                    {"problemIds": ["problem-1"], "mode": "ai"},
                )

            self.assertEqual(original_path.resolve(), Path(reconstruct.call_args.args[0]).resolve())

    def test_non_gemini_provider_is_rejected(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = self._build_session(root)

            with self.assertRaises(ValueError) as ctx:
                app_server._mutate_enhance_image(
                    session,
                    {"problemIds": ["problem-1"], "provider": "openai", "mode": "ai"},
                )
            self.assertIn("unsupported image reconstruction provider", str(ctx.exception))

    def test_enhance_image_adds_specific_formula_loss_review_flag(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = self._build_session(root)

            def reconstruct_with_loss(source_path, output_path, **kwargs):
                Image.new("RGBA", (640, 360), (0, 0, 0, 0)).save(output_path)
                return ImageReconstructionResult(
                    output_path=Path(output_path),
                    provider="gemini",
                    model=DEFAULT_GEMINI_IMAGE_MODEL,
                    prompt=kwargs.get("prompt") or "",
                    source_path=Path(source_path),
                    latency_ms=12,
                    postprocess={
                        "status": "applied",
                        "content_preservation": {
                            "status": "review_required",
                            "review_required": True,
                            "severity": "high",
                            "reasons": ["formula_row_loss", "localized_ink_loss"],
                        },
                    },
                )

            with (
                patch.object(app_server, "reconstruct_problem_image", side_effect=reconstruct_with_loss),
                patch.object(app_server, "build_content_safe_upscale", side_effect=reconstruct_with_loss),
            ):
                updated = app_server._mutate_enhance_image(
                    session,
                    {"problemIds": ["problem-1"], "mode": "ai"},
                )

            problem = updated["problems"][0]
            self.assertIn("ai_image_formula_loss_suspected", problem["riskFlags"])
            self.assertEqual(
                "review_required",
                problem["aiImageReconstruction"]["contentPreservation"]["status"],
            )
            self.assertEqual(
                "high",
                updated["ai_image_reconstruction_summary"][0]["contentPreservation"]["severity"],
            )
            self.assertEqual("content_safe_fallback", problem["aiImageReconstruction"]["deliveryMode"])

    def test_enhance_image_uses_content_safe_fallback_after_two_rejected_generations(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = self._build_session(root)

            def rejected_reconstruct(source_path, output_path, **kwargs):
                result = self._fake_reconstruct(source_path, output_path, **kwargs)
                result.postprocess["content_preservation"] = {
                    "status": "review_required",
                    "review_required": True,
                    "severity": "medium",
                    "reasons": ["localized_ink_loss"],
                }
                return result

            with patch.object(app_server, "reconstruct_problem_image", side_effect=rejected_reconstruct) as reconstruct:
                updated = app_server._mutate_enhance_image(
                    session,
                    {"problemIds": ["problem-1"], "mode": "ai"},
                )

            problem = updated["problems"][0]
            self.assertEqual(2, reconstruct.call_count)
            self.assertEqual("normal", problem["reviewStatus"])
            self.assertEqual([], problem["riskFlags"])
            self.assertEqual("content_safe_fallback", problem["aiImageReconstruction"]["deliveryMode"])
            self.assertEqual("local", problem["aiImageReconstruction"]["provider"])
            self.assertTrue(problem["aiImageReconstruction"]["autoRecovered"])

    def test_enhance_image_hides_provider_failure_with_content_safe_fallback(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = self._build_session(root)

            with patch.object(app_server, "reconstruct_problem_image", side_effect=RuntimeError("provider timeout")):
                updated = app_server._mutate_enhance_image(
                    session,
                    {"problemIds": ["problem-1"], "mode": "ai"},
                )

            problem = updated["problems"][0]
            self.assertEqual("normal", problem["reviewStatus"])
            self.assertEqual([], problem["riskFlags"])
            self.assertEqual("content_safe_fallback", problem["aiImageReconstruction"]["deliveryMode"])
            self.assertEqual("local", problem["aiImageReconstruction"]["provider"])
            self.assertEqual("failed", problem["aiImageReconstruction"]["attempts"][0]["status"])

    def test_content_safe_fallback_preserves_formula_ink(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_path = root / "source.png"
            output_path = root / "safe.png"
            source = Image.new("RGB", (240, 120), "white")
            pixels = source.load()
            for y in (35, 70, 95):
                for x in range(25, 215):
                    pixels[x, y] = (0, 0, 0)
            source.save(source_path)

            result = build_content_safe_upscale(source_path, output_path)

            self.assertEqual("local", result.provider)
            self.assertEqual("content-safe-lanczos", result.model)
            with Image.open(output_path) as output:
                self.assertEqual((480, 240), output.size)
            self.assertEqual("pass", result.postprocess["content_preservation"]["status"])

    def test_content_preservation_passes_scaled_equivalent_formula(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_path = root / "source.png"
            output_path = root / "output.png"
            source = Image.new("RGB", (320, 180), "white")
            pixels = source.load()
            for x in range(35, 285):
                pixels[x, 55] = (0, 0, 0)
                pixels[x, 120] = (0, 0, 0)
            for y in range(38, 140):
                pixels[90, y] = (0, 0, 0)
                pixels[230, y] = (0, 0, 0)
            source.save(source_path)
            source.resize((640, 360), Image.Resampling.NEAREST).save(output_path)

            stats = analyze_reconstruction_content_preservation(source_path, output_path)

            self.assertEqual("pass", stats["status"])
            self.assertFalse(stats["review_required"])

    def test_content_preservation_catches_missing_formula_row(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_path = root / "source.png"
            output_path = root / "output.png"
            source = Image.new("RGB", (320, 180), "white")
            pixels = source.load()
            for row_y in (35, 85, 135):
                for x in range(30, 290):
                    pixels[x, row_y] = (0, 0, 0)
                    if x % 20 < 9:
                        pixels[x, row_y + 1] = (0, 0, 0)
            source.save(source_path)
            output = source.copy()
            output_pixels = output.load()
            for y in range(128, 143):
                for x in range(20, 300):
                    output_pixels[x, y] = (255, 255, 255)
            output.resize((640, 360), Image.Resampling.NEAREST).save(output_path)

            stats = analyze_reconstruction_content_preservation(source_path, output_path)

            self.assertEqual("review_required", stats["status"])
            self.assertTrue(stats["review_required"])
            self.assertTrue(
                {"formula_row_loss", "localized_ink_loss", "source_ink_missing"}.intersection(stats["reasons"])
            )

    def test_content_preservation_tolerates_small_layout_shift(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_path = root / "source.png"
            output_path = root / "output.png"
            source = Image.new("RGB", (320, 180), "white")
            pixels = source.load()
            for row_y in (45, 90, 135):
                for x in range(35, 285):
                    pixels[x, row_y] = (0, 0, 0)
            source.save(source_path)
            scaled = source.resize((640, 360), Image.Resampling.NEAREST)
            shifted = Image.new("RGB", scaled.size, "white")
            shifted.paste(scaled.crop((0, 0, 632, 354)), (8, 6))
            shifted.save(output_path)

            stats = analyze_reconstruction_content_preservation(source_path, output_path)

            self.assertEqual("pass", stats["status"])
            self.assertFalse(stats["review_required"])

    def test_content_preservation_catches_changed_glyph_structure(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_path = root / "source.png"
            output_path = root / "output.png"
            source = Image.new("RGB", (320, 180), "white")
            output = Image.new("RGB", source.size, "white")
            source_pixels = source.load()
            output_pixels = output.load()
            for center_y in (40, 90, 140):
                for center_x in range(40, 300, 40):
                    for offset in range(-8, 9):
                        source_pixels[center_x, center_y + offset] = (0, 0, 0)
                        source_pixels[center_x + offset, center_y] = (0, 0, 0)
                        output_pixels[center_x + offset, center_y + offset] = (0, 0, 0)
                        output_pixels[center_x + offset, center_y - offset] = (0, 0, 0)
            source.save(source_path)
            output.resize((640, 360), Image.Resampling.NEAREST).save(output_path)

            stats = analyze_reconstruction_content_preservation(source_path, output_path)

            self.assertEqual("review_required", stats["status"])
            self.assertIn("glyph_structure_changed", stats["reasons"])

    def test_content_preservation_catches_blank_reconstruction(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_path = root / "source.png"
            output_path = root / "output.png"
            source = Image.new("RGB", (200, 100), "white")
            pixels = source.load()
            for x in range(20, 180):
                pixels[x, 50] = (0, 0, 0)
            source.save(source_path)
            Image.new("RGBA", (400, 200), (0, 0, 0, 0)).save(output_path)

            stats = analyze_reconstruction_content_preservation(source_path, output_path)

            self.assertEqual("review_required", stats["status"])
            self.assertEqual("high", stats["severity"])
            self.assertIn("output_nearly_empty", stats["reasons"])

    def test_postprocess_removes_white_background_without_erasing_dark_text(self):
        with TemporaryDirectory() as raw_tmp:
            path = Path(raw_tmp) / "reconstructed.png"
            image = Image.new("RGBA", (3, 1), (255, 255, 255, 255))
            image.putdata([
                (255, 255, 255, 255),
                (238, 238, 238, 255),
                (0, 0, 0, 255),
            ])
            image.save(path)

            stats = postprocess_reconstructed_problem_image(path, sharpen=False)
            output = Image.open(path).convert("RGBA")

            self.assertEqual(stats["status"], "applied")
            self.assertEqual(output.getpixel((0, 0))[3], 0)
            self.assertLess(output.getpixel((1, 0))[3], 255)
            self.assertEqual(output.getpixel((2, 0))[3], 255)

    def test_postprocess_upscales_undersized_reconstruction_and_boosts_ink_alpha(self):
        with TemporaryDirectory() as raw_tmp:
            path = Path(raw_tmp) / "undersized_reconstructed.png"
            image = Image.new("RGBA", (40, 20), (18, 22, 26, 255))
            pixels = image.load()
            for x in range(8, 32):
                pixels[x, 10] = (210, 214, 214, 128)
            image.save(path)

            stats = postprocess_reconstructed_problem_image(
                path,
                source_size=(60, 30),
                upscale_factor=2.0,
            )
            output = Image.open(path).convert("RGBA")

            self.assertEqual(output.size, (120, 60))
            self.assertTrue(stats["upscaled"])
            self.assertGreaterEqual(stats["upscale_scale"], 3.0)
            self.assertGreater(output.getpixel((60, 30))[3], 180)

    def test_postprocess_removes_dark_model_background_without_erasing_chalk(self):
        with TemporaryDirectory() as raw_tmp:
            path = Path(raw_tmp) / "dark_reconstructed.png"
            image = Image.new("RGBA", (80, 30), (18, 22, 26, 255))
            pixels = image.load()
            pixels[10, 10] = (35, 39, 43, 255)
            pixels[30, 10] = (180, 184, 188, 255)
            pixels[50, 10] = (248, 248, 246, 255)
            image.save(path)

            stats = postprocess_reconstructed_problem_image(path, sharpen=False)
            output = Image.open(path).convert("RGBA")

            self.assertEqual(stats["background_kind"], "dark")
            self.assertEqual(output.getpixel((0, 0))[3], 0)
            self.assertLess(output.getpixel((10, 10))[3], 255)
            self.assertGreater(output.getpixel((30, 10))[3], 200)
            self.assertEqual(output.getpixel((50, 10))[3], 255)

    def test_clean_transparency_uses_numpy_alpha_backend_for_flat_light_pages(self):
        image = Image.new("RGBA", (160, 120), (255, 255, 255, 255))
        pixels = image.load()
        pixels[10, 10] = (246, 246, 246, 255)
        pixels[42, 30] = (36, 36, 36, 255)
        pixels[80, 60] = (245, 245, 245, 255)

        cleaned, stats = clean_problem_image_transparency(
            image,
            remove_corner_page_artifacts=False,
        )

        self.assertEqual("numpy", stats["alpha_backend"])
        self.assertEqual(stats["background_kind"], "light")
        self.assertEqual(cleaned.getpixel((0, 0))[3], 0)
        self.assertEqual(cleaned.getpixel((10, 10))[3], 0)
        self.assertEqual(cleaned.getpixel((42, 30))[3], 255)
        self.assertLess(cleaned.getpixel((80, 60))[3], 255)

    def test_clean_transparency_removes_lower_right_pdf_page_badge(self):
        image = Image.new("RGBA", (120, 90), (18, 22, 26, 255))
        pixels = image.load()
        # Real problem content near the bottom but away from the outer edge.
        for x in range(8, 48):
            pixels[x, 74] = (248, 248, 246, 255)
        # Page-number badge glued to the lower-right crop edge.
        for x in range(102, 120):
            pixels[x, 74] = (248, 248, 246, 255)
            pixels[x, 89] = (248, 248, 246, 255)
        for y in range(74, 90):
            pixels[102, y] = (248, 248, 246, 255)
            pixels[119, y] = (248, 248, 246, 255)
        for x in range(108, 113):
            for y in range(78, 86):
                pixels[x, y] = (248, 248, 246, 255)

        cleaned, stats = clean_problem_image_transparency(image)

        self.assertEqual(stats["removed_corner_artifacts"], 1)
        self.assertGreater(cleaned.getpixel((16, 74))[3], 200)
        self.assertEqual(cleaned.getpixel((112, 82))[3], 0)
        self.assertEqual(cleaned.getpixel((119, 89))[3], 0)

    def test_clean_transparency_removes_upper_right_artifact_but_keeps_problem_number(self):
        image = Image.new("RGBA", (140, 100), (18, 22, 26, 255))
        pixels = image.load()
        # Problem number/content near the upper-left must never be treated as a page marker.
        for x in range(6, 18):
            pixels[x, 8] = (248, 248, 246, 255)
        for y in range(8, 22):
            pixels[6, y] = (248, 248, 246, 255)
        # Small header/page marker glued to the upper-right page edge.
        for x in range(128, 140):
            pixels[x, 0] = (248, 248, 246, 255)
            pixels[x, 9] = (248, 248, 246, 255)
        for y in range(0, 10):
            pixels[128, y] = (248, 248, 246, 255)
            pixels[139, y] = (248, 248, 246, 255)

        cleaned, stats = clean_problem_image_transparency(image)

        self.assertEqual(stats["removed_corner_artifacts"], 1)
        self.assertGreater(cleaned.getpixel((6, 12))[3], 200)
        self.assertEqual(cleaned.getpixel((134, 4))[3], 0)
        self.assertEqual(cleaned.getpixel((139, 0))[3], 0)

    def test_publish_cutout_removes_dark_background_and_corner_page_badge(self):
        image = Image.new("RGBA", (120, 90), (18, 22, 26, 255))
        pixels = image.load()
        for x in range(12, 70):
            pixels[x, 22] = (248, 248, 246, 255)
        for x in range(102, 120):
            pixels[x, 74] = (248, 248, 246, 255)
            pixels[x, 89] = (248, 248, 246, 255)
        for y in range(74, 90):
            pixels[102, y] = (248, 248, 246, 255)
            pixels[119, y] = (248, 248, 246, 255)

        cutout = build_problem_board_edb._extract_problem_cutout(image)

        self.assertEqual(cutout.mode, "RGBA")
        self.assertEqual(cutout.getpixel((0, 0))[3], 0)
        self.assertGreater(cutout.getpixel((20, 22))[3], 200)
        self.assertEqual(cutout.getpixel((112, 82))[3], 0)

    def test_publish_loader_preserves_transparent_cutout_instead_of_compositing_board(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            rendered_path = root / "rendered.png"
            rendered = Image.new("RGBA", (12, 8), (248, 248, 246, 0))
            rendered.putpixel((4, 3), (248, 248, 246, 255))
            rendered.save(rendered_path)
            crop = Image.new("RGB", (12, 8), "white")

            loaded = build_problem_board_edb._load_board_export_image(rendered_path, crop)

            self.assertEqual(loaded.mode, "RGBA")
            self.assertEqual(loaded.getpixel((0, 0))[3], 0)
            self.assertEqual(loaded.getpixel((4, 3))[3], 255)

    def test_stage_three_text_priority_skips_neural_upscaler(self):
        source = Image.new("RGB", (320, 180), "white")
        pixels = source.load()
        for x in range(20, 300):
            pixels[x, 80] = (0, 0, 0)

        with patch("upscayl_backend.auto_upscale_image") as neural_upscale:
            output = build_problem_board_edb._build_transparent_reconstruction_image(
                source,
                text_priority=True,
            )

        neural_upscale.assert_not_called()
        self.assertEqual("RGBA", output.mode)
        self.assertGreaterEqual(output.width, 1024)


if __name__ == "__main__":
    unittest.main()
