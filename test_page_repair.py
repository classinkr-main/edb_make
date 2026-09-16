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


class TestPageRepairConfig(unittest.TestCase):
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
                    config=build_ai_fallback_config(mode="force"),
                    cache=EmptyCache(),
                )

        summary = repaired.metadata["ai_fallback"]
        self.assertEqual("applied", summary["status"])
        self.assertTrue(summary["applied"])
        self.assertEqual(2, summary["baseline_problem_count"])
        self.assertEqual(2, summary["repaired_problem_count"])
        self.assertEqual(0, summary["blocks_changed"])
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

    def test_identical_pages_have_zero_changed_blocks(self):
        page = self._page(
            {"block-0": BlockType.TITLE, "block-1": BlockType.STEM},
            [ProblemUnit(unit_id="p1", subject=Subject.SCIENCE, title=None, stem_block_ids=["block-0", "block-1"])],
        )
        before = page_repair._classification_state(page)
        after = page_repair._classification_state(page)
        self.assertEqual(0, page_repair._count_changed_blocks(before[0], after[0]))
        self.assertEqual(before[1], after[1])

    def test_a_retyped_block_counts_as_changed(self):
        baseline = self._page({"block-0": BlockType.STEM, "block-1": BlockType.CHOICE}, [])
        repaired = self._page({"block-0": BlockType.TITLE, "block-1": BlockType.CHOICE}, [])
        baseline_types, _ = page_repair._classification_state(baseline)
        repaired_types, _ = page_repair._classification_state(repaired)
        self.assertEqual(1, page_repair._count_changed_blocks(baseline_types, repaired_types))

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
        baseline_types, baseline_partition = page_repair._classification_state(baseline)
        repaired_types, repaired_partition = page_repair._classification_state(repaired)
        self.assertEqual(0, page_repair._count_changed_blocks(baseline_types, repaired_types))
        self.assertNotEqual(baseline_partition, repaired_partition)

    def test_grouping_source_metadata_alone_is_not_a_change(self):
        # _apply_repair_payload always stamps grouping_source="ai_fallback"
        # on every block it writes, whether or not the AI's answer differed
        # from the baseline -- the diff must ignore metadata entirely, or
        # every applied page would read as "changed".
        baseline = self._page({"block-0": BlockType.TITLE}, [])
        baseline.blocks[0].metadata["grouping_source"] = None
        repaired = self._page({"block-0": BlockType.TITLE}, [])
        repaired.blocks[0].metadata["grouping_source"] = "ai_fallback"
        repaired.blocks[0].metadata["grouping_reason"] = ["forced"]
        baseline_types, _ = page_repair._classification_state(baseline)
        repaired_types, _ = page_repair._classification_state(repaired)
        self.assertEqual(0, page_repair._count_changed_blocks(baseline_types, repaired_types))


if __name__ == "__main__":
    unittest.main()
