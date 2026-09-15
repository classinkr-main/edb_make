import json
import tomllib
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
VERCEL = json.loads((PROJECT_ROOT / "vercel.json").read_text(encoding="utf-8"))
PYPROJECT = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _header(name: str) -> str:
    for rule in VERCEL.get("headers", []):
        if rule.get("source") == "/(.*)":
            for header in rule["headers"]:
                if header["key"].lower() == name.lower():
                    return header["value"]
    raise AssertionError(f"{name} header missing for /(.*)")


class TestVercelConfig(unittest.TestCase):
    def test_functions_key_matches_entrypoint(self):
        module, _, variable = PYPROJECT["tool"]["vercel"]["entrypoint"].partition(":")
        self.assertEqual("app", variable)
        self.assertEqual([f"{module}.py"], list(VERCEL["functions"]))

    def test_memory_is_not_set_in_vercel_json(self):
        # Fluid compute ignores it with a build warning; memory is a dashboard setting.
        for settings in VERCEL["functions"].values():
            self.assertNotIn("memory", settings)
            self.assertLessEqual(settings["maxDuration"], 300)

    def test_bundle_excludes_do_not_drop_runtime_modules(self):
        pattern = VERCEL["functions"]["trial_server.py"]["excludeFiles"]
        self.assertTrue(pattern.startswith("{") and pattern.endswith("}"))
        entries = set(pattern[1:-1].split(","))
        self.assertIn("test_*.py", entries)
        for runtime_path in ("**/*.py", "*.py", "**/**", "*"):
            self.assertNotIn(runtime_path, entries)
        runtime_modules = [
            "trial_server.py", "trial_config.py", "trial_input.py", "trial_preview.py", "trial_quota.py",
            "trial_turnstile.py", "problem_parser.py", "build_problem_board_edb.py", "structured_schema.py",
        ]
        for module in runtime_modules:
            self.assertTrue((PROJECT_ROOT / module).is_file(), module)
            for entry in entries:
                self.assertFalse(entry.endswith(".py") and not entry.startswith("test_") and module.endswith(entry.lstrip("*")), entry)

    def test_seoul_region(self):
        self.assertEqual(["icn1"], VERCEL["regions"])

    def test_daily_cron_hits_an_existing_get_route_after_kst_midnight(self):
        from trial_config import TrialConfig

        try:
            from trial_server import create_app
        except ModuleNotFoundError as error:
            self.skipTest(f"trial web dependencies missing: {error}")
        routes = {(route.path, method) for route in create_app(TrialConfig()).routes for method in getattr(route, "methods", ())}
        self.assertEqual(1, len(VERCEL["crons"]))
        cron = VERCEL["crons"][0]
        self.assertIn((cron["path"], "GET"), routes)
        minute, hour, *_ = cron["schedule"].split()
        self.assertEqual("15", hour)  # 15:xx UTC is 00:xx KST
        self.assertLess(int(minute), 60)

    def test_content_security_policy_allows_turnstile_and_nothing_else_external(self):
        policy = {
            directive.split()[0]: directive.split()[1:]
            for directive in (part.strip() for part in _header("Content-Security-Policy").split(";"))
            if directive
        }
        self.assertEqual(["'self'"], policy["default-src"])
        self.assertEqual(["'self'", "https://challenges.cloudflare.com"], policy["script-src"])
        self.assertEqual(["https://challenges.cloudflare.com"], policy["frame-src"])
        self.assertEqual(["'self'", "data:"], policy["img-src"])
        self.assertEqual(["'self'"], policy["connect-src"])
        self.assertEqual(["'none'"], policy["frame-ancestors"])
        self.assertEqual(["'none'"], policy["object-src"])
        for directive, sources in policy.items():
            self.assertNotIn("'unsafe-inline'", sources, directive)
            self.assertNotIn("'unsafe-eval'", sources, directive)

    def test_basic_hardening_headers(self):
        self.assertEqual("nosniff", _header("X-Content-Type-Options"))
        self.assertEqual("strict-origin-when-cross-origin", _header("Referrer-Policy"))
        self.assertIn("camera=()", _header("Permissions-Policy"))


if __name__ == "__main__":
    unittest.main()
