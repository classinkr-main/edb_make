from __future__ import annotations

import http.client
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app_server


@contextmanager
def running_app_server():
    with TemporaryDirectory() as raw_tmp:
        runtime_dir = Path(raw_tmp)
        with (
            patch.object(app_server, "RUNTIME_DIR", runtime_dir),
            patch.object(app_server, "LATEST_SESSION_JSON", runtime_dir / "latest_session.json"),
            patch.object(app_server, "SESSION_HISTORY_JSON", runtime_dir / "session_history.json"),
            patch.object(app_server, "GENERATED_SESSION_JS", runtime_dir / "generated_session.js"),
        ):
            server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield server
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


@contextmanager
def client_for(server):
    host, port = server.server_address[0], server.server_address[1]
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        yield connection
    finally:
        connection.close()


class TestHttpCachingAndKeepAlive(unittest.TestCase):
    def test_static_assets_are_revalidated_rather_than_uncacheable(self) -> None:
        with running_app_server() as server, client_for(server) as connection:
            connection.request("GET", "/index.html", headers={"Host": "127.0.0.1"})
            response = connection.getresponse()
            body = response.read()

            self.assertEqual(200, response.status)
            self.assertTrue(body)
            self.assertEqual("no-cache", response.getheader("Cache-Control"))
            self.assertIsNone(response.getheader("Pragma"))
            last_modified = response.getheader("Last-Modified")
            self.assertIsNotNone(last_modified)

            connection.request(
                "GET",
                "/index.html",
                headers={"Host": "127.0.0.1", "If-Modified-Since": last_modified},
            )
            revalidated = connection.getresponse()
            revalidated.read()
            self.assertEqual(304, revalidated.status)

    def test_api_responses_stay_uncacheable(self) -> None:
        with running_app_server() as server, client_for(server) as connection:
            connection.request("GET", "/api/health", headers={"Host": "127.0.0.1"})
            response = connection.getresponse()
            response.read()

            self.assertEqual(200, response.status)
            self.assertEqual("no-store, max-age=0", response.getheader("Cache-Control"))
            self.assertEqual("no-cache", response.getheader("Pragma"))

    def test_generated_session_script_stays_uncacheable(self) -> None:
        with running_app_server() as server, client_for(server) as connection:
            connection.request("GET", "/generated_session.js", headers={"Host": "127.0.0.1"})
            response = connection.getresponse()
            response.read()

            self.assertEqual(200, response.status)
            self.assertEqual("no-store, max-age=0", response.getheader("Cache-Control"))

    def test_handler_supplied_cache_control_is_not_overwritten(self) -> None:
        class CacheControlProbeHandler(app_server.AppRequestHandler):
            def do_GET(self) -> None:  # noqa: D401 - test-only route
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "2")
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(b"ok")

        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            with patch.object(app_server, "RUNTIME_DIR", runtime_dir):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), CacheControlProbeHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with client_for(server) as connection:
                        connection.request("GET", "/api/file", headers={"Host": "127.0.0.1"})
                        response = connection.getresponse()
                        response.read()
                        self.assertEqual(
                            ["private, max-age=3600"],
                            response.msg.get_all("Cache-Control"),
                        )
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_one_connection_serves_several_requests(self) -> None:
        with running_app_server() as server, client_for(server) as connection:
            for _ in range(3):
                connection.request("GET", "/api/health", headers={"Host": "127.0.0.1"})
                response = connection.getresponse()
                response.read()
                self.assertEqual(200, response.status)
                self.assertNotEqual("close", (response.getheader("Connection") or "").lower())
            self.assertEqual("HTTP/1.1", app_server.AppRequestHandler.protocol_version)

    def test_post_rejected_before_its_body_is_read_closes_the_connection(self) -> None:
        with running_app_server() as server, client_for(server) as connection:
            announced = app_server.MAX_JSON_BODY_BYTES + 1
            connection.putrequest("POST", "/api/session/mutate")
            connection.putheader("Host", "127.0.0.1")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(announced))
            connection.endheaders()

            response = connection.getresponse()
            response.read()
            self.assertGreaterEqual(response.status, 400)
            # The body was never read, so the server must not keep the
            # connection alive with those bytes still queued behind it.
            self.assertTrue(response.will_close)

    def test_post_whose_body_is_read_keeps_the_connection_alive(self) -> None:
        with running_app_server() as server, client_for(server) as connection:
            connection.request(
                "POST",
                "/api/user-settings",
                body=b"{}",
                headers={"Host": "127.0.0.1", "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            response.read()
            self.assertLess(response.status, 500)
            self.assertFalse(response.will_close)

            connection.request("GET", "/api/health", headers={"Host": "127.0.0.1"})
            followup = connection.getresponse()
            followup.read()
            self.assertEqual(200, followup.status)


if __name__ == "__main__":
    unittest.main()
