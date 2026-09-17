import unittest

from PIL import Image

from problem_parser import ParsedPage, ParsedProblem, ParsedRegion, ParseResult
from structured_schema import Box
from trial_continuation import continuations, passage_range


def _problem(problem_id: str, number: int | None, title: str) -> ParsedProblem:
    return ParsedProblem(
        problem_id=problem_id,
        number=number,
        title=title,
        regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=0.0, width=10.0, height=10.0))],
        risk_flags=[],
        image=Image.new("RGB", (10, 10), "white"),
    )


def _result(problems: list[ParsedProblem], *, source_pages: int, processed_pages: int = 1) -> ParseResult:
    pages = [
        ParsedPage(page_id=f"p{index + 1}", index=index, width=10, height=10, image=Image.new("RGB", (10, 10), "white"))
        for index in range(processed_pages)
    ]
    return ParseResult(pages=pages, problems=problems, source_page_count=source_pages, parser_version="dev", timing_ms={})


class TestPassageRange(unittest.TestCase):
    def test_parses_the_pipeline_title_and_common_dash_variants(self):
        self.assertEqual((10, 13), passage_range("지문 10~13"))
        self.assertEqual((4, 9), passage_range("지문 4∼9"))
        self.assertEqual((1, 3), passage_range("지문 1-3"))
        self.assertEqual((16, 17), passage_range("지문 16～17"))

    def test_rejects_non_ranges(self):
        for title in ("지문", "3.", "지문 9~4", "", "지문 a~b"):
            self.assertIsNone(passage_range(title), title)


class TestContinuations(unittest.TestCase):
    def test_trailing_numbers_of_a_passage_continue_on_the_next_page(self):
        result = _result(
            [_problem("pass", None, "지문 10~13"), _problem("q10", 10, "10."), _problem("q11", 11, "11."), _problem("q12", 12, "12.")],
            source_pages=16,
            processed_pages=4,
        )
        self.assertEqual({"pass": {"numbers": [13], "page": 5}}, continuations(result))

    def test_gaps_below_the_last_found_number_are_recognition_misses_not_cuts(self):
        result = _result(
            [_problem("pass", None, "지문 10~13"), _problem("q11", 11, "11."), _problem("q12", 12, "12.")],
            source_pages=16,
            processed_pages=4,
        )
        self.assertEqual({"pass": {"numbers": [13], "page": 5}}, continuations(result))

    def test_multiple_trailing_numbers(self):
        result = _result([_problem("pass", None, "지문 11~13"), _problem("q11", 11, "11.")], source_pages=16, processed_pages=4)
        self.assertEqual({"pass": {"numbers": [12, 13], "page": 5}}, continuations(result))

    def test_complete_documents_have_no_continuations(self):
        result = _result([_problem("pass", None, "지문 10~13"), _problem("q10", 10, "10.")], source_pages=4, processed_pages=4)
        self.assertEqual({}, continuations(result))

    def test_no_numbered_problems_means_no_continuations(self):
        result = _result([_problem("pass", None, "지문 10~13")], source_pages=16, processed_pages=4)
        self.assertEqual({}, continuations(result))

    def test_passages_that_end_before_the_last_number_are_not_marked(self):
        result = _result(
            [_problem("early", None, "지문 1~3"), _problem("q3", 3, "3."), _problem("q13", 13, "13.")],
            source_pages=16,
            processed_pages=4,
        )
        self.assertEqual({}, continuations(result))


if __name__ == "__main__":
    unittest.main()
