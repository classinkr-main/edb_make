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
from scripts.trial_bench.oracle import force_config
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

    def test_passage_range_requires_the_passage_marker(self):
        # A bare "<digits><sep><digits>" run in free exam text is not a
        # passage range: the parser hands us this title verbatim whenever it
        # cannot number the unit, and it routinely contains ranges like this.
        self.assertIsNone(common.passage_range_from_title("표는 1-3족 원소의 성질을 나타낸 것이다"))
        self.assertIsNone(common.passage_range_from_title("2020~2023년 사이의 인구 변화를 나타낸 그래프이다"))

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

    def test_title_drops_free_text_but_keeps_labels(self):
        # segment.py sets display_title = text[:120] whenever a unit cannot be
        # numbered or grouped into a passage; that raw exam text must never
        # reach the observation (spec 5-1: "텍스트는 넣지 않는다"), unlike a
        # genuine number or passage marker, which is just a short label.
        sentence = "표는 1-3족 원소의 성질을 비교하여 나타낸 것이다 다음 물음에 답하시오"
        page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
        problems = [
            ParsedProblem(
                problem_id="q9", number=9, title=sentence,
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=0.0, width=10.0, height=10.0))],
                risk_flags=[], image=Image.new("RGB", (10, 10), "white"),
            ),
            ParsedProblem(
                problem_id="c1", number=None, title="이어지는 자료",
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=20.0, width=10.0, height=10.0))],
                risk_flags=[], image=Image.new("RGB", (10, 10), "white"),
            ),
        ]
        result = ParseResult(pages=[page], problems=problems, source_page_count=1, parser_version="dev", timing_ms={"total": 1})
        observation = common.observation_from_result("case", result)
        self.assertIsNone(observation["problems"][0]["title"])
        self.assertIsNone(observation["problems"][1]["title"])
        self.assertNotIn(sentence, json.dumps(observation, ensure_ascii=False))
        # The numbered path stays clean: a real marker is not exam text.
        self.assertEqual("1번", common.observation_from_result(
            "case",
            ParseResult(
                pages=[page],
                problems=[ParsedProblem(problem_id="q1", number=1, title="1번", regions=[], risk_flags=[], image=Image.new("RGB", (1, 1)))],
                source_page_count=1, parser_version="dev", timing_ms={"total": 1},
            ),
        )["problems"][0]["title"])

    def test_duplicate_fallback_titles_get_unique_keys_and_crops(self):
        # build_problem_board_edb.py gives every marker-continuation unit the
        # same constant title ("이어지는 자료"), so the fallback key collides
        # for any exam with more than one such unit.
        page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
        problems = [
            ParsedProblem(
                problem_id="c1", number=None, title="이어지는 자료",
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=0.0, width=10.0, height=10.0))],
                risk_flags=[], image=Image.new("RGB", (10, 10), "white"),
            ),
            ParsedProblem(
                problem_id="c2", number=None, title="이어지는 자료",
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=20.0, width=10.0, height=10.0))],
                risk_flags=[], image=Image.new("RGB", (10, 10), "white"),
            ),
        ]
        result = ParseResult(pages=[page], problems=problems, source_page_count=1, parser_version="dev", timing_ms={"total": 1})
        with tempfile.TemporaryDirectory() as temp_dir:
            crops_dir = Path(temp_dir) / "crops"
            observation = common.observation_from_result("case", result, crops_dir=crops_dir)
            crop_files = sorted(p.name for p in crops_dir.iterdir())
        keys = [problem["key"] for problem in observation["problems"]]
        self.assertEqual(2, len(set(keys)))
        self.assertEqual(2, len(crop_files))

    def test_duplicate_numbers_get_unique_keys_and_crops(self):
        # A parser mistake that numbers two units the same must not overwrite
        # the first crop with the second, nor collapse both JSON entries onto
        # one key (Task 10's expected/trial maps are keyed by "key").
        page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
        problems = [
            ParsedProblem(
                problem_id="c1", number=3, title="3번",
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=0.0, width=10.0, height=10.0))],
                risk_flags=[], image=Image.new("RGB", (10, 10), "white"),
            ),
            ParsedProblem(
                problem_id="c2", number=3, title="3번",
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=20.0, width=20.0, height=20.0))],
                risk_flags=[], image=Image.new("RGB", (20, 20), "white"),
            ),
        ]
        result = ParseResult(pages=[page], problems=problems, source_page_count=1, parser_version="dev", timing_ms={"total": 1})
        with tempfile.TemporaryDirectory() as temp_dir:
            crops_dir = Path(temp_dir) / "crops"
            observation = common.observation_from_result("case", result, crops_dir=crops_dir)
            crop_files = sorted(crops_dir.iterdir())
            self.assertEqual(2, len(crop_files))
            with Image.open(crop_files[0]) as first_crop:
                first_size = first_crop.size
            with Image.open(crop_files[1]) as second_crop:
                second_size = second_crop.size
        self.assertEqual(["q3", "q3#2"], [problem["key"] for problem in observation["problems"]])
        self.assertEqual((10, 10), first_size)
        self.assertEqual((20, 20), second_size)

    def test_crop_filename_sanitizes_unsafe_characters(self):
        # problem.image.save() treats "/" as a directory separator; a raw
        # fallback title routinely contains one (m/s, g/cm3, A/B, ...).
        page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
        problem = ParsedProblem(
            problem_id="c1", number=None, title="Which of A/B is correct?",
            regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=0.0, width=10.0, height=10.0))],
            risk_flags=[], image=Image.new("RGB", (10, 10), "white"),
        )
        result = ParseResult(pages=[page], problems=[problem], source_page_count=1, parser_version="dev", timing_ms={"total": 1})
        with tempfile.TemporaryDirectory() as temp_dir:
            crops_dir = Path(temp_dir) / "crops"
            common.observation_from_result("case", result, crops_dir=crops_dir)
            crop_files = list(crops_dir.iterdir())
        self.assertEqual(1, len(crop_files))
        self.assertNotIn("/", crop_files[0].name)

    def test_crop_filename_truncates_long_titles(self):
        # A raw Korean title over ~85 characters is over 255 bytes and would
        # otherwise raise OSError ENAMETOOLONG when saving the crop.
        long_title = "가" * 200
        page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
        problem = ParsedProblem(
            problem_id="c1", number=None, title=long_title,
            regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=0.0, width=10.0, height=10.0))],
            risk_flags=[], image=Image.new("RGB", (10, 10), "white"),
        )
        result = ParseResult(pages=[page], problems=[problem], source_page_count=1, parser_version="dev", timing_ms={"total": 1})
        with tempfile.TemporaryDirectory() as temp_dir:
            crops_dir = Path(temp_dir) / "crops"
            common.observation_from_result("case", result, crops_dir=crops_dir)
            crop_files = list(crops_dir.iterdir())
        self.assertEqual(1, len(crop_files))
        self.assertLessEqual(len(crop_files[0].stem), 80)

    def test_long_duplicate_titles_get_distinct_crops(self):
        # The "#n" uniquifier is appended to the key, but the crop stem was
        # truncated *after* that, so a title at segment.py's normal fallback
        # length (display_title = text[:120]) sliced the suffix back off and
        # every duplicate silently collapsed onto one file.
        long_title = "가" * 90
        page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
        problems = [
            ParsedProblem(
                problem_id=f"c{index}", number=None, title=long_title,
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=float(index * 40), width=float(size), height=float(size)))],
                risk_flags=[], image=Image.new("RGB", (size, size), "white"),
            )
            for index, size in enumerate((10, 22, 33))
        ]
        result = ParseResult(pages=[page], problems=problems, source_page_count=1, parser_version="dev", timing_ms={"total": 1})
        with tempfile.TemporaryDirectory() as temp_dir:
            crops_dir = Path(temp_dir) / "crops"
            observation = common.observation_from_result("case", result, crops_dir=crops_dir)
            crop_files = sorted(crops_dir.iterdir())
            self.assertEqual(3, len(crop_files))
            sizes = set()
            for path in crop_files:
                with Image.open(path) as crop:
                    sizes.add(crop.size)
        self.assertEqual({(10, 10), (22, 22), (33, 33)}, sizes)
        self.assertEqual(3, len({problem["crop"] for problem in observation["problems"]}))
        self.assertTrue(all(len(Path(problem["crop"]).stem) <= 80 for problem in observation["problems"]))

    def test_long_titles_sharing_a_prefix_get_distinct_crops(self):
        # Two *different* fallback titles that agree on their first 80
        # characters have different keys, so their crops must not share a file.
        prefix = "가" * 90
        page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
        problems = [
            ParsedProblem(
                problem_id=f"c{index}", number=None, title=f"{prefix} {suffix}",
                regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=float(index * 40), width=float(size), height=float(size)))],
                risk_flags=[], image=Image.new("RGB", (size, size), "white"),
            )
            for index, (suffix, size) in enumerate((("갑", 10), ("을", 22)))
        ]
        result = ParseResult(pages=[page], problems=problems, source_page_count=1, parser_version="dev", timing_ms={"total": 1})
        with tempfile.TemporaryDirectory() as temp_dir:
            crops_dir = Path(temp_dir) / "crops"
            observation = common.observation_from_result("case", result, crops_dir=crops_dir)
            crop_files = list(crops_dir.iterdir())
        self.assertEqual(2, len({problem["key"] for problem in observation["problems"]}))
        self.assertEqual(2, len(crop_files))

    def test_percentile_is_nearest_rank(self):
        self.assertEqual(10, common.percentile(range(1, 11), 95))
        self.assertEqual(5, common.percentile(range(1, 11), 50))
        self.assertTrue(math.isnan(common.percentile([], 50)))

    def test_markdown_table(self):
        table = common.markdown_table(["a", "b"], [[1, None]])
        self.assertEqual("| a | b |\n|---|---|\n| 1 |  |", table)


class TestParseInScratch(unittest.TestCase):
    def test_copies_into_a_fresh_temp_dir_and_forwards_kwargs(self):
        captured: dict = {}

        def _spy(src, *, work_dir, max_pages, **kw):
            # Read the copy's bytes now: parse_in_scratch's temp dir is gone
            # by the time this call returns to the test.
            captured.update(source=src, work_dir=work_dir, max_pages=max_pages, source_bytes=src.read_bytes(), extra=kw)
            return src

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "exam.pdf"
            source.write_bytes(b"%PDF-1.4 fake exam bytes for the spy to copy")
            original_bytes = source.read_bytes()
            common.parse_in_scratch(source, _spy, subject="korean")

            self.assertNotEqual(source, captured["source"])
            self.assertEqual(original_bytes, captured["source_bytes"])
            self.assertEqual(captured["source"].parent, captured["work_dir"].parent)
            self.assertEqual(common.MAX_PAGES, captured["max_pages"])
            self.assertEqual({"subject": "korean"}, captured["extra"])

        # No .pipeline_cache warm start on a second run: the copy's directory
        # is cleaned up once parse_in_scratch returns.
        self.assertFalse(captured["source"].parent.exists())


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
            short_source = _write_pdf(Path(temp_dir) / "short.pdf", page_count=2)
            long_source = _write_pdf(Path(temp_dir) / "긴 시험지.pdf", page_count=5)
            # Two calls into the same root exercise the cases.json merge
            # branch (load_json(...) if cases_path.is_file() else {}).
            short_target = make_input(short_source, "science", root=root)
            long_target = make_input(long_source, "korean", root=root)
            with fitz.open(short_target) as copied:
                self.assertEqual(2, copied.page_count)
            cases = json.loads((root / "cases.json").read_text(encoding="utf-8"))
        self.assertEqual({"subject": "science", "source_page_count": 2, "source_name": "short.pdf"}, cases["short"])
        self.assertEqual({"subject": "korean", "source_page_count": 5, "source_name": "긴 시험지.pdf"}, cases["긴_시험지"])
        self.assertEqual(root / "inputs" / "short.pdf", short_target)
        self.assertEqual(root / "inputs" / "긴_시험지.pdf", long_target)


class TestOracleConfig(unittest.TestCase):
    def test_force_config_fails_loudly_and_forces_repair(self):
        config = force_config("")
        self.assertEqual("force", config["mode"])
        self.assertEqual("gemini", config["provider"])
        self.assertTrue(config["fail_on_error"])
        self.assertEqual("gemini-x", force_config("gemini-x")["model"])


if __name__ == "__main__":
    unittest.main()
