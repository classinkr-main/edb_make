import json
import unittest
from unittest.mock import patch
import base64
from io import BytesIO

from PIL import Image

import page_repair
from page_repair import build_ai_fallback_config, repair_page_model
from preprocess import PreparedPage
from structured_schema import BlockType, Box, ContentBlock, PageModel, ProblemUnit, Subject


class _EmptyCache:
    """PipelineCache stand-in that never serves a cached repair, so the test
    exercises repair_page_model's fresh-call branch."""

    def load_ai_repair(self, **_kwargs):
        return None

    def save_ai_repair(self, **_kwargs):
        return None


class TestPageRepairConfig(unittest.TestCase):
    def _two_numbered_blocks_page(self) -> tuple[PreparedPage, PageModel]:
        """Two separately numbered stem blocks. The local baseline groups
        these into two problems titled "문제" each, with both blocks typed
        TITLE -- the starting point every change-signal test below diffs
        against."""
        prepared_page = PreparedPage(
            page_id="page-1",
            source_path="sample.png",
            page_number=1,
            image=Image.new("RGB", (100, 120), "white"),
            original_size=(100, 120),
        )
        page = PageModel(
            page_id="page-1",
            width_px=100,
            height_px=120,
            subject=Subject.SCIENCE,
            blocks=[
                ContentBlock(
                    block_id=f"block-{index}",
                    block_type=BlockType.STEM,
                    bbox=Box(left=0, top=index * 10, width=80, height=8),
                    reading_order=index,
                    text=f"{index + 1}. 문제",
                )
                for index in range(2)
            ],
        )
        return prepared_page, page

    def test_max_tokens_propagates_to_gemini_payload(self):
        captured = {}

        def fake_post_json(url, payload, *, headers, timeout_ms):
            captured["url"] = url
            captured["payload"] = payload
            captured["headers"] = headers
            captured["timeout_ms"] = timeout_ms
            return {
                "responseId": "response-1",
                "usageMetadata": {
                    "promptTokenCount": 210,
                    "candidatesTokenCount": 35,
                    "thoughtsTokenCount": 5,
                    "totalTokenCount": 250,
                },
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "problem_start_block_ids": ["block-1"],
                                            "choice_block_ids": [],
                                            "figure_block_ids": [],
                                            "display_titles": [],
                                            "notes": [],
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ],
            }

        prepared_page = PreparedPage(
            page_id="page-1",
            source_path="sample.png",
            page_number=1,
            image=Image.new("RGB", (100, 120), "white"),
            original_size=(100, 120),
        )
        page = PageModel(
            page_id="page-1",
            width_px=100,
            height_px=120,
            subject=Subject.SCIENCE,
            blocks=[
                ContentBlock(
                    block_id="block-1",
                    block_type=BlockType.STEM,
                    bbox=Box(left=0, top=0, width=80, height=40),
                    reading_order=0,
                    text="1. 문제",
                )
            ],
        )
        config = build_ai_fallback_config(mode="force", max_tokens=6789, timeout_ms=12345)

        with patch.object(page_repair, "_image_to_base64", return_value="encoded-image"):
            with patch.object(page_repair, "_post_json", side_effect=fake_post_json):
                payload, response_id, token_usage = page_repair._request_gemini_repair(
                    prepared_page=prepared_page,
                    page=page,
                    config=config,
                    trigger_reasons=["forced"],
                    api_key="test-key",
                )

        self.assertEqual(response_id, "response-1")
        self.assertEqual(payload["problem_start_block_ids"], ["block-1"])
        self.assertEqual(250, token_usage["total_token_count"])
        self.assertEqual(1, token_usage["request_count"])
        self.assertEqual(captured["timeout_ms"], 12345)
        generation_config = captured["payload"]["generationConfig"]
        self.assertEqual(generation_config["maxOutputTokens"], 536)
        self.assertEqual(
            generation_config["thinkingConfig"],
            {"thinkingLevel": "low"},
        )
        self.assertNotIn(
            "problem_units",
            generation_config["responseSchema"]["properties"],
        )
        self.assertNotIn(
            "notes",
            generation_config["responseSchema"]["properties"],
        )
        self.assertEqual(token_usage["configured_max_output_tokens"], 6789)
        self.assertEqual(token_usage["effective_max_output_tokens"], 536)

    def test_repair_context_image_caps_long_edge(self):
        encoded = page_repair._image_to_base64(
            Image.new("RGB", (4200, 2800), "white")
        )

        with Image.open(BytesIO(base64.b64decode(encoded))) as decoded:
            self.assertEqual(
                max(decoded.size),
                page_repair.AI_REPAIR_IMAGE_MAX_DIMENSION,
            )

    def test_31_pro_falls_back_to_stable_pro_on_call_error(self):
        urls = []

        def fake_post_json(url, payload, *, headers, timeout_ms):
            urls.append(url)
            if "gemini-3.1-pro-preview" in url:
                raise RuntimeError("Gemini request failed with HTTP 404: model not found")
            return {
                "responseId": "fallback-response",
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "problem_start_block_ids": ["block-1"],
                                            "choice_block_ids": [],
                                            "figure_block_ids": [],
                                            "display_titles": [],
                                            "notes": [],
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ],
            }

        prepared_page = PreparedPage(
            page_id="page-1",
            source_path="sample.png",
            page_number=1,
            image=Image.new("RGB", (100, 120), "white"),
            original_size=(100, 120),
        )
        page = PageModel(
            page_id="page-1",
            width_px=100,
            height_px=120,
            subject=Subject.SCIENCE,
            blocks=[
                ContentBlock(
                    block_id="block-1",
                    block_type=BlockType.STEM,
                    bbox=Box(left=0, top=0, width=80, height=40),
                    reading_order=0,
                    text="1. 문제",
                )
            ],
        )
        config = build_ai_fallback_config(mode="force", timeout_ms=1000)
        sleep_calls = []

        with patch.object(page_repair, "_image_to_base64", return_value="encoded-image"):
            with patch.object(page_repair, "_post_json", side_effect=fake_post_json):
                with patch.object(
                    page_repair.time,
                    "sleep",
                    side_effect=lambda seconds: sleep_calls.append(seconds),
                ):
                    (
                        payload,
                        response_id,
                        used_model,
                        attempts,
                        token_usage,
                    ) = page_repair._request_ai_repair_with_model_fallback(
                        prepared_page=prepared_page,
                        page=page,
                        config=config,
                        trigger_reasons=["forced"],
                        api_key="test-key",
                    )

        self.assertEqual(response_id, "fallback-response")
        self.assertEqual(payload["problem_start_block_ids"], ["block-1"])
        self.assertEqual(used_model, "gemini-3.6-flash")
        self.assertEqual(4096, token_usage["configured_max_output_tokens"])
        self.assertEqual(536, token_usage["effective_max_output_tokens"])
        self.assertEqual(0, token_usage["problem_units_requested"])
        self.assertEqual(["error", "ok"], [attempt["status"] for attempt in attempts])
        self.assertEqual([], sleep_calls)
        self.assertEqual(1, sum("gemini-3.1-pro-preview" in url for url in urls))
        self.assertTrue(any("gemini-3.1-pro-preview" in url for url in urls))
        self.assertTrue(any("gemini-3.6-flash" in url for url in urls))

    def test_force_mode_ignores_max_regions(self):
        # The bench oracle (scripts/trial_bench/oracle.py force_config) runs
        # mode="force" and leaves max_regions at the pipeline default 48
        # instead of the desktop forced path's 30, on the grounds that force
        # mode never consults it. If that ever stopped being true, a dense
        # page would be skipped as "too_many_blocks" -- exactly the silent
        # no-op the oracle's repair counters exist to expose -- so pin it
        # here rather than only asserting it in a docstring.
        prepared_page = PreparedPage(
            page_id="page-1",
            source_path="sample.png",
            page_number=1,
            image=Image.new("RGB", (100, 120), "white"),
            original_size=(100, 120),
        )
        page = PageModel(
            page_id="page-1",
            width_px=100,
            height_px=120,
            subject=Subject.SCIENCE,
            blocks=[
                ContentBlock(
                    block_id=f"block-{index}",
                    block_type=BlockType.STEM,
                    bbox=Box(left=0, top=index * 10, width=80, height=8),
                    reading_order=index,
                    text=f"{index + 1}. 문제",
                )
                for index in range(4)
            ],
        )
        payload = {
            "problem_start_block_ids": ["block-0"],
            "choice_block_ids": [],
            "figure_block_ids": [],
            "display_titles": [],
            "notes": [],
        }

        class EmptyCache:
            def load_ai_repair(self, **_kwargs):
                return None

            def save_ai_repair(self, **_kwargs):
                return None

        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
            with patch.object(
                page_repair,
                "_request_ai_repair_with_model_fallback",
                return_value=(payload, "response-1", "gemini-3.1-pro-preview", [], {}),
            ):
                repaired = repair_page_model(
                    prepared_page,
                    page,
                    ocr_mode="gemini",
                    # One block over the cap: "auto" would skip, "force" must not.
                    config=build_ai_fallback_config(mode="force", max_regions=3),
                    cache=EmptyCache(),
                )

        summary = repaired.metadata["ai_fallback"]
        self.assertEqual("applied", summary["status"])
        self.assertTrue(summary["attempted"])
        self.assertNotIn("skip_reason", summary)

    def test_invalid_repair_response_still_records_provider_token_usage(self):
        prepared_page = PreparedPage(
            page_id="page-1",
            source_path="sample.png",
            page_number=1,
            image=Image.new("RGB", (100, 120), "white"),
            original_size=(100, 120),
        )
        page = PageModel(
            page_id="page-1",
            width_px=100,
            height_px=120,
            subject=Subject.SCIENCE,
            blocks=[
                ContentBlock(
                    block_id="block-1",
                    block_type=BlockType.STEM,
                    bbox=Box(left=0, top=0, width=80, height=40),
                    reading_order=0,
                    text="1. 문제",
                )
            ],
        )
        invalid_payload = {
            "problem_start_block_ids": ["unknown-block"],
            "choice_block_ids": [],
            "figure_block_ids": [],
            "display_titles": [],
            "notes": [],
        }
        usage = {
            "request_count": 1,
            "prompt_token_count": 100,
            "candidates_token_count": 20,
            "thoughts_token_count": 5,
            "total_token_count": 125,
        }

        class EmptyCache:
            def load_ai_repair(self, **_kwargs):
                return None

        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
            with patch.object(
                page_repair,
                "_request_ai_repair_with_model_fallback",
                return_value=(
                    invalid_payload,
                    "response-invalid",
                    "gemini-3.1-pro-preview",
                    [{"model": "gemini-3.1-pro-preview", "status": "ok"}],
                    usage,
                ),
            ):
                repaired = repair_page_model(
                    prepared_page,
                    page,
                    ocr_mode="gemini",
                    config=build_ai_fallback_config(mode="force"),
                    cache=EmptyCache(),
                )

        self.assertEqual("invalid_response", repaired.metadata["ai_fallback"]["status"])
        self.assertEqual(125, repaired.metadata["ai_fallback"]["token_usage"]["total_token_count"])

    def test_quota_exhausted_error_does_not_retry_or_fallback(self):
        urls = []
        sleep_calls = []

        def fake_post_json(url, payload, *, headers, timeout_ms):
            urls.append(url)
            raise RuntimeError(
                'Gemini request failed with HTTP 429: {"error":{"status":"RESOURCE_EXHAUSTED",'
                '"message":"Your prepayment credits are depleted."}}'
            )

        prepared_page = PreparedPage(
            page_id="page-1",
            source_path="sample.png",
            page_number=1,
            image=Image.new("RGB", (100, 120), "white"),
            original_size=(100, 120),
        )
        page = PageModel(
            page_id="page-1",
            width_px=100,
            height_px=120,
            subject=Subject.SCIENCE,
            blocks=[
                ContentBlock(
                    block_id="block-1",
                    block_type=BlockType.STEM,
                    bbox=Box(left=0, top=0, width=80, height=40),
                    reading_order=0,
                    text="1. 문제",
                )
            ],
        )
        config = build_ai_fallback_config(mode="force", timeout_ms=1000)

        with patch.object(page_repair, "_image_to_base64", return_value="encoded-image"):
            with patch.object(page_repair, "_post_json", side_effect=fake_post_json):
                with patch.object(
                    page_repair.time,
                    "sleep",
                    side_effect=lambda seconds: sleep_calls.append(seconds),
                ):
                    with self.assertRaisesRegex(RuntimeError, "RESOURCE_EXHAUSTED"):
                        page_repair._request_ai_repair_with_model_fallback(
                            prepared_page=prepared_page,
                            page=page,
                            config=config,
                            trigger_reasons=["forced"],
                            api_key="test-key",
                        )

        self.assertEqual([], sleep_calls)
        self.assertEqual(1, sum("gemini-3.1-pro-preview" in url for url in urls))
        self.assertFalse(any("gemini-2.5-pro" in url for url in urls))

    def test_repair_uses_fallback_model_cache_before_api_key_check(self):
        repair_payload = {
            "problem_start_block_ids": ["block-1"],
            "choice_block_ids": [],
            "figure_block_ids": [],
            "display_titles": [{"block_id": "block-1", "title": "1."}],
            "notes": ["cached"],
        }

        class FallbackOnlyCache:
            def __init__(self):
                self.models = []

            def load_ai_repair(self, *, page, provider, model, trigger_reasons):
                self.models.append(model)
                if model == page_repair.FALLBACK_GEMINI_REPAIR_MODEL:
                    return repair_payload, "cached-response"
                return None

        prepared_page = PreparedPage(
            page_id="page-1",
            source_path="sample.png",
            page_number=1,
            image=Image.new("RGB", (100, 120), "white"),
            original_size=(100, 120),
        )
        page = PageModel(
            page_id="page-1",
            width_px=100,
            height_px=120,
            subject=Subject.SCIENCE,
            blocks=[
                ContentBlock(
                    block_id="block-1",
                    block_type=BlockType.STEM,
                    bbox=Box(left=0, top=0, width=80, height=40),
                    reading_order=0,
                    text="1. 문제",
                )
            ],
        )
        cache = FallbackOnlyCache()

        with patch.dict(page_repair.os.environ, {}, clear=True):
            repaired = page_repair.repair_page_model(
                prepared_page,
                page,
                ocr_mode="none",
                config=build_ai_fallback_config(mode="force"),
                cache=cache,
            )

        summary = repaired.metadata["ai_fallback"]
        self.assertEqual(
            [
                page_repair.DEFAULT_GEMINI_REPAIR_MODEL,
                page_repair.FALLBACK_GEMINI_REPAIR_MODEL,
            ],
            cache.models,
        )
        self.assertTrue(summary["cache_hit"])
        self.assertTrue(summary["applied"])
        self.assertEqual("cached-response", summary["response_id"])
        self.assertEqual(page_repair.FALLBACK_GEMINI_REPAIR_MODEL, summary["model_used"])
        self.assertEqual("page_repair", summary["stage"])
        self.assertEqual("3단계 문항 경계 보정", summary["stage_label"])
        self.assertEqual("fallback_model_cache_hit", summary["model_fallback"]["reason"])
        self.assertEqual(
            {
                "stage": "page_repair",
                "order": 3,
                "label": "3단계 문항 경계 보정",
                "status": "cache_hit",
                "provider": "gemini",
                "model": page_repair.DEFAULT_GEMINI_REPAIR_MODEL,
                "model_used": page_repair.FALLBACK_GEMINI_REPAIR_MODEL,
                "enabled": True,
                "attempted": False,
                "applied": True,
                "cache_hit": True,
                "route": "ai_patch",
                "route_tier": "red",
            },
            repaired.metadata["ai_stages"]["page_repair"],
        )

    def test_ai_answer_matching_the_local_baseline_is_applied_but_not_changed(self):
        # Reproduces the review finding: page_repair.py used to set
        # summary["applied"] = True whenever a response validated, with no
        # signal for whether the write actually differed from the pre-repair
        # baseline. A stubbed Gemini answer that returns exactly the
        # baseline's own problem_start_block_ids must still report
        # applied=True (a validated response was written and _apply_repair_
        # payload always stamps grouping_source="ai_fallback") but
        # changed=False and blocks_changed=0 -- "the oracle agreed" and
        # "Gemini confirmed the trial's own grouping verbatim" must stay
        # distinguishable.
        prepared_page, page = self._two_numbered_blocks_page()
        # The local baseline already classifies both numbered blocks as
        # their own problem start (each has its own "N." marker); this
        # payload reproduces that classification exactly.
        payload = {
            "problem_start_block_ids": ["block-0", "block-1"],
            "choice_block_ids": [],
            "figure_block_ids": [],
            "display_titles": [],
            "notes": [],
        }

        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
            with patch.object(
                page_repair,
                "_request_ai_repair_with_model_fallback",
                return_value=(payload, "response-1", "gemini-3.1-pro-preview", [], {}),
            ):
                repaired = repair_page_model(
                    prepared_page,
                    page,
                    ocr_mode="gemini",
                    config=build_ai_fallback_config(mode="force"),
                    cache=_EmptyCache(),
                )

        summary = repaired.metadata["ai_fallback"]
        self.assertEqual("applied", summary["status"])
        self.assertTrue(summary["applied"])
        self.assertEqual(2, summary["baseline_problem_count"])
        self.assertEqual(2, summary["repaired_problem_count"])
        self.assertEqual(0, summary["blocks_changed"])
        self.assertEqual(0, summary["titles_changed"])
        self.assertEqual(0, summary["boxes_overridden"])
        self.assertEqual(0, summary["problem_metadata_changed"])
        self.assertFalse(summary["problems_regrouped"])
        self.assertFalse(summary["changed"])

    def test_ai_regrouping_that_differs_from_the_local_baseline_reports_changed(self):
        # The positive counterpart of the test above, and the only end-to-end
        # assertion that summary["changed"] is ever True. Without it,
        # hardcoding blocks_changed=0 / changed=False in repair_page_model's
        # applied branches leaves the suite green while the bench oracle
        # prints "NO AI EVIDENCE" for every case no matter what Gemini did.
        prepared_page, page = self._two_numbered_blocks_page()
        # The local baseline makes each numbered block its own problem; this
        # answer merges them into one, with block-1 demoted to a choice.
        payload = {
            "problem_start_block_ids": ["block-0"],
            "choice_block_ids": ["block-1"],
            "figure_block_ids": [],
            "display_titles": [],
            "notes": [],
        }

        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
            with patch.object(
                page_repair,
                "_request_ai_repair_with_model_fallback",
                return_value=(payload, "response-1", "gemini-3.1-pro-preview", [], {}),
            ):
                repaired = repair_page_model(
                    prepared_page,
                    page,
                    ocr_mode="gemini",
                    config=build_ai_fallback_config(mode="force"),
                    cache=_EmptyCache(),
                )

        summary = repaired.metadata["ai_fallback"]
        self.assertEqual("applied", summary["status"])
        self.assertEqual(2, summary["baseline_problem_count"])
        self.assertEqual(1, summary["repaired_problem_count"])
        self.assertEqual(1, summary["blocks_changed"])
        self.assertTrue(summary["problems_regrouped"])
        self.assertTrue(summary["changed"])

    def test_ai_titles_and_boxes_are_evidence_even_when_the_grouping_matches(self):
        # Reproduces the review finding: an answer whose
        # problem_start_block_ids reproduce the local baseline exactly, but
        # which rewrites a display_title and supplies bbox_px/review_flags
        # for both units, still rewrites the observation's keys and every
        # crop box. A "changed" signal that only watched block types and the
        # ProblemUnit partition would call this "NO AI EVIDENCE" -- a false
        # claim, and exactly the shape of a real response (display_titles is
        # in the schema's top-level "required" list and bbox_px/review_flags
        # are required per problem_unit).
        prepared_page, page = self._two_numbered_blocks_page()
        payload = {
            "problem_start_block_ids": ["block-0", "block-1"],
            "choice_block_ids": [],
            "figure_block_ids": [],
            "display_titles": [{"block_id": "block-0", "title": "지문 1~2"}],
            "problem_units": [
                {
                    "problem_start_block_id": "block-0",
                    "bbox_px": {"left": 0, "top": 0, "width": 80, "height": 18},
                    "review_flags": ["ai_boundary"],
                },
                {
                    "problem_start_block_id": "block-1",
                    "bbox_px": {"left": 0, "top": 10, "width": 80, "height": 18},
                    "review_flags": [],
                },
            ],
            "notes": [],
        }

        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
            with patch.object(
                page_repair,
                "_request_ai_repair_with_model_fallback",
                return_value=(payload, "response-1", "gemini-3.1-pro-preview", [], {}),
            ):
                repaired = repair_page_model(
                    prepared_page,
                    page,
                    ocr_mode="gemini",
                    config=build_ai_fallback_config(mode="force"),
                    cache=_EmptyCache(),
                )

        summary = repaired.metadata["ai_fallback"]
        self.assertEqual("applied", summary["status"])
        # The grouping half of the diff genuinely saw nothing.
        self.assertEqual(0, summary["blocks_changed"])
        self.assertFalse(summary["problems_regrouped"])
        # ... but the AI's title and boxes did reach the output.
        self.assertEqual(["지문 1~2", "문제"], [problem.title for problem in repaired.problems])
        self.assertEqual(2, summary["boxes_overridden"])
        self.assertGreater(summary["titles_changed"], 0)
        self.assertGreater(summary["problem_metadata_changed"], 0)
        self.assertTrue(summary["changed"])

    def test_a_page_repair_that_never_ran_defaults_every_change_counter_to_zero(self):
        # The false positive this instrumentation exists to prevent: a run
        # with no GEMINI_API_KEY at all must not report AI evidence. Nothing
        # else pins _attach_ai_fallback_summary's defaults, so a
        # setdefault("changed", True) there would make every disabled /
        # not_needed / missing_api_key page print full AI evidence.
        prepared_page, page = self._two_numbered_blocks_page()

        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            repaired = repair_page_model(
                prepared_page,
                page,
                ocr_mode="gemini",
                config=build_ai_fallback_config(mode="force"),
                cache=_EmptyCache(),
            )

        summary = repaired.metadata["ai_fallback"]
        self.assertEqual("missing_api_key", summary["status"])
        self.assertFalse(summary["applied"])
        self.assertEqual(0, summary["blocks_changed"])
        self.assertEqual(0, summary["titles_changed"])
        self.assertEqual(0, summary["boxes_overridden"])
        self.assertEqual(0, summary["problem_metadata_changed"])
        self.assertFalse(summary["problems_regrouped"])
        self.assertFalse(summary["changed"])

    def test_repair_prompt_prioritizes_all_problem_starts_on_busy_pages(self):
        page = PageModel(
            page_id="page-1",
            width_px=100,
            height_px=500,
            subject=Subject.SCIENCE,
            blocks=[
                ContentBlock(
                    block_id=f"block-{idx}",
                    block_type=BlockType.STEM,
                    bbox=Box(left=0, top=idx * 40, width=80, height=30),
                    reading_order=idx,
                    text=f"{idx + 1}. 문제",
                )
                for idx in range(5)
            ],
        )

        prompt = page_repair._build_repair_prompt(page, ["forced"])

        self.assertIn("For 5+ questions include all", prompt)
        self.assertIn("not only the first 2–3", prompt)
        self.assertIn("Do not return problem_units or notes", prompt)

        complex_prompt = page_repair._build_repair_prompt(
            page,
            ["merged_problem_block"],
        )
        self.assertIn("Return problem_units only", complex_prompt)


class TestClassificationState(unittest.TestCase):
    """White-box tests for the diff page_repair.py uses to tell "applied"
    (a validated AI response was written) apart from "changed" (the write
    actually differs from the pre-repair baseline) -- see
    test_ai_answer_matching_the_local_baseline_is_applied_but_not_changed
    above for the end-to-end version through repair_page_model.
    """

    def _page(self, block_types: dict[str, BlockType], problems: list[ProblemUnit]) -> PageModel:
        return PageModel(
            page_id="page-1",
            width_px=100,
            height_px=100,
            subject=Subject.SCIENCE,
            blocks=[
                ContentBlock(
                    block_id=block_id,
                    block_type=block_type,
                    bbox=Box(left=0, top=0, width=10, height=10),
                    reading_order=index,
                )
                for index, (block_id, block_type) in enumerate(block_types.items())
            ],
            problems=problems,
        )

    def _counters(self, baseline: PageModel, repaired: PageModel) -> dict:
        return page_repair._repair_change_counters(
            page_repair._classification_state(baseline),
            page_repair._classification_state(repaired),
        )

    def test_identical_pages_have_zero_changed_blocks(self):
        page = self._page(
            {"block-0": BlockType.TITLE, "block-1": BlockType.STEM},
            [ProblemUnit(unit_id="p1", subject=Subject.SCIENCE, title=None, stem_block_ids=["block-0", "block-1"])],
        )
        counters = self._counters(page, page)
        self.assertEqual(0, counters["blocks_changed"])
        self.assertFalse(counters["problems_regrouped"])
        self.assertEqual(0, counters["titles_changed"])
        self.assertEqual(0, counters["boxes_overridden"])
        self.assertEqual(0, counters["problem_metadata_changed"])
        self.assertFalse(counters["changed"])

    def test_a_retyped_block_counts_as_changed(self):
        baseline = self._page({"block-0": BlockType.STEM, "block-1": BlockType.CHOICE}, [])
        repaired = self._page({"block-0": BlockType.TITLE, "block-1": BlockType.CHOICE}, [])
        counters = self._counters(baseline, repaired)
        self.assertEqual(1, counters["blocks_changed"])
        self.assertTrue(counters["changed"])

    def test_a_different_problem_partition_counts_as_changed_even_with_identical_block_types(self):
        # Two problems merged into one: no block changed type, but the
        # stem/choice/figure partition of the ProblemUnits differs -- the
        # "problem_units" half of the diff, not the "blocks" half.
        block_types = {"block-0": BlockType.TITLE, "block-1": BlockType.TITLE}
        baseline = self._page(
            block_types,
            [
                ProblemUnit(unit_id="p1", subject=Subject.SCIENCE, title=None, stem_block_ids=["block-0"]),
                ProblemUnit(unit_id="p2", subject=Subject.SCIENCE, title=None, stem_block_ids=["block-1"]),
            ],
        )
        repaired = self._page(
            block_types,
            [ProblemUnit(unit_id="p1", subject=Subject.SCIENCE, title=None, stem_block_ids=["block-0", "block-1"])],
        )
        counters = self._counters(baseline, repaired)
        self.assertEqual(0, counters["blocks_changed"])
        self.assertTrue(counters["problems_regrouped"])
        self.assertTrue(counters["changed"])

    def test_grouping_source_metadata_alone_is_not_a_change(self):
        # _apply_repair_payload always stamps grouping_source="ai_fallback"
        # on every block it writes, and _annotate_problem_metadata stamps the
        # same on every problem, whether or not the AI's answer differed from
        # the baseline -- the diff must ignore those two keys, or every
        # applied page would read as "changed".
        baseline = self._page(
            {"block-0": BlockType.TITLE},
            [ProblemUnit(unit_id="p1", subject=Subject.SCIENCE, title="문제", stem_block_ids=["block-0"])],
        )
        baseline.blocks[0].metadata["grouping_source"] = None
        repaired = self._page(
            {"block-0": BlockType.TITLE},
            [ProblemUnit(unit_id="p1", subject=Subject.SCIENCE, title="문제", stem_block_ids=["block-0"])],
        )
        repaired.blocks[0].metadata["grouping_source"] = "ai_fallback"
        repaired.blocks[0].metadata["grouping_reason"] = ["forced"]
        repaired.problems[0].metadata["grouping_source"] = "ai_fallback"
        repaired.problems[0].metadata["grouping_reason"] = ["forced"]
        counters = self._counters(baseline, repaired)
        self.assertEqual(0, counters["blocks_changed"])
        self.assertFalse(counters["changed"])

    def test_an_ai_display_title_counts_as_changed_even_with_identical_grouping(self):
        # display_titles is in the response schema's top-level "required"
        # list, and assemble_page.py's _problem_display_title turns
        # block.metadata["display_title"] into ProblemUnit.title, which
        # common.problem_key turns into the observation key (and the passage
        # range). A snapshot that ignored it would call a page where the AI
        # supplied every title "NO AI EVIDENCE".
        baseline = self._page(
            {"block-0": BlockType.TITLE},
            [ProblemUnit(unit_id="p1", subject=Subject.SCIENCE, title="문제", stem_block_ids=["block-0"])],
        )
        repaired = self._page(
            {"block-0": BlockType.TITLE},
            [ProblemUnit(unit_id="p1", subject=Subject.SCIENCE, title="지문 1~2", stem_block_ids=["block-0"])],
        )
        repaired.blocks[0].metadata["display_title"] = "지문 1~2"
        counters = self._counters(baseline, repaired)
        self.assertEqual(0, counters["blocks_changed"])
        self.assertFalse(counters["problems_regrouped"])
        # One block display_title, plus the ProblemUnit title it produced.
        self.assertEqual(2, counters["titles_changed"])
        self.assertTrue(counters["changed"])

    def test_ai_bbox_px_and_review_flags_count_as_changed(self):
        # _annotate_problem_metadata writes each accepted AI unit's bbox_px
        # and review_flags onto problem.metadata, and
        # build_problem_board_edb.py's _should_prefer_problem_metadata_bbox
        # makes that bbox_px *replace* the locally derived crop box on every
        # AI-grouped problem -- i.e. every region the oracle scores.
        baseline = self._page(
            {"block-0": BlockType.TITLE},
            [ProblemUnit(unit_id="p1", subject=Subject.SCIENCE, title="문제", stem_block_ids=["block-0"])],
        )
        repaired = self._page(
            {"block-0": BlockType.TITLE},
            [ProblemUnit(unit_id="p1", subject=Subject.SCIENCE, title="문제", stem_block_ids=["block-0"])],
        )
        repaired.problems[0].metadata["bbox_px"] = {"left": 1.0, "top": 2.0, "width": 30.0, "height": 40.0}
        repaired.problems[0].metadata["review_flags"] = ["low_confidence"]
        repaired.problems[0].metadata["ai_problem_unit"] = {"unit_id": "u1"}
        counters = self._counters(baseline, repaired)
        self.assertEqual(0, counters["blocks_changed"])
        self.assertEqual(0, counters["titles_changed"])
        self.assertEqual(1, counters["boxes_overridden"])
        self.assertEqual(1, counters["problem_metadata_changed"])
        self.assertTrue(counters["changed"])


if __name__ == "__main__":
    unittest.main()
