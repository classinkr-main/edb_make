import base64
import io
import json
import random
import unittest
from dataclasses import replace
from unittest import mock

from PIL import Image, features

from problem_parser import ParsedPage, ParsedProblem, ParsedRegion, ParseResult
from structured_schema import Box
from trial_preview import (
    BOARD_BACKGROUND_RGB,
    PREVIEW_FORMAT,
    PREVIEW_MIME,
    PREVIEW_STEPS,
    PreviewBudgetExceeded,
    _payload_for_step,
    build_parse_body,
    build_parse_payload,
    compose_board_preview,
    encode_preview_data_uri,
)


def _noise(width: int, height: int, seed: int) -> Image.Image:
    # Deterministic high-entropy pixels so JPEG sizes are realistic.
    return Image.frombytes("RGB", (width, height), random.Random(seed).randbytes(width * height * 3))


def _result(problem_count: int = 2, page_size=(1915, 2811), problem_size=(1600, 900)) -> ParseResult:
    pages = [ParsedPage(page_id="p1", index=0, width=page_size[0], height=page_size[1], image=_noise(*page_size, seed=1))]
    problems = [
        ParsedProblem(
            problem_id=f"q{index}",
            number=index,
            title=f"{index}.",
            regions=[ParsedRegion(page_id="p1", bbox=Box(left=10.5, top=20.25, width=300.0, height=200.0))],
            risk_flags=["passage_cross_page_merge_check"] if index == 1 else (["marker_conflicts"] if index == 2 else []),
            image=_noise(*problem_size, seed=index + 10),
        )
        for index in range(1, problem_count + 1)
    ]
    return ParseResult(pages=pages, problems=problems, source_page_count=16, parser_version="abc1234", timing_ms={"total": 1500})


def _decode(data_uri: str) -> Image.Image:
    prefix = f"data:{PREVIEW_MIME};base64,"
    assert data_uri.startswith(prefix), data_uri[:40]
    return Image.open(io.BytesIO(base64.b64decode(data_uri[len(prefix):])))


def _board(width: int, height: int, seed: int) -> Image.Image:
    # A chalk cutout: uniform chalk RGB, ink only in the alpha channel.
    board = Image.new("RGBA", (width, height), (248, 249, 246, 0))
    board.putalpha(Image.frombytes("L", (width, height), random.Random(seed).randbytes(width * height)))
    return board


def _with_boards(result: ParseResult) -> ParseResult:
    result.problems[:] = [
        replace(problem, board_image=_board(*problem.image.size, seed=index + 40))
        for index, problem in enumerate(result.problems)
    ]
    return result


class TestEncodePreviewDataUri(unittest.TestCase):
    def test_downscales_long_side_and_never_upscales(self):
        big = _decode(encode_preview_data_uri(_noise(2000, 1000, 1), long_side=800, quality=70))
        self.assertEqual((800, 400), big.size)
        small = _decode(encode_preview_data_uri(_noise(300, 200, 1), long_side=800, quality=70))
        self.assertEqual((300, 200), small.size)

    def test_downscale_uses_hamming_with_reducing_gap(self):
        with mock.patch.object(Image.Image, "resize", wraps=Image.new("RGB", (2339, 3308), "white").resize) as resize:
            encode_preview_data_uri(Image.new("RGB", (2339, 3308), "white"), long_side=1200, quality=70)
        self.assertEqual(Image.Resampling.HAMMING, resize.call_args.args[1])
        self.assertEqual(2.0, resize.call_args.kwargs["reducing_gap"])

    def test_downscaled_dimensions_are_unchanged(self):
        preview = _decode(encode_preview_data_uri(_noise(2339, 3308, seed=1), long_side=1200, quality=70))
        self.assertEqual((848, 1200), preview.size)

    def test_uses_webp_in_this_runtime(self):
        self.assertTrue(features.check("webp"))
        self.assertEqual(("WEBP", "image/webp"), (PREVIEW_FORMAT, PREVIEW_MIME))
        self.assertTrue(encode_preview_data_uri(_noise(30, 20, 1), long_side=800, quality=70).startswith("data:image/webp;base64,"))

    def test_is_deterministic_for_the_same_pixels(self):
        first = encode_preview_data_uri(_noise(300, 200, 3), long_side=800, quality=70)
        second = encode_preview_data_uri(_noise(300, 200, 3), long_side=800, quality=70)
        self.assertEqual(first, second)


class TestBuildParsePayload(unittest.TestCase):
    def test_shape_and_coordinates(self):
        payload = build_parse_payload(_result(), remaining_today=2, elapsed_ms=1234, processed_page_limit=3)
        self.assertEqual("abc1234", payload["parser_version"])
        self.assertEqual(1234, payload["elapsed_ms"])
        self.assertEqual(16, payload["source_page_count"])
        self.assertEqual(1, payload["processed_page_count"])
        self.assertEqual(3, payload["processed_page_limit"])
        self.assertEqual(2, payload["remaining_today"])
        page = payload["pages"][0]
        self.assertEqual({"page_id": "p1", "index": 0, "width": 1915, "height": 2811}, {k: page[k] for k in ("page_id", "index", "width", "height")})
        problem = payload["problems"][0]
        self.assertEqual(
            {"problem_id": "q1", "number": 1, "title": "1.", "risk_flags": ["passage_cross_page_merge_check"], "needs_review": False},
            {k: problem[k] for k in ("problem_id", "number", "title", "risk_flags", "needs_review")},
        )
        self.assertTrue(payload["problems"][1]["needs_review"])
        self.assertEqual([{"page_id": "p1", "bbox": {"left": 10.5, "top": 20.25, "width": 300.0, "height": 200.0}}], problem["regions"])
        self.assertEqual(0, payload["preview_step"])
        self.assertEqual(PREVIEW_STEPS[0].page_long_side, max(_decode(page["preview"]).size))
        self.assertEqual(PREVIEW_STEPS[0].problem_long_side, max(_decode(problem["preview"]).size))
        self.assertIsNone(problem["board"])
        self.assertFalse(payload["board_previews"])

    def test_falls_back_to_smaller_step_when_over_budget(self):
        roomy, roomy_body = build_parse_body(_result(problem_count=4), remaining_today=1, elapsed_ms=1, processed_page_limit=3)
        tight, tight_body = build_parse_body(
            _result(problem_count=4),
            remaining_today=1,
            elapsed_ms=1,
            processed_page_limit=3,
            budget_bytes=len(roomy_body) - 1,
        )
        self.assertGreater(tight["preview_step"], 0)
        self.assertLessEqual(len(tight_body), len(roomy_body) - 1)
        self.assertEqual(json.dumps(tight, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"), tight_body)

    def test_rejects_when_even_last_step_exceeds_budget(self):
        with self.assertRaises(PreviewBudgetExceeded):
            build_parse_payload(_result(), remaining_today=1, elapsed_ms=1, processed_page_limit=3, budget_bytes=10)

    def test_many_noisy_crops_cannot_escape_the_real_response_budget(self):
        result = _result(problem_count=1, page_size=(450, 450), problem_size=(450, 450))
        # Reuse one immutable crop to reproduce a large response without a large
        # test memory footprint. All 60 entries must still be encoded in the JSON.
        result.problems.extend([result.problems[0]] * 59)
        with self.assertRaises(PreviewBudgetExceeded):
            build_parse_body(result, remaining_today=1, elapsed_ms=1, processed_page_limit=4)

    def test_regions_with_non_finite_coordinates_are_dropped(self):
        result = _result()
        result.problems[0].regions.append(ParsedRegion(page_id="p1", bbox=Box(left=float("inf"), top=0.0, width=1.0, height=1.0)))
        result.problems[0].regions.append(ParsedRegion(page_id="p1", bbox=Box(left=1.0, top=float("nan"), width=1.0, height=1.0)))
        payload = build_parse_payload(result, remaining_today=1, elapsed_ms=1, processed_page_limit=3)
        self.assertEqual(1, len(payload["problems"][0]["regions"]))
        json.dumps(payload, allow_nan=False)

    def test_is_deterministic(self):
        first = build_parse_payload(_result(), remaining_today=1, elapsed_ms=1, processed_page_limit=3)
        second = build_parse_payload(_result(), remaining_today=1, elapsed_ms=1, processed_page_limit=3)
        self.assertEqual(first, second)

    def test_extra_fields_are_merged_into_the_payload(self):
        payload = build_parse_payload(
            _result(),
            remaining_today=2,
            elapsed_ms=10,
            processed_page_limit=3,
            extra={"timing_ms": {"render": 1}, "instance_id": "abcd1234", "instance_age_s": 12.5},
        )
        self.assertEqual({"render": 1}, payload["timing_ms"])
        self.assertEqual("abcd1234", payload["instance_id"])
        self.assertEqual(12.5, payload["instance_age_s"])
        self.assertNotIn("timing_ms", build_parse_payload(_result(), remaining_today=2, elapsed_ms=10, processed_page_limit=3))

    def test_build_parse_body_returns_the_bytes_it_measured(self):
        payload, body = build_parse_body(_result(), remaining_today=2, elapsed_ms=10, processed_page_limit=3)
        self.assertEqual(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"), body)
        self.assertEqual(payload, build_parse_payload(_result(), remaining_today=2, elapsed_ms=10, processed_page_limit=3))


class TestBoardPreviews(unittest.TestCase):
    def test_board_is_composited_and_encoded_at_the_step_size(self):
        payload = build_parse_payload(_with_boards(_result()), remaining_today=2, elapsed_ms=1, processed_page_limit=4)
        self.assertEqual(0, payload["preview_step"])
        self.assertTrue(payload["board_previews"])
        for problem in payload["problems"]:
            board = _decode(problem["board"])
            self.assertEqual(PREVIEW_STEPS[0].board_long_side, max(board.size))
            self.assertEqual("RGB", board.mode)

    def test_step_table_keeps_boards_until_the_last_step(self):
        self.assertEqual([800, 600, 600, None], [step.board_long_side for step in PREVIEW_STEPS])
        self.assertEqual([1000, 1000, 900, 700], [step.page_long_side for step in PREVIEW_STEPS])
        self.assertEqual([800, 600, 600, 450], [step.problem_long_side for step in PREVIEW_STEPS])

    def test_compose_flattens_onto_the_charcoal_board(self):
        transparent = Image.new("RGBA", (4, 4), (248, 249, 246, 0))
        self.assertEqual(BOARD_BACKGROUND_RGB, compose_board_preview(transparent).getpixel((0, 0)))
        opaque = Image.new("RGBA", (4, 4), (248, 249, 246, 255))
        self.assertEqual((248, 249, 246), compose_board_preview(opaque).getpixel((0, 0)))
        self.assertEqual("RGB", compose_board_preview(opaque).mode)

    def test_board_color_matches_the_desktop_default_theme(self):
        from build_problem_board_edb import BOARD_THEME_PALETTES, DEFAULT_BOARD_THEME

        self.assertEqual(BOARD_THEME_PALETTES[DEFAULT_BOARD_THEME]["background"], BOARD_BACKGROUND_RGB)

    def test_problems_without_a_cutout_get_null_board(self):
        result = _with_boards(_result(problem_count=3))
        result.problems[1] = replace(result.problems[1], board_image=None)
        payload = build_parse_payload(result, remaining_today=2, elapsed_ms=1, processed_page_limit=4)
        self.assertEqual([True, False, True], [problem["board"] is not None for problem in payload["problems"]])
        self.assertTrue(payload["board_previews"])

    def test_boards_are_dropped_on_the_last_step_before_rejecting(self):
        result = _with_boards(_result(problem_count=4))
        sizes = []
        for step_index in range(len(PREVIEW_STEPS)):
            payload = _payload_for_step(result, step_index, remaining_today=1, elapsed_ms=1, processed_page_limit=4)
            sizes.append(len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        payload, body = build_parse_body(result, remaining_today=1, elapsed_ms=1, processed_page_limit=4, budget_bytes=sizes[2] - 1)
        self.assertEqual(3, payload["preview_step"])
        self.assertFalse(payload["board_previews"])
        self.assertTrue(all(problem["board"] is None for problem in payload["problems"]))
        self.assertLessEqual(len(body), sizes[2] - 1)
        with self.assertRaises(PreviewBudgetExceeded):
            build_parse_body(result, remaining_today=1, elapsed_ms=1, processed_page_limit=4, budget_bytes=sizes[3] - 1)


if __name__ == "__main__":
    unittest.main()
