"""The --board flag of the bench scripts must reach the parser as render_board_assets."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts.trial_bench import complexity, memory


def _fake_parse(captured: dict):
    def parse(source, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(pages=[1], problems=[], timing_ms={"total": 1, "render": 1, "segment": 0, "assets": 0})

    return parse


class TestComplexityBoardFlag(unittest.TestCase):
    def test_board_flag_reaches_the_parser(self):
        captured: dict = {}
        with mock.patch.object(complexity, "parse_problems", _fake_parse(captured)):
            self.assertEqual(0, complexity.main(["--words", "500", "--drawings", "0", "--pages", "1", "--board"]))
        self.assertTrue(captured["render_board_assets"])
        self.assertEqual(1, captured["max_pages"])

    def test_without_the_flag_the_parser_gets_no_cutouts(self):
        captured: dict = {}
        with mock.patch.object(complexity, "parse_problems", _fake_parse(captured)):
            complexity.main(["--words", "500", "--drawings", "0", "--pages", "1"])
        self.assertFalse(captured["render_board_assets"])


class TestMemoryBoardFlag(unittest.TestCase):
    def test_worker_functions_forward_the_flag(self):
        captured: dict = {}
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf = Path(temp_dir) / "x.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            with mock.patch.object(memory, "parse_problems", _fake_parse(captured)):
                memory.run_single_parse(pdf, board=True)
                self.assertTrue(captured["render_board_assets"])
                memory.run_two_overlapping_parses(pdf, board=False)
                self.assertFalse(captured["render_board_assets"])

    def test_measure_case_passes_the_flag_to_the_child_process(self):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"pages": 1, "rss_peak_mb": 1.0, "total_ms": 1}), stderr="")

        with mock.patch.object(memory.subprocess, "run", fake_run):
            memory.measure_case(Path("x.pdf"), concurrency=1, board=True)
            memory.measure_case(Path("x.pdf"), concurrency=1)
        self.assertIn("--board", commands[0])
        self.assertNotIn("--board", commands[1])

    def test_main_only_mentions_board_when_asked(self):
        calls = []

        def fake_measure_case(pdf, *, concurrency, **kwargs):
            calls.append(kwargs)
            return {"pages": 1, "rss_peak_mb": 1.0, "total_ms": 1}

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf = Path(temp_dir) / "x.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            with mock.patch.object(memory, "measure_case", fake_measure_case), mock.patch("builtins.print"):
                memory.main([str(pdf), "--concurrency", "1"])
                memory.main([str(pdf), "--concurrency", "1", "--board"])
        self.assertEqual([{}, {"board": True}], calls)


if __name__ == "__main__":
    unittest.main()
