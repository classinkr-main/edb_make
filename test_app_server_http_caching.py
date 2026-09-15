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


class TestPagesJsonParsing(unittest.TestCase):
    def setUp(self) -> None:
        app_server._load_pages_json_pages.cache_clear()

    def test_repeat_reads_of_one_revision_parse_the_file_once(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            pages_json = Path(raw_tmp) / "pages.json"
            pages_json.write_text(
                app_server.json.dumps([{"page_id": "p1", "blocks": []}]),
                encoding="utf-8",
            )
            session = {"pages_json_path": str(pages_json)}

            first = app_server._session_pages_json_pages(session)
            for _ in range(4):
                app_server._session_pages_json_pages(session)

            self.assertEqual([{"page_id": "p1", "blocks": []}], first)
            self.assertEqual(1, app_server._load_pages_json_pages.cache_info().misses)
            self.assertEqual(4, app_server._load_pages_json_pages.cache_info().hits)

    def test_a_rewritten_file_is_reparsed(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            pages_json = Path(raw_tmp) / "pages.json"
            pages_json.write_text(app_server.json.dumps([{"page_id": "p1"}]), encoding="utf-8")
            session = {"pages_json_path": str(pages_json)}
            self.assertEqual([{"page_id": "p1"}], app_server._session_pages_json_pages(session))

            pages_json.write_text(
                app_server.json.dumps([{"page_id": "p1"}, {"page_id": "p2"}]),
                encoding="utf-8",
            )
            self.assertEqual(
                [{"page_id": "p1"}, {"page_id": "p2"}],
                app_server._session_pages_json_pages(session),
            )

    def test_missing_or_malformed_files_return_no_pages(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            missing = Path(raw_tmp) / "absent.json"
            self.assertEqual([], app_server._session_pages_json_pages({"pages_json_path": str(missing)}))
            self.assertEqual([], app_server._session_pages_json_pages({}))

            broken = Path(raw_tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            self.assertEqual([], app_server._session_pages_json_pages({"pages_json_path": str(broken)}))

            not_a_list = Path(raw_tmp) / "object.json"
            not_a_list.write_text("{}", encoding="utf-8")
            self.assertEqual([], app_server._session_pages_json_pages({"pages_json_path": str(not_a_list)}))


class TestSessionFilePathCollection(unittest.TestCase):
    def setUp(self) -> None:
        app_server._canonical_reference_path.cache_clear()

    def test_existing_artifacts_are_collected_and_canonicalized(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            edb = root / "lesson.edb"
            edb.write_bytes(b"edb")
            session = {"edb_path": str(root / "." / "lesson.edb")}

            self.assertEqual({str(edb.resolve())}, app_server.collect_session_file_paths(session))

    def test_a_deleted_artifact_stops_being_collected_even_after_caching(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            edb = root / "lesson.edb"
            edb.write_bytes(b"edb")
            session = {"edb_path": str(edb)}

            self.assertEqual({str(edb.resolve())}, app_server.collect_session_file_paths(session))
            edb.unlink()
            self.assertEqual(set(), app_server.collect_session_file_paths(session))

            edb.write_bytes(b"edb again")
            self.assertEqual({str(edb.resolve())}, app_server.collect_session_file_paths(session))

    def test_missing_and_blank_references_are_ignored(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            session = {"edb_path": str(Path(raw_tmp) / "absent.edb"), "pages_json_path": ""}
            self.assertEqual(set(), app_server.collect_session_file_paths(session))

    def test_history_scan_reuses_resolutions_across_entries(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            edb = root / "lesson.edb"
            edb.write_bytes(b"edb")
            session = {"edb_path": str(edb)}
            history = [{"id": f"entry-{index}", "session": session} for index in range(5)]

            self.assertEqual(
                {str(edb.resolve())},
                app_server.collect_session_history_file_paths(history),
            )
            # One decode for the entry-level key and one for the snapshot key.
            self.assertEqual(1, app_server._canonical_reference_path.cache_info().misses)


class TestSessionPollDoesNotRewriteHistory(unittest.TestCase):
    @contextmanager
    def _server_with_session(self, session):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            history_path = runtime_dir / "session_history.json"
            latest_path = runtime_dir / "latest_session.json"
            with (
                patch.object(app_server, "RUNTIME_DIR", runtime_dir),
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", runtime_dir / "generated_session.js"),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                server.latest_session = session
                latest_path.write_text(app_server.json.dumps(session), encoding="utf-8")
                try:
                    yield server, history_path
                finally:
                    server.server_close()

    def test_repeat_registration_of_an_unchanged_session_writes_once(self) -> None:
        session = {"session_name": "Poll", "output_dir": "/tmp/out", "problems": [], "pages": []}
        with self._server_with_session(session) as (server, history_path):
            self.assertTrue(server.register_current_session(session))
            self.assertTrue(history_path.exists())
            first_write = history_path.stat().st_mtime_ns
            first_payload = history_path.read_text(encoding="utf-8")

            for _ in range(5):
                self.assertTrue(server.register_current_session(session))

            self.assertEqual(first_write, history_path.stat().st_mtime_ns)
            self.assertEqual(first_payload, history_path.read_text(encoding="utf-8"))

    def test_a_changed_session_is_still_recorded(self) -> None:
        session = {"session_name": "Poll", "output_dir": "/tmp/out", "problems": [], "pages": []}
        with self._server_with_session(session) as (server, history_path):
            self.assertTrue(server.register_current_session(session))

            updated = dict(session)
            updated["problems"] = [{"id": "p1"}]
            server.latest_session = updated
            self.assertTrue(server.register_current_session(updated))

            history = app_server.json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual([{"id": "p1"}], history[0]["session"]["problems"])

    def test_head_detection_requires_both_identity_and_content(self) -> None:
        session = {"session_name": "Poll", "output_dir": "/tmp/out", "problems": []}
        entry = app_server._session_history_entry(session)
        self.assertTrue(app_server._session_history_head_stores([entry], session))
        self.assertFalse(app_server._session_history_head_stores([], session))
        self.assertFalse(
            app_server._session_history_head_stores(
                [{**entry, "session": {"session_name": "Poll", "problems": [{"id": "x"}]}}],
                session,
            )
        )
        self.assertFalse(
            app_server._session_history_head_stores([{**entry, "id": "other"}], session)
        )


if __name__ == "__main__":
    unittest.main()
