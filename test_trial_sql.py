import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
MIGRATION = PROJECT_ROOT / "supabase" / "migrations" / "20260915000000_web_trial.sql"
PG_BIN_CANDIDATES = [
    os.environ.get("TRIAL_PG_BIN", ""),
    "/opt/homebrew/opt/postgresql@17/bin",
    "/usr/lib/postgresql/17/bin",
    "/usr/lib/postgresql/16/bin",
]


def _pg_bin() -> Path | None:
    for candidate in PG_BIN_CANDIDATES:
        if candidate and (Path(candidate) / "initdb").is_file():
            return Path(candidate)
    found = shutil.which("initdb")
    return Path(found).parent if found else None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipIf(_pg_bin() is None, "PostgreSQL binaries not available")
class TestTrialSql(unittest.TestCase):
    """Applies the migration to a throwaway cluster that mimics Supabase's API roles."""

    @classmethod
    def setUpClass(cls):
        cls.bin = _pg_bin()
        cls.temp = tempfile.TemporaryDirectory(prefix="trial-pg-")
        root = Path(cls.temp.name)
        cls.data = root / "data"
        cls.socket_dir = root / "sock"
        cls.socket_dir.mkdir()
        cls.port = _free_port()
        subprocess.run(
            [cls.bin / "initdb", "-D", cls.data, "-A", "trust", "-U", "postgres", "--no-sync"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                cls.bin / "pg_ctl", "-D", cls.data,
                "-o", f"-k {cls.socket_dir} -p {cls.port} -c listen_addresses=''",
                "-w", "start", "-l", root / "pg.log",
            ],
            check=True,
            capture_output=True,
        )
        # Older Supabase projects grant anon/authenticated everything new in public,
        # so the migration must revoke it. Projects created after 2026-05-30 grant
        # nothing by default, so service_role must work from the migration's own grants.
        cls.psql(
            "create role anon nologin; create role authenticated nologin; create role service_role nologin bypassrls;"
            "grant usage on schema public to anon, authenticated, service_role;"
            "alter default privileges in schema public grant all on tables to anon, authenticated;"
            "alter default privileges in schema public grant all on functions to anon, authenticated;"
        )
        cls.psql(MIGRATION.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        subprocess.run([cls.bin / "pg_ctl", "-D", cls.data, "-m", "immediate", "stop"], capture_output=True)
        cls.temp.cleanup()

    @classmethod
    def psql(cls, sql: str, *, role: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        script = (f"set role {role};\n" if role else "") + sql
        return subprocess.run(
            [
                cls.bin / "psql", "-h", str(cls.socket_dir), "-p", str(cls.port), "-U", "postgres", "-d", "postgres",
                "-v", "ON_ERROR_STOP=1", "-X", "-q", "-A", "-t", "-c", script,
            ],
            check=check,
            capture_output=True,
            text=True,
        )

    def setUp(self):
        self.psql("truncate public.trial_quota; truncate public.trial_events; truncate public.trial_charges;")

    def consume(
        self,
        subject: str,
        limit: int = 3,
        global_limit: int = 500,
        day: str = "2026-09-15",
        request_id: str | None = None,
    ) -> str:
        rid = f"'{request_id}'::uuid" if request_id else "gen_random_uuid()"
        return self.psql(
            f"select allowed, remaining, coalesce(reason, '') from public.trial_consume({rid}, '{day}', '{subject}', {limit}, {global_limit});",
            role="service_role",
        ).stdout.strip()

    def refund(self, request_id: str) -> str:
        return self.psql(f"select public.trial_refund('{request_id}'::uuid);", role="service_role").stdout.strip()

    def used(self, subject: str) -> str:
        return self.psql(f"select coalesce(max(used), 0) from public.trial_quota where subject = '{subject}';").stdout.strip()

    def test_ip_limit_counts_down_then_blocks(self):
        self.assertEqual(["t|2|", "t|1|", "t|0|", "f|0|ip"], [self.consume("ip-a") for _ in range(4)])

    def test_global_limit_blocks_other_ips(self):
        self.assertEqual("t|2|", self.consume("ip-a", global_limit=2))
        self.assertEqual("t|2|", self.consume("ip-b", global_limit=2))
        self.assertEqual("f|0|global", self.consume("ip-c", global_limit=2))

    def test_days_are_independent(self):
        for _ in range(3):
            self.consume("ip-a")
        self.assertEqual("t|2|", self.consume("ip-a", day="2026-09-16"))

    def test_denied_consume_records_no_charge(self):
        for _ in range(4):
            self.consume("ip-a")
        self.assertEqual("3", self.psql("select count(*) from public.trial_charges;").stdout.strip())

    def test_same_request_id_charges_once(self):
        rid = "00000000-0000-4000-8000-000000000001"
        self.assertEqual("t|2|", self.consume("ip-a", request_id=rid))
        self.assertEqual("t|2|duplicate", self.consume("ip-a", request_id=rid))
        self.assertEqual("1", self.used("ip-a"))
        self.assertEqual("1", self.used("__global__"))

    def test_refund_by_request_id_restores_exactly_once(self):
        ids = [f"00000000-0000-4000-8000-00000000000{index}" for index in range(1, 4)]
        for rid in ids:
            self.consume("ip-a", request_id=rid)
        self.assertEqual("t", self.refund(ids[1]))
        self.assertEqual("f", self.refund(ids[1]))
        self.assertEqual("2", self.used("ip-a"))
        self.assertEqual("2", self.used("__global__"))
        self.assertEqual("t|0|", self.consume("ip-a"))

    def test_refund_of_unknown_request_changes_nothing(self):
        self.consume("ip-a")
        self.assertEqual("f", self.refund("00000000-0000-4000-8000-00000000abcd"))
        self.assertEqual("1", self.used("ip-a"))
        self.assertEqual("1", self.used("__global__"))

    def test_refund_never_goes_negative(self):
        rid = "00000000-0000-4000-8000-000000000009"
        self.consume("ip-a", request_id=rid)
        self.psql("update public.trial_quota set used = 0;")
        self.assertEqual("t", self.refund(rid))
        rows = self.psql("select string_agg(subject || '=' || used, ',' order by subject) from public.trial_quota;").stdout.strip()
        self.assertEqual("__global__=0,ip-a=0", rows)

    def test_quota_functions_give_up_on_long_lock_waits(self):
        # A lock wait longer than the 5 s client timeout would otherwise commit a
        # charge the server already reported as failed.
        for name in ("trial_consume", "trial_refund"):
            config = self.psql(f"select array_to_string(proconfig, ',') from pg_proc where proname = '{name}';").stdout.strip()
            self.assertIn("lock_timeout=3s", config, name)

    def pgbench(self, script: str, *, clients: int = 16, transactions: int = 25) -> None:
        # psql processes start too slowly to overlap; pgbench keeps 16 sessions open and truly concurrent.
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as handle:
            handle.write("set role service_role;\n" + script)
            script_path = handle.name
        try:
            subprocess.run(
                [
                    self.bin / "pgbench", "-h", str(self.socket_dir), "-p", str(self.port), "-U", "postgres",
                    "-n", "-c", str(clients), "-j", str(clients), "-t", str(transactions), "-f", script_path, "postgres",
                ],
                check=True,
                capture_output=True,
            )
        finally:
            os.unlink(script_path)

    def test_concurrent_consumers_never_exceed_limits(self):
        self.pgbench(
            "select * from public.trial_consume(gen_random_uuid(), '2026-09-15', 'ip-race-' || (random() * 3)::int, 3, 7);"
        )
        per_ip = self.psql(
            "select coalesce(max(used), 0) from public.trial_quota where subject like 'ip-race-%';"
        ).stdout.strip()
        self.assertLessEqual(int(per_ip), 3)
        self.assertEqual("7", self.used("__global__"))
        ip_total = self.psql("select sum(used) from public.trial_quota where subject like 'ip-race-%';").stdout.strip()
        self.assertEqual("7", ip_total)
        self.assertEqual("7", self.psql("select count(*) from public.trial_charges;").stdout.strip())

    def test_concurrent_first_calls_for_new_subjects_do_not_deadlock(self):
        self.pgbench(
            "select * from public.trial_consume(gen_random_uuid(), '2026-09-15', 'ip-new-' || (random() * 1000000)::int, 3, 100000);",
            transactions=10,
        )
        self.assertEqual("160", self.used("__global__"))
        ip_total = self.psql("select sum(used) from public.trial_quota where subject like 'ip-new-%';").stdout.strip()
        self.assertEqual("160", ip_total)

    def test_concurrent_consumes_and_refunds_on_shared_subjects_do_not_deadlock(self):
        # Hex subjects like real IP hashes sort before '__global__', which is the
        # order that deadlocked when refunds locked rows in index order.
        self.pgbench(
            "\\set n random(1, 2000000000)\n"
            "\\set s random(0, 2)\n"
            "select * from public.trial_consume(md5(:n || '-' || pg_backend_pid())::uuid, '2026-09-15', '0a' || :s, 1000000, 1000000);\n"
            "select public.trial_refund(md5(:n || '-' || pg_backend_pid())::uuid);\n",
            transactions=40,
        )
        self.assertEqual("0", self.used("__global__"))
        self.assertEqual("0", self.psql("select coalesce(sum(used), 0) from public.trial_quota where subject like '0a%';").stdout.strip())
        self.assertEqual("0", self.psql("select count(*) from public.trial_charges where not refunded;").stdout.strip())

    def test_anon_and_authenticated_cannot_call_or_read(self):
        rid = "00000000-0000-4000-8000-000000000001"
        for role in ("anon", "authenticated"):
            denied = self.psql(f"select * from public.trial_consume('{rid}', '2026-09-15', 'x', 3, 500);", role=role, check=False)
            self.assertNotEqual(0, denied.returncode, role)
            self.assertIn("permission denied", denied.stderr)
            refund = self.psql(f"select public.trial_refund('{rid}');", role=role, check=False)
            self.assertIn("permission denied", refund.stderr)
            cleanup = self.psql("select public.trial_cleanup('2026-09-15');", role=role, check=False)
            self.assertIn("permission denied", cleanup.stderr)
            for table in ("trial_quota", "trial_charges"):
                read = self.psql(f"select * from public.{table};", role=role, check=False)
                self.assertIn("permission denied", read.stderr)
            write = self.psql("insert into public.trial_events(kind) values ('parse');", role=role, check=False)
            self.assertIn("permission denied", write.stderr)
            funnel = self.psql("select * from public.trial_weekly_funnel;", role=role, check=False)
            self.assertIn("permission denied", funnel.stderr)

    def test_service_role_can_insert_events_and_read_funnel(self):
        self.psql(
            "insert into public.trial_events(kind, status, pages, problems) values ('parse', 200, 3, 12);"
            "insert into public.trial_events(kind, feature, action) values ('popup', 'edb', 'open');"
            "insert into public.trial_events(kind, feature, action) values ('popup', 'edb', 'inquiry');",
            role="service_role",
        )
        row = self.psql(
            "select parses_ok, popup_opens, inquiries from public.trial_weekly_funnel order by week desc limit 1;",
            role="service_role",
        ).stdout.strip()
        self.assertEqual("1|1|1", row)

    def test_event_counts_accept_huge_documents(self):
        self.psql(
            "insert into public.trial_events(kind, status, reject_code, source_pages, bytes) "
            "values ('parse', 422, 'too_many_pages', 40000, 3999999);",
            role="service_role",
        )
        self.assertEqual("40000", self.psql("select source_pages from public.trial_events;").stdout.strip())

    def test_cleanup_removes_old_rows(self):
        self.psql(
            "insert into public.trial_quota(day, subject, used) values (date '2026-09-01', 'old', 1), (date '2026-09-14', 'new', 1);"
            "insert into public.trial_events(created_at, kind) values (timestamptz '2026-01-01', 'parse'), (now(), 'parse');"
            "insert into public.trial_charges(request_id, day, subject) values "
            "('00000000-0000-4000-8000-000000000001', date '2026-09-01', 'old'), "
            "('00000000-0000-4000-8000-000000000002', date '2026-09-14', 'new');"
        )
        self.psql("select public.trial_cleanup(date '2026-09-15');", role="service_role")
        self.assertEqual("new", self.psql("select string_agg(subject, ',') from public.trial_quota;").stdout.strip())
        self.assertEqual("1", self.psql("select count(*) from public.trial_events;").stdout.strip())
        self.assertEqual("new", self.psql("select string_agg(subject, ',') from public.trial_charges;").stdout.strip())

    def test_rls_enabled(self):
        flags = self.psql(
            "select string_agg(relname || ':' || relrowsecurity, ',' order by relname) from pg_class "
            "where relname in ('trial_quota', 'trial_events', 'trial_charges');"
        ).stdout.strip()
        self.assertEqual("trial_charges:true,trial_events:true,trial_quota:true", flags)

    def test_migration_is_rerunnable(self):
        self.psql(MIGRATION.read_text(encoding="utf-8"))
        self.assertEqual("t|2|", self.consume("ip-after-rerun"))


if __name__ == "__main__":
    unittest.main()
