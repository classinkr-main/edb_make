"""Behavior checks for the password gate without a browser dependency."""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class TestDemoWeb(unittest.TestCase):
    def test_authentication_expiry_errors_and_logout(self):
        script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {hidden:false, value:'', disabled:false, handlers:{}, addEventListener(event, fn) {this.handlers[event]=fn;}});
  return elements.get(id);
};
const requests = [];
const replies = [];
let timer;
const sandbox = {
  window:{addEventListener(){}}, document:{getElementById:element,addEventListener(){}}, Date, Number, Math, JSON,
  setTimeout(fn) { timer=fn; return 1; }, clearTimeout() {},
  fetch(url, options) {
    requests.push({url, options});
    const reply = replies.shift();
    if (reply instanceof Error) return Promise.reject(reply);
    return Promise.resolve({ok:reply.status>=200 && reply.status<300,status:reply.status,
      json:async()=>reply.body,text:async()=>JSON.stringify(reply.body)});
  }
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('public/demo.js','utf8'), sandbox);
const demo=sandbox.window.TRIAL_DEMO;
const end=new Date(Date.now()+3600000).toISOString();
(async()=>{
  replies.push({status:200,body:{active:true,authenticated:false,ends_at:end}});
  let resets=0;
  await demo.initialize(()=>resets++);
  assert.equal(demo.canParse(),false);
  assert.equal(element('demo-app').hidden,true);
  assert.equal(element('demo-login').hidden,false);
  element('demo-password').value='demo-test-secret';
  replies.push({status:401,body:{error:{code:'demo_auth_required'}}});
  const badLogin=element('demo-login').handlers.submit({preventDefault(){}});
  assert.equal(element('demo-password').value,'','password cleared before response');
  await badLogin;
  assert.equal(demo.canParse(),false);
  assert.match(element('demo-message').textContent,/비밀번호를 확인/);
  element('demo-password').value='demo-test-secret';
  replies.push({status:429,body:{error:{code:'throttled'}}});
  await element('demo-login').handlers.submit({preventDefault(){}});
  assert.match(element('demo-message').textContent,/요청이 많아요/);
  replies.push({status:200,body:{authenticated:true,ends_at:end}});
  await element('demo-login').handlers.submit({preventDefault(){}});
  assert.equal(demo.canParse(),true);
  assert.equal(element('demo-app').hidden,false);
  assert.match(element('demo-expiration').textContent,/한국시간/);
  const loginRequest=requests.find(r=>r.url==='/api/demo/login');
  assert.equal(loginRequest.options.credentials,'same-origin');
  assert.equal(loginRequest.options.headers['X-Demo-Request'],'1');
  assert.deepEqual(JSON.parse(loginRequest.options.body),{password:'demo-test-secret'});
  assert.equal(demo.handleResponse(401,'{"error":{"code":"demo_auth_required"}}'),true);
  assert.equal(demo.canParse(),false);
  assert.equal(element('demo-app').hidden,true);
  assert.match(element('demo-message').textContent,/인증이 만료/);
  replies.push({status:200,body:{authenticated:true,ends_at:end}});
  await element('demo-login').handlers.submit({preventDefault(){}});
  demo.setBusy(true);
  assert.equal(element('demo-logout').disabled,true);
  demo.setBusy(false);
  replies.push({status:204});
  await element('demo-logout').handlers.click();
  assert.equal(demo.canParse(),false);
  assert.match(element('demo-message').textContent,/로그아웃했어요/);
  assert.equal(requests.at(-1).options.headers['X-Demo-Request'],'1');
  replies.push({status:200,body:{authenticated:true,ends_at:new Date(Date.now()-1000).toISOString()}});
  await element('demo-login').handlers.submit({preventDefault(){}});
  assert.equal(demo.canParse(),false);
  assert.equal(element('demo-login').hidden,true);
  assert.match(element('demo-message').textContent,/기간이 끝났어요/);
  assert.ok(resets>=4,'locking clears rendered results');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        subprocess.run(["node", "-e", script], cwd=ROOT, check=True)

    def test_demo_page_uses_shared_renderer_and_keeps_premium_gates(self):
        html = (ROOT / "public/demo/index.html").read_text()
        self.assertIn('data-trial-mode="demo"', html)
        self.assertIn('id="demo-app" hidden', html)
        self.assertLess(html.index('src="/demo.js"'), html.index('src="/app.js"'))
        for feature in ("edb", "image", "edit"):
            self.assertIn(f'data-premium="{feature}"', html)
        self.assertNotIn("하루 3회", html)
        self.assertNotIn('data-premium="limit_daily"', html)
        code = (ROOT / "public/demo.js").read_text()
        self.assertNotIn("localStorage", code)
        self.assertNotIn("sessionStorage", code)


if __name__ == "__main__":
    unittest.main()
