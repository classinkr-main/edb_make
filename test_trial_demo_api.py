import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi.testclient import TestClient

from test_trial_api import FakeInspector, FakeParser, FakeVerifier, PDF_BODY
from trial_config import TrialConfig
from trial_demo import COOKIE_NAME, DemoConfig, hash_password
from trial_quota import MemoryQuotaStore
from trial_server import create_app

NOW = datetime(2026, 9, 17, 3, tzinfo=timezone.utc)
PASSWORD = 'demo-test-password'
HEADERS = {'X-Demo-Request': '1'}


class TestDemoApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.demo = DemoConfig(enabled=True, starts_at=NOW-timedelta(hours=1), ends_at=NOW+timedelta(hours=65),
                              password_hash=hash_password(PASSWORD), signing_secret='test-secret-'*4)

    def setUp(self):
        self.clock = NOW
        self.parser = FakeParser()
        self.inspector = FakeInspector()
        self.verifier = FakeVerifier()
        self.store = MemoryQuotaStore()
        self.config = TrialConfig(demo=self.demo, ip_salt='salt')

    def client(self, config=None):
        app = create_app(config or self.config, parser=self.parser, inspector=self.inspector,
                         quota_store=self.store, verifier=self.verifier, now=lambda: self.clock)
        return TestClient(app, base_url='https://demo.test')

    def login(self, client, password=PASSWORD, headers=None):
        return client.post('/api/demo/login', json={'password':password}, headers=headers or HEADERS)

    def parse(self, client, headers=None, body=PDF_BODY):
        return client.post('/api/demo/parse', content=body, headers={**HEADERS, **(headers or {})})

    def test_cookie_properties_and_config_do_not_disclose_secrets(self):
        with self.client() as client:
            config=client.get('/api/demo/config')
            self.assertTrue(config.json()['active'])
            self.assertFalse(config.json()['authenticated'])
            self.assertEqual('no-store', config.headers['cache-control'])
            for secret in (PASSWORD, self.demo.password_hash, self.demo.signing_secret):
                self.assertNotIn(secret, config.text)
            response=self.login(client)
            self.assertEqual(200,response.status_code)
            cookie=response.headers['set-cookie']
            for flag in ('HttpOnly','Secure','SameSite=strict','Path=/'):
                self.assertIn(flag,cookie)
            self.assertNotIn(PASSWORD,cookie)
            self.assertTrue(client.get('/api/demo/config').json()['authenticated'])
            self.assertIsNone(client.get('/api/demo/config').json()['daily_limit'])

    def test_no_auth_no_body_inspection_or_parser(self):
        with self.client() as client:
            self.assertEqual(401,self.parse(client).status_code)
            client.cookies.set(COOKIE_NAME,'forged')
            self.assertEqual(401,self.parse(client).status_code)
        self.assertEqual([],self.inspector.calls)
        self.assertEqual([],self.parser.calls)
        self.assertEqual({},self.store.used)

    def test_repeated_demo_bypasses_ip_global_and_bot_limits_but_public_does_not(self):
        with self.client(replace(self.config,daily_limit=1,global_daily_limit=1)) as client:
            self.assertEqual(200,client.post('/api/parse',content=PDF_BODY).status_code)
            self.assertEqual(503,client.post('/api/parse',content=PDF_BODY).status_code)
            charged=dict(self.store.used)
            verifier_count=len(self.verifier.calls)
            self.assertEqual(200,self.login(client).status_code)
            for n in range(5):
                r=self.parse(client,headers={'X-Real-IP':f'203.0.113.{n+1}'})
                self.assertEqual(200,r.status_code,r.text)
                self.assertEqual('demo',r.json()['mode'])
                self.assertIsNone(r.json()['remaining_today'])
                self.assertEqual(4,r.json()['processed_page_limit'])
            self.assertEqual(charged,self.store.used)
            self.assertEqual(verifier_count,len(self.verifier.calls))
            self.assertEqual(503,client.post('/api/parse',content=PDF_BODY).status_code)
            self.assertTrue(self.store.events[-2]['timing']['demo'])

    def test_demo_survives_missing_public_dependencies(self):
        self.store.available=False
        with self.client(replace(self.config,production=True)) as client:
            self.assertEqual(503,client.post('/api/parse',content=PDF_BODY).status_code)
            self.assertEqual(200,self.login(client).status_code)
            self.assertEqual(200,self.parse(client).status_code)

    def test_logout_removes_browser_session(self):
        with self.client() as client:
            self.login(client)
            self.assertEqual(204,client.post('/api/demo/logout',headers=HEADERS).status_code)
            self.assertFalse(client.get('/api/demo/config').json()['authenticated'])
            self.assertEqual(401,self.parse(client).status_code)

    def test_before_start_at_expiry_and_disabled(self):
        with self.client() as client:
            self.clock=self.demo.starts_at-timedelta(seconds=1)
            self.assertEqual(403,self.login(client).status_code)
            self.clock=NOW
            self.assertEqual(200,self.login(client).status_code)
            self.clock=self.demo.ends_at
            self.assertFalse(client.get('/api/demo/config').json()['active'])
            self.assertEqual(401,self.parse(client).status_code)
            self.assertEqual(403,self.login(client).status_code)
        with self.client(replace(self.config,demo=replace(self.demo,enabled=False))) as client:
            self.assertEqual(403,self.login(client).status_code)
            self.assertEqual(401,self.parse(client).status_code)

    def test_expiry_during_inspection_releases_slot_without_parsing(self):
        def inspect(*args,**kwargs):
            self.clock=self.demo.ends_at
            return FakeInspector()(*args,**kwargs)
        self.inspector=inspect
        with self.client() as client:
            self.login(client)
            self.assertEqual(401,self.parse(client).status_code)
        self.assertEqual([],self.parser.calls)

    def test_password_and_request_origin_validation(self):
        with self.client() as client:
            self.assertEqual(401,self.login(client,password='wrong-pass').status_code)
            self.assertEqual(403,client.post('/api/demo/login',json={'password':PASSWORD}).status_code)
            self.assertEqual(403,self.login(client,headers={**HEADERS,'Origin':'https://evil.test'}).status_code)
            self.assertEqual(403,self.login(client,headers={**HEADERS,'Origin':'https://['}).status_code)
            self.assertEqual(200,self.login(client,headers={**HEADERS,'Origin':'https://demo.test'}).status_code)
            self.assertEqual(403,self.parse(client,headers={'Origin':'https://evil.test'}).status_code)
            self.assertEqual(403,client.post('/api/demo/logout').status_code)

    def test_login_attempts_are_rate_limited(self):
        with self.client() as client:
            for _ in range(5):
                self.assertEqual(401,self.login(client,password='wrong-pass').status_code)
            response=self.login(client)
            self.assertEqual(429,response.status_code)
            self.assertEqual('60',response.headers['retry-after'])

    def test_demo_keeps_upload_limits(self):
        with self.client() as client:
            self.login(client)
            self.assertEqual(415,self.parse(client,body=b'not pdf').status_code)
            self.assertEqual(413,self.parse(client,body=b'x'*4_000_001).status_code)
            self.assertEqual(200,self.parse(client).status_code)
        self.assertEqual({},self.store.used)


if __name__ == '__main__':
    unittest.main()
