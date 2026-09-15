import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError as error:  # the CI lock has no web dependencies until Plan 2
    raise unittest.SkipTest(f"trial web dependencies missing: {error}")

import trial_server


class TestTrialServerModule(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(trial_server.app)

    def test_health_reports_commit(self):
        with mock.patch.dict(os.environ, {"VERCEL_GIT_COMMIT_SHA": "abcdef1234"}):
            response = self.client.get("/api/health")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok", "commit": "abcdef1"}, {k: response.json()[k] for k in ("status", "commit")})

    def test_spike_endpoint_is_gone_even_with_a_token(self):
        # The Vercel spike is recorded in docs/web-trial-spike-results.md; the
        # measurement endpoint skipped every trial limit and must not ship.
        with mock.patch.dict(os.environ, {"TRIAL_SPIKE_TOKEN": "right"}):
            response = self.client.post("/api/spike", content=b"%PDF-1.7", headers={"x-spike-token": "right"})
        self.assertIn(response.status_code, {404, 405})
        self.assertFalse(hasattr(trial_server, "SPIKE_MAX_BYTES"))

    def test_trial_modules_do_not_import_desktop_server(self):
        code = "import sys, trial_server; sys.exit(1 if 'app_server' in sys.modules else 0)"
        completed = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent, capture_output=True)
        self.assertEqual(0, completed.returncode, completed.stderr.decode())


if __name__ == "__main__":
    unittest.main()
