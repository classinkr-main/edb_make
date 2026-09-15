import base64
import io
import json
import random
import unittest

from PIL import Image

from problem_parser import ParsedPage, ParsedProblem, ParsedRegion, ParseResult
from structured_schema import Box
from trial_preview import PREVIEW_STEPS, build_parse_payload, encode_jpeg_data_uri


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
    prefix = "data:image/jpeg;base64,"
    assert data_uri.startswith(prefix)
    return Image.open(io.BytesIO(base64.b64decode(data_uri[len(prefix):])))


class TestEncodeJpegDataUri(unittest.TestCase):
    def test_downscales_long_side_and_never_upscales(self):
        big = _decode(encode_jpeg_data_uri(_noise(2000, 1000, 1), long_side=800, quality=70))
        self.assertEqual((800, 400), big.size)
        small = _decode(encode_jpeg_data_uri(_noise(300, 200, 1), long_side=800, quality=70))
        self.assertEqual((300, 200), small.size)


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

    def test_falls_back_to_smaller_step_when_over_budget(self):
        roomy = build_parse_payload(_result(problem_count=4), remaining_today=1, elapsed_ms=1, processed_page_limit=3)
        roomy_size = len(json.dumps(roomy, ensure_ascii=False).encode("utf-8"))
        tight = build_parse_payload(
            _result(problem_count=4),
            remaining_today=1,
            elapsed_ms=1,
            processed_page_limit=3,
            budget_bytes=roomy_size - 1,
        )
        self.assertGreater(tight["preview_step"], 0)
        self.assertLessEqual(len(json.dumps(tight, ensure_ascii=False).encode("utf-8")), roomy_size - 1)

    def test_uses_last_step_when_nothing_fits(self):
        payload = build_parse_payload(_result(), remaining_today=1, elapsed_ms=1, processed_page_limit=3, budget_bytes=10)
        self.assertEqual(len(PREVIEW_STEPS) - 1, payload["preview_step"])

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


if __name__ == "__main__":
    unittest.main()
