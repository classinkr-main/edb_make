import assert from "node:assert/strict";
import test from "node:test";

import worker, { REPORT_CONTRACT, payloadHash, reportId, validateReport } from "../src/index.js";


function validPayload() {
  return {
    schemaVersion: 1,
    category: "bug",
    description: "설정 화면에서 저장 버튼이 작동하지 않습니다.",
    app: {
      id: "ClassInEDBMVP",
      version: "0.1.0",
      platform: "macos",
    },
    context: {
      view: "board",
      settingsTab: "board",
      itemCount: 3,
    },
    diagnostics: {
      system: "Darwin",
    },
  };
}


class FakeStatement {
  constructor(database, sql) {
    this.database = database;
    this.sql = sql;
    this.values = [];
  }

  bind(...values) {
    this.values = values;
    return this;
  }

  async run() {
    this.database.rows.push({ sql: this.sql, values: this.values });
    return { success: true };
  }

  async first() {
    if (this.sql.includes("schema_ready")) {
      this.database.healthProbeCount += 1;
      return { schema_ready: this.database.healthSchemaReady ? 1 : 0 };
    }
    if (!this.sql.includes("WHERE payload_hash = ?")) {
      throw new Error(`unexpected first() query: ${this.sql}`);
    }
    const hash = this.values[0];
    const row = this.database.rows.find(candidate => candidate.values[13] === hash);
    if (!row) return null;
    return {
      id: row.values[0],
      created_at: row.values[1],
      consent_to_contact: row.values[10],
    };
  }
}


class FakeD1 {
  constructor({ healthSchemaReady = true } = {}) {
    this.rows = [];
    this.healthProbeCount = 0;
    this.healthSchemaReady = healthSchemaReady;
  }

  prepare(sql) {
    return new FakeStatement(this, sql);
  }
}


function availableRateLimiter(onLimit = () => {}) {
  return {
    async limit(argument) {
      onLimit(argument);
      return { success: true };
    },
  };
}


function readyEnvironment(database = new FakeD1()) {
  return {
    REPORTS_DB: database,
    REPORT_RATE_LIMITER: availableRateLimiter(),
  };
}


test("report ids are recognizable and unique", () => {
  const first = reportId(new Date("2026-07-27T00:00:00Z"));
  const second = reportId(new Date("2026-07-27T00:00:00Z"));
  assert.match(first, /^EDB-20260727-[0-9A-F]{10}$/);
  assert.notEqual(first, second);
});


test("validation accepts the EDB schema and rejects unknown apps", () => {
  assert.equal(validateReport(validPayload()), "");
  const unknown = validPayload();
  unknown.app.id = "OtherApp";
  assert.equal(validateReport(unknown), "unknown_app");
});


test("health endpoint performs read-only D1 and rate-limiter readiness probes", async () => {
  const limiterKeys = [];
  const environment = {
    REPORTS_DB: new FakeD1(),
    REPORT_RATE_LIMITER: availableRateLimiter(({ key }) => limiterKeys.push(key)),
  };
  const response = await worker.fetch(
    new Request("https://reports.classin.cloud/health"),
    environment,
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    ok: true,
    ready: true,
    service: "classin-edb-reports",
    readiness: {
      ready: true,
      bindings: {
        REPORTS_DB: true,
        REPORT_RATE_LIMITER: true,
      },
    },
    reportContract: REPORT_CONTRACT,
  });
  assert.equal(environment.REPORTS_DB.rows.length, 0);
  assert.equal(environment.REPORTS_DB.healthProbeCount, 1);
  assert.deepEqual(limiterKeys, ["edb-report-health-readiness"]);
});


test("health endpoint fails readiness when a required binding is missing", async () => {
  for (const environment of [
    {},
    { REPORTS_DB: new FakeD1() },
    { REPORT_RATE_LIMITER: availableRateLimiter() },
  ]) {
    const response = await worker.fetch(
      new Request("https://reports.classin.cloud/health"),
      environment,
    );
    assert.equal(response.status, 503);
    const payload = await response.json();
    assert.equal(payload.ok, false);
    assert.equal(payload.ready, false);
  }
});


test("health endpoint fails closed for wrong D1 or throwing/malformed limiter bindings", async () => {
  const environments = [
    readyEnvironment(new FakeD1({ healthSchemaReady: false })),
    {
      REPORTS_DB: new FakeD1(),
      REPORT_RATE_LIMITER: { async limit() { throw new Error("limiter unavailable"); } },
    },
    {
      REPORTS_DB: new FakeD1(),
      REPORT_RATE_LIMITER: { async limit() { return { allowed: true }; } },
    },
  ];
  const originalConsoleError = console.error;
  console.error = () => {};
  try {
    for (const environment of environments) {
      const response = await worker.fetch(
        new Request("https://reports.classin.cloud/health"),
        environment,
      );
      assert.equal(response.status, 503);
      const payload = await response.json();
      assert.equal(payload.ok, false);
      assert.equal(payload.ready, false);
    }
  } finally {
    console.error = originalConsoleError;
  }
});


test("valid reports are inserted and receive a receipt", async () => {
  const database = new FakeD1();
  const response = await worker.fetch(
    new Request("https://reports.classin.cloud/v1/edb-reports", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(validPayload()),
    }),
    readyEnvironment(database),
  );
  assert.equal(response.status, 201);
  const receipt = await response.json();
  assert.equal(receipt.ok, true);
  assert.equal(receipt.duplicate, false);
  assert.match(receipt.reportId, /^EDB-\d{8}-[0-9A-F]{10}$/);
  assert.equal(database.rows.length, 1);
  assert.equal(database.rows[0].values[6], validPayload().description);
});


test("consented contact and structured operation errors are stored", async () => {
  const database = new FakeD1();
  const payload = validPayload();
  payload.reporter = {
    contact: "customer@example.com",
    consentToContact: true,
  };
  payload.context.lastOperationError = {
    operation: "session_publish",
    code: "edb_write_failed",
    status: 500,
  };
  const response = await worker.fetch(
    new Request("https://reports.classin.cloud/v1/edb-reports", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    }),
    readyEnvironment(database),
  );
  assert.equal(response.status, 201);
  const receipt = await response.json();
  assert.equal(receipt.contactAccepted, true);
  assert.equal(database.rows[0].values[9], "customer@example.com");
  assert.equal(database.rows[0].values[10], 1);
  assert.equal(database.rows[0].values[11], "edb_write_failed");
  assert.equal(database.rows[0].values[12], "session_publish");
  assert.match(database.rows[0].values[13], /^[0-9a-f]{64}$/);
});


test("canonical payload hashes make retries idempotent", async () => {
  const database = new FakeD1();
  const payload = validPayload();
  payload.submittedAt = "2026-08-24T03:01:02Z";
  const firstResponse = await worker.fetch(
    new Request("https://reports.classin.cloud/v1/edb-reports", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    }),
    readyEnvironment(database),
  );
  const firstReceipt = await firstResponse.json();
  const reorderedPayload = {
    diagnostics: payload.diagnostics,
    context: payload.context,
    app: payload.app,
    description: payload.description,
    category: payload.category,
    schemaVersion: payload.schemaVersion,
    submittedAt: payload.submittedAt,
  };
  assert.equal(await payloadHash(payload), await payloadHash(reorderedPayload));

  const retryResponse = await worker.fetch(
    new Request("https://reports.classin.cloud/v1/edb-reports", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(reorderedPayload),
    }),
    readyEnvironment(database),
  );
  const retryReceipt = await retryResponse.json();

  assert.equal(firstResponse.status, 201);
  assert.equal(retryResponse.status, 200);
  assert.equal(retryReceipt.reportId, firstReceipt.reportId);
  assert.equal(retryReceipt.duplicate, true);
  assert.equal(database.rows.length, 1);
  const laterPayload = { ...payload, submittedAt: "2026-08-24T04:00:00Z" };
  assert.notEqual(await payloadHash(payload), await payloadHash(laterPayload));
});


test("contact is rejected unless consent and value are both present", () => {
  const missingConsent = validPayload();
  missingConsent.reporter = { contact: "customer@example.com" };
  assert.equal(validateReport(missingConsent), "contact_consent_required");

  const missingContact = validPayload();
  missingContact.reporter = { consentToContact: true };
  assert.equal(validateReport(missingContact), "contact_required");
});


test("collector rejects short descriptions and oversized payloads", async () => {
  const short = validPayload();
  short.description = "짧음";
  const shortResponse = await worker.fetch(
    new Request("https://reports.classin.cloud/v1/edb-reports", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(short),
    }),
    readyEnvironment(),
  );
  assert.equal(shortResponse.status, 400);

  const oversizedResponse = await worker.fetch(
    new Request("https://reports.classin.cloud/v1/edb-reports", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "content-length": String(70 * 1024),
      },
      body: "{}",
    }),
    readyEnvironment(),
  );
  assert.equal(oversizedResponse.status, 413);

  const streamingOversizedResponse = await worker.fetch(
    new Request("https://reports.classin.cloud/v1/edb-reports", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ padding: "x".repeat(70 * 1024) }),
    }),
    readyEnvironment(),
  );
  assert.equal(streamingOversizedResponse.status, 413);
});


test("collector rejects malformed schema before touching D1", async () => {
  let deeplyNested = {};
  for (let depth = 0; depth < 20; depth += 1) deeplyNested = { nested: deeplyNested };
  const invalidPayloads = [
    { ...validPayload(), context: [] },
    { ...validPayload(), diagnostics: "Darwin" },
    { ...validPayload(), description: "x".repeat(4_001) },
    { ...validPayload(), reporter: { contact: "x".repeat(241), consentToContact: true } },
    { ...validPayload(), context: deeplyNested },
  ];
  for (const payload of invalidPayloads) {
    const database = new FakeD1();
    const response = await worker.fetch(
      new Request("https://reports.classin.cloud/v1/edb-reports", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      }),
      readyEnvironment(database),
    );
    assert.equal(response.status, 400);
    assert.equal(database.rows.length, 0);
  }
});


test("rate limiting rejects abusive bursts before D1 access", async () => {
  const database = new FakeD1();
  const rateLimiter = {
    async limit({ key }) {
      assert.equal(key, "edb-report:203.0.113.7");
      return { success: false };
    },
  };
  const response = await worker.fetch(
    new Request("https://reports.classin.cloud/v1/edb-reports", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "cf-connecting-ip": "203.0.113.7",
      },
      body: JSON.stringify(validPayload()),
    }),
    { REPORTS_DB: database, REPORT_RATE_LIMITER: rateLimiter },
  );
  assert.equal(response.status, 429);
  assert.equal(response.headers.get("retry-after"), "60");
  assert.equal(database.rows.length, 0);
});


test("missing or unavailable rate-limit binding fails closed before D1", async () => {
  for (const environment of [
    { REPORTS_DB: new FakeD1() },
    {
      REPORTS_DB: new FakeD1(),
      REPORT_RATE_LIMITER: { async limit() { throw new Error("limiter unavailable"); } },
    },
  ]) {
    const originalConsoleError = console.error;
    console.error = () => {};
    try {
      const response = await worker.fetch(
        new Request("https://reports.classin.cloud/v1/edb-reports", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(validPayload()),
        }),
        environment,
      );
      assert.equal(response.status, 503);
      assert.equal((await response.json()).error, "rate_limiter_unavailable");
      assert.equal(response.headers.get("retry-after"), "60");
      assert.equal(environment.REPORTS_DB.rows.length, 0);
    } finally {
      console.error = originalConsoleError;
    }
  }
});
