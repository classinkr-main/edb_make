import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import fitz

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError as error:  # the CI lock has no web dependencies until Plan 2
    raise unittest.SkipTest(f"trial web dependencies missing: {error}")

import trial_server


def _text_pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    for number, (x, y) in zip((1, 2), ((60, 120), (330, 120))):
        page.insert_text((x, y), f"{number}. problem stem", fontsize=14)
        page.insert_text((x, y + 210), "① a   ② b   ③ c", fontsize=12)
    payload = doc.tobytes()
    doc.close()
    return payload


class TestTrialServer(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(trial_server.app)

    def test_health_reports_commit(self):
        with mock.patch.dict(os.environ, {"VERCEL_GIT_COMMIT_SHA": "abcdef1234"}):
            response = self.client.get("/api/health")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok", "commit": "abcdef1"}, response.json())

    def test_spike_hidden_without_configured_token(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRIAL_SPIKE_TOKEN", None)
            response = self.client.post("/api/spike", content=b"%PDF-1.7", headers={"x-spike-token": ""})
        self.assertEqual(404, response.status_code)

    def test_spike_rejects_wrong_token(self):
        with mock.patch.dict(os.environ, {"TRIAL_SPIKE_TOKEN": "right"}):
            response = self.client.post("/api/spike", content=b"%PDF-1.7", headers={"x-spike-token": "wrong"})
        self.assertEqual(404, response.status_code)

    def test_spike_rejects_oversized_body(self):
        with mock.patch.dict(os.environ, {"TRIAL_SPIKE_TOKEN": "right"}):
            response = self.client.post(
                "/api/spike",
                content=b"0" * (trial_server.SPIKE_MAX_BYTES + 1),
                headers={"x-spike-token": "right"},
            )
        self.assertEqual(413, response.status_code)

    def test_spike_rejects_non_pdf(self):
        with mock.patch.dict(os.environ, {"TRIAL_SPIKE_TOKEN": "right"}):
            response = self.client.post("/api/spike", content=b"not a pdf", headers={"x-spike-token": "right"})
        self.assertEqual(415, response.status_code)

    def test_spike_parses_pdf_and_reports_measurements(self):
        with mock.patch.dict(os.environ, {"TRIAL_SPIKE_TOKEN": "right"}):
            response = self.client.post(
                "/api/spike",
                content=_text_pdf_bytes(),
                headers={"x-spike-token": "right", "content-type": "application/pdf"},
            )
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(1, body["page_count"])
        self.assertEqual(0, body["pages_without_text"])
        self.assertEqual([1, 2], body["problem_numbers"])
        for key in ("import_ms", "parse_ms", "timing_ms", "max_rss_mb", "instance_age_s", "request_index"):
            self.assertIn(key, body)

    def test_trial_modules_do_not_import_desktop_server(self):
        code = "import sys, trial_server; sys.exit(1 if 'app_server' in sys.modules else 0)"
        completed = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent, capture_output=True)
        self.assertEqual(0, completed.returncode, completed.stderr.decode())


if __name__ == "__main__":
    unittest.main()
