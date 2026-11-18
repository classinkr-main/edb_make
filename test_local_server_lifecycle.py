from __future__ import annotations

import threading
import unittest
from pathlib import Path

import app_server


PROJECT_ROOT = Path(__file__).resolve().parent


class TestLocalServerLifecycle(unittest.TestCase):
    def test_launcher_detaches_server_and_enables_persistent_logging(self) -> None:
        source = (PROJECT_ROOT / "run_local_app.command").read_text(encoding="utf-8")

        self.assertIn('FOREGROUND="${EDB_FOREGROUND:-0}"', source)
        self.assertIn('"--log-file" "$APP_LOG"', source)
        self.assertIn('nohup "$PYTHON_BIN" "${APP_ARGS[@]}"', source)
        self.assertIn('HEALTH_URL="${URL}api/health"', source)
        self.assertIn('if /usr/bin/curl -fsS "$HEALTH_URL"', source)
        self.assertIn('서버를 터미널과 분리해 실행합니다', source)

    def test_server_health_exposes_process_identity(self) -> None:
        server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
        try:
            payload = server.health_payload()
        finally:
            server.server_close()

        self.assertTrue(payload["ok"])
        self.assertEqual(app_server.APP_NAME, payload["app"])
        self.assertGreater(payload["pid"], 0)
        self.assertTrue(payload["sessionEpoch"])
        self.assertGreaterEqual(payload["uptimeSeconds"], 0)

    def test_shutdown_reason_keeps_the_first_cause(self) -> None:
        server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            server.request_shutdown("api_system_shutdown")
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual("api_system_shutdown", server.shutdown_reason())
            self.assertEqual("api_system_shutdown", server.note_shutdown_reason("later_reason"))
        finally:
            server.server_close()

    def test_queue_network_failure_probes_server_health(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")

        self.assertIn("async function probeLocalServerHealth", source)
        self.assertIn("async function classifyNetworkRequestError", source)
        self.assertIn("fetch('/api/health'", source)
        self.assertIn("const serverAvailable = await probeLocalServerHealth(1200)", source)
        self.assertIn("classified.code = serverAvailable ? fallbackCode : 'local_server_unavailable'", source)
        self.assertIn("await classifyNetworkRequestError(e, 'recognition_connection_failed')", source)
        self.assertIn("await classifyNetworkRequestError(e, 'registration_connection_failed')", source)


if __name__ == "__main__":
    unittest.main()
