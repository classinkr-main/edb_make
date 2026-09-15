import json
import math
import tempfile
import unittest
from pathlib import Path

import fitz
from PIL import Image

from problem_parser import ParsedPage, ParsedProblem, ParsedRegion, ParseResult
from scripts.trial_bench import common
from scripts.trial_bench.make_inputs import make_input
from structured_schema import Box


def _result() -> ParseResult:
    page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
    problems = [
        ParsedProblem(
            problem_id="q1", number=1, title="1번",
            regions=[ParsedRegion(page_id="p1", bbox=Box(left=10.0, top=10.0, width=200.0, height=80.0))],
            risk_flags=[], image=Image.new("RGB", (200, 80), "white"),
        ),
        ParsedProblem(
            problem_id="passage", number=None, title="지문 1~3",
            regions=[ParsedRegion(page_id="p1", bbox=Box(left=10.0, top=100.0, width=200.0, height=300.0))],
            risk_flags=["passage_cross_page_merge_check"], image=Image.new("RGB", (200, 300), "white"),
        ),
    ]
    return ParseResult(pages=[page], problems=problems, source_page_count=16, parser_version="dev", timing_ms={"total": 7})


def _write_pdf(path: Path, page_count: int) -> Path:
    doc = fitz.open()
    for index in range(page_count):
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), f"{index + 1}. problem stem with enough words to count as text on the page", fontsize=12)
        page.insert_text((40, 300), "① a   ② b   ③ c   ④ d   ⑤ e", fontsize=12)
    doc.save(path)
    doc.close()
    return path


class TestCommon(unittest.TestCase):
    def test_passage_range_from_title(self):
        self.assertEqual([4, 9], common.passage_range_from_title("지문 4~9"))
        self.assertEqual([10, 13], common.passage_range_from_title("지문 10 ~ 13"))
        self.assertIsNone(common.passage_range_from_title("3번"))
        self.assertIsNone(common.passage_range_from_title(None))

    def test_problem_key(self):
        self.assertEqual("q12", common.problem_key(12, "12번"))
        self.assertEqual("p1-3", common.problem_key(None, "지문 1~3"))
        self.assertEqual("t:그림", common.problem_key(None, "그림"))

    def test_case_id_sanitizes_names(self):
        self.assertEqual("2026학년도_수능_국어", common.case_id(Path("/x/2026학년도 수능 국어.pdf")))
        self.assertEqual("01_물리학Ⅰ_문제지", common.case_id(Path("01 물리학Ⅰ_문제지.pdf")))

    def test_observation_has_numbers_boxes_and_no_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            observation = common.observation_from_result("case", _result(), crops_dir=Path(temp_dir) / "crops")
            self.assertTrue((Path(temp_dir) / "crops" / "q1.png").is_file())
        self.assertEqual(["q1", "p1-3"], [problem["key"] for problem in observation["problems"]])
        self.assertEqual([[1, 3]], observation["passage_ranges"])
        self.assertEqual(0, observation["problems"][0]["regions"][0]["page_index"])
        self.assertEqual({"left": 10.0, "top": 10.0, "width": 200.0, "height": 80.0}, observation["problems"][0]["regions"][0]["bbox"])
        self.assertEqual([[600, 800]], observation["page_sizes"])
        self.assertNotIn("text", json.dumps(observation, ensure_ascii=False))

    def test_percentile_is_nearest_rank(self):
        self.assertEqual(10, common.percentile(range(1, 11), 95))
        self.assertEqual(5, common.percentile(range(1, 11), 50))
        self.assertTrue(math.isnan(common.percentile([], 50)))

    def test_markdown_table(self):
        table = common.markdown_table(["a", "b"], [[1, None]])
        self.assertEqual("| a | b |\n|---|---|\n| 1 |  |", table)


class TestMakeInputs(unittest.TestCase):
    def test_trims_to_three_pages_and_records_the_case(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            source = _write_pdf(Path(temp_dir) / "긴 시험지.pdf", page_count=5)
            target = make_input(source, "korean", root=root)
            with fitz.open(target) as trimmed:
                self.assertEqual(3, trimmed.page_count)
            cases = json.loads((root / "cases.json").read_text(encoding="utf-8"))
        self.assertEqual({"subject": "korean", "source_page_count": 5, "source_name": "긴 시험지.pdf"}, cases["긴_시험지"])
        self.assertEqual(root / "inputs" / "긴_시험지.pdf", target)

    def test_short_documents_are_copied_whole(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            source = _write_pdf(Path(temp_dir) / "short.pdf", page_count=2)
            target = make_input(source, "science", root=root)
            with fitz.open(target) as copied:
                self.assertEqual(2, copied.page_count)


if __name__ == "__main__":
    unittest.main()
