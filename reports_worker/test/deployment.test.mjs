import assert from "node:assert/strict";
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { REPORT_CONTRACT } from "../src/index.js";
import {
  BASE_COLUMNS,
  DEDUPLICATION_COLUMNS,
  EXPECTED_MIGRATIONS,
  REPORTER_AND_RESOLUTION_COLUMNS,
  REQUIRED_COLUMNS,
  REQUIRED_UNIQUE_INDEXES,
  collectHealthErrors,
  collectMigrationFileErrors,
  collectPreMigrationErrors,
  collectSchemaErrors,
  configuredReportsDatabase,
  parseWranglerJson,
  unwrapD1Rows,
  verifyHealth,
} from "../scripts/verify_deployment.mjs";
import {
  assertConfiguredDatabase,
  extractBookmark,
  extractPreviousWorkerVersion,
  orchestrateDeployment,
} from "../scripts/deploy_remote.mjs";


const WORKER_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");


function validSchemaRows() {
  return [
    { kind: "table", name: "bug_reports" },
    { kind: "table", name: "d1_migrations" },
    ...REQUIRED_COLUMNS.map(name => ({
      kind: "column",
      name,
      is_unique: 0,
      is_partial: 0,
      indexed_column: null,
      index_sql: null,
    })),
    ...REQUIRED_UNIQUE_INDEXES.map(name => ({
      kind: "index",
      name,
      is_unique: 1,
      is_partial: 1,
      indexed_column: "payload_hash",
      index_sql: (
        "CREATE UNIQUE INDEX bug_reports_payload_hash_unique_idx "
        + "ON bug_reports(payload_hash) WHERE payload_hash IS NOT NULL"
      ),
    })),
  ];
}


function migrationStageRows(stage) {
  const columns = [
    ...(stage >= 1 ? BASE_COLUMNS : []),
    ...(stage >= 2 ? REPORTER_AND_RESOLUTION_COLUMNS : []),
    ...(stage >= 3 ? DEDUPLICATION_COLUMNS : []),
  ];
  const rows = [
    { kind: "table", name: "d1_migrations" },
    ...(stage >= 1 ? [{ kind: "table", name: "bug_reports" }] : []),
    ...columns.map(name => ({ kind: "column", name })),
    ...EXPECTED_MIGRATIONS.slice(0, stage).map(name => ({ kind: "migration", name })),
  ];
  if (stage >= 3) {
    rows.push(...validSchemaRows().filter(row => row.kind === "index"));
  }
  return rows;
}


function readyHealthPayload() {
  return {
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
  };
}


test("deployment verifier accepts the required D1 and health contracts", () => {
  assert.deepEqual(collectSchemaErrors(migrationStageRows(3)), []);
  assert.deepEqual(collectHealthErrors(readyHealthPayload()), []);
});


test("pre-migration verifier accepts every clean migration prefix", () => {
  assert.deepEqual(collectPreMigrationErrors([]), []);
  for (const stage of [0, 1, 2, 3]) {
    assert.deepEqual(collectPreMigrationErrors(migrationStageRows(stage)), [], `stage ${stage}`);
  }
});


test("pre-migration verifier blocks partial, untracked, and out-of-order states", () => {
  const partial = migrationStageRows(1);
  partial.push({ kind: "column", name: "reporter_contact" });
  assert.ok(collectPreMigrationErrors(partial).some(error => error.includes("untracked column")));

  const untracked = migrationStageRows(1).filter(row => row.name !== "d1_migrations");
  assert.ok(collectPreMigrationErrors(untracked).some(error => error.includes("without a d1_migrations")));

  const outOfOrder = migrationStageRows(2).filter(
    row => !(row.kind === "migration" && row.name === EXPECTED_MIGRATIONS[0]),
  );
  assert.ok(collectPreMigrationErrors(outOfOrder).some(error => error.includes("ordered prefix")));
});


test("deployment verifier catches contact, operation error, and dedupe migration gaps", () => {
  const rows = validSchemaRows().filter(row => ![
    "reporter_contact",
    "consent_to_contact",
    "error_code",
    "failed_operation",
    "payload_hash",
    "bug_reports_payload_hash_unique_idx",
  ].includes(row.name));
  const errors = collectSchemaErrors(rows);

  for (const expected of [
    "reporter_contact",
    "consent_to_contact",
    "error_code",
    "failed_operation",
    "payload_hash",
    "bug_reports_payload_hash_unique_idx",
  ]) {
    assert.ok(errors.some(error => error.includes(expected)), expected);
  }
});


test("deployment verifier rejects a stale Worker response contract", () => {
  const errors = collectHealthErrors({
    ok: true,
    ready: true,
    service: "classin-edb-reports",
  });
  assert.ok(errors.some(error => error.includes("reportContract")));
});


test("deployment verifier rejects a stale storage schema contract", () => {
  const payload = readyHealthPayload();
  payload.reportContract = { ...REPORT_CONTRACT, storageSchemaVersion: 2 };
  assert.ok(
    collectHealthErrors(payload).some(error => error.includes("storageSchemaVersion")),
  );
});


test("post-deploy health verification retries a stale propagated contract", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return new Response(JSON.stringify(calls === 1 ? {
      ok: true,
      ready: true,
      service: "classin-edb-reports",
    } : readyHealthPayload()), { status: 200 });
  };
  try {
    const payload = await verifyHealth("https://reports.classin.cloud/health", {
      attempts: 2,
      delayMs: 0,
      validate: collectHealthErrors,
    });
    assert.equal(payload.reportContract.storageSchemaVersion, 3);
    assert.equal(calls, 2);
  } finally {
    globalThis.fetch = originalFetch;
  }
});


test("deployment verifier rejects unavailable Worker bindings", () => {
  const payload = readyHealthPayload();
  payload.ok = false;
  payload.ready = false;
  payload.readiness.ready = false;
  payload.readiness.bindings.REPORT_RATE_LIMITER = false;

  const errors = collectHealthErrors(payload);

  assert.ok(errors.some(error => error.includes("deployment-ready")));
  assert.ok(errors.some(error => error.includes("REPORT_RATE_LIMITER")));
});


test("deployment verifier checks payload hash index column and partial SQL", () => {
  const wrongColumnRows = validSchemaRows().map(row => (
    row.name === "bug_reports_payload_hash_unique_idx"
      ? { ...row, indexed_column: "description" }
      : row
  ));
  const missingWhereRows = validSchemaRows().map(row => (
    row.name === "bug_reports_payload_hash_unique_idx"
      ? {
          ...row,
          is_partial: 0,
          index_sql: "CREATE UNIQUE INDEX bug_reports_payload_hash_unique_idx ON bug_reports(payload_hash)",
        }
      : row
  ));
  const extraPredicateRows = validSchemaRows().map(row => (
    row.name === "bug_reports_payload_hash_unique_idx"
      ? {
          ...row,
          index_sql: (
            "CREATE UNIQUE INDEX bug_reports_payload_hash_unique_idx "
            + "ON bug_reports(payload_hash) WHERE payload_hash IS NOT NULL AND description != ''"
          ),
        }
      : row
  ));

  assert.ok(collectSchemaErrors(wrongColumnRows).some(error => error.includes("only payload_hash")));
  const missingWhereErrors = collectSchemaErrors(missingWhereRows);
  assert.ok(missingWhereErrors.some(error => error.includes("partial index")));
  assert.ok(missingWhereErrors.some(error => error.includes("partial predicate")));
  assert.ok(collectSchemaErrors(extraPredicateRows).some(error => error.includes("partial predicate")));
});


test("deployment verifier derives the target from the REPORTS_DB Wrangler binding", () => {
  const config = `
[[d1_databases]]
binding = "REPORTS_DB"
database_name = "configured-reports-db"
database_id = "11111111-2222-3333-4444-555555555555"
`;
  assert.deepEqual(configuredReportsDatabase(config), {
    binding: "REPORTS_DB",
    databaseName: "configured-reports-db",
    databaseId: "11111111-2222-3333-4444-555555555555",
  });
  assert.throws(
    () => configuredReportsDatabase(config.replace("REPORTS_DB", "WRONG_BINDING")),
    /exactly one REPORTS_DB/,
  );
  const actualTarget = configuredReportsDatabase(
    readFileSync(resolve(WORKER_ROOT, "wrangler.toml"), "utf8"),
  );
  assert.equal(actualTarget.binding, "REPORTS_DB");
  assert.ok(actualTarget.databaseName);
  assert.ok(actualTarget.databaseId);
});


test("deduplication migration adds the hash column and partial unique index", () => {
  const migration = readFileSync(
    resolve(WORKER_ROOT, "migrations/0003_add_payload_deduplication.sql"),
    "utf8",
  );
  assert.match(migration, /ADD COLUMN payload_hash TEXT/);
  assert.match(migration, /CREATE UNIQUE INDEX IF NOT EXISTS bug_reports_payload_hash_unique_idx/);
  assert.match(migration, /WHERE payload_hash IS NOT NULL/);
});


test("deployment verifier unwraps Wrangler JSON without executing mutations", () => {
  const payload = [{ success: true, results: validSchemaRows() }];
  const parsed = parseWranglerJson(`wrangler informational banner\n${JSON.stringify(payload)}`);
  assert.deepEqual(unwrapD1Rows(parsed), validSchemaRows());
});


test("migration manifest pins immutable SQL contents", () => {
  assert.deepEqual(collectMigrationFileErrors(), []);
  const temporaryRoot = mkdtempSync(resolve(tmpdir(), "reports-migration-manifest-"));
  const migrationsDir = resolve(temporaryRoot, "migrations");
  const manifestPath = resolve(migrationsDir, "manifest.json");
  mkdirSync(migrationsDir);
  try {
    for (const name of EXPECTED_MIGRATIONS) {
      copyFileSync(resolve(WORKER_ROOT, "migrations", name), resolve(migrationsDir, name));
    }
    copyFileSync(resolve(WORKER_ROOT, "migrations", "manifest.json"), manifestPath);
    writeFileSync(
      resolve(migrationsDir, EXPECTED_MIGRATIONS[1]),
      `${readFileSync(resolve(migrationsDir, EXPECTED_MIGRATIONS[1]), "utf8")}\n-- changed`,
    );
    assert.ok(
      collectMigrationFileErrors({ migrationsDir, manifestPath })
        .some(error => error.includes("was modified")),
    );
  } finally {
    rmSync(temporaryRoot, { recursive: true, force: true });
  }
});


test("migration manifest rejects missing, malformed, and version-mismatched hashes", () => {
  const temporaryRoot = mkdtempSync(resolve(tmpdir(), "reports-migration-manifest-shape-"));
  const migrationsDir = resolve(temporaryRoot, "migrations");
  const manifestPath = resolve(migrationsDir, "manifest.json");
  mkdirSync(migrationsDir);
  try {
    for (const name of EXPECTED_MIGRATIONS) {
      copyFileSync(resolve(WORKER_ROOT, "migrations", name), resolve(migrationsDir, name));
    }
    const original = JSON.parse(readFileSync(resolve(WORKER_ROOT, "migrations/manifest.json"), "utf8"));

    for (const badHash of [undefined, "", "not-a-sha256", "A".repeat(64)]) {
      const changed = structuredClone(original);
      if (badHash === undefined) delete changed.migrations[0].sha256;
      else changed.migrations[0].sha256 = badHash;
      writeFileSync(manifestPath, JSON.stringify(changed));
      assert.ok(
        collectMigrationFileErrors({ migrationsDir, manifestPath })
          .some(error => error.includes("lowercase 64-character SHA-256")),
      );
    }

    const wrongVersion = structuredClone(original);
    wrongVersion.version = 2;
    writeFileSync(manifestPath, JSON.stringify(wrongVersion));
    assert.ok(
      collectMigrationFileErrors({ migrationsDir, manifestPath })
        .some(error => error.includes("manifest version")),
    );
  } finally {
    rmSync(temporaryRoot, { recursive: true, force: true });
  }
});


test("final schema verification requires the exact migration ledger", () => {
  const missingLedgerEntry = migrationStageRows(3).filter(
    row => !(row.kind === "migration" && row.name === EXPECTED_MIGRATIONS[2]),
  );
  assert.ok(
    collectSchemaErrors(missingLedgerEntry)
      .some(error => error.includes("final migration ledger")),
  );
});


test("health verification aborts a hanging request at the per-attempt timeout", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(options.signal.reason), { once: true });
  });
  try {
    await assert.rejects(
      verifyHealth("https://reports.classin.cloud/health", {
        attempts: 1,
        timeoutMs: 5,
      }),
      /timed out after 5ms/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});


test("remote deploy helpers pin the configured D1 and exact current rollback target", () => {
  const target = {
    databaseId: "11111111-2222-3333-4444-555555555555",
    databaseName: "configured-reports-db",
  };
  assert.doesNotThrow(() => assertConfiguredDatabase({
    uuid: target.databaseId,
    name: target.databaseName,
  }, target));
  assert.throws(() => assertConfiguredDatabase({
    uuid: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    name: target.databaseName,
  }, target), /unexpected database UUID/);
  assert.equal(extractBookmark({ result: { bookmark: "bookmark-123" } }), "bookmark-123");
  assert.throws(() => extractBookmark({}), /recovery bookmark/);
  assert.equal(extractPreviousWorkerVersion([
    {
      created_on: "2026-08-20T00:00:00.000Z",
      versions: [{ version_id: "oldest", percentage: 100 }],
    },
    {
      created_on: "2026-08-24T00:00:00.000Z",
      versions: [
        { version_id: "current-canary", percentage: 10 },
        { version_id: "current-stable", percentage: 90 },
      ],
    },
  ]), "current-stable");
  assert.equal(extractPreviousWorkerVersion([
    { versions: [{ version_id: "old-without-date", percentage: 100 }] },
    { versions: [{ version_id: "current-without-date", percentage: 100 }] },
  ]), "current-without-date");
  assert.throws(() => extractPreviousWorkerVersion([]), /currently deployed Worker version/);
});


test("remote deploy orchestration preserves every guarded command and argument in order", async () => {
  const target = {
    databaseId: "11111111-2222-3333-4444-555555555555",
    databaseName: "configured-reports-db",
  };
  const events = [];
  const wranglerCommand = (argumentsList, options = {}) => {
    events.push({ type: "wrangler", argumentsList, options });
    if (argumentsList[0] === "d1" && argumentsList[1] === "info") {
      return JSON.stringify({ uuid: target.databaseId, name: target.databaseName });
    }
    if (argumentsList[0] === "d1" && argumentsList[1] === "time-travel") {
      return JSON.stringify({ bookmark: "bookmark-before-migration" });
    }
    if (argumentsList[0] === "deployments") {
      return JSON.stringify([{
        created_on: "2026-08-24T00:00:00.000Z",
        versions: [{ version_id: "current-worker-version", percentage: 100 }],
      }]);
    }
    return "";
  };

  await orchestrateDeployment({
    target,
    wranglerCommand,
    verifyPhase: phase => events.push({ type: "verify", phase }),
    log: () => {},
    reportRecovery: () => assert.fail("successful dry orchestration must not request recovery"),
  });

  assert.deepEqual(events, [
    { type: "verify", phase: "pre-migration" },
    {
      type: "wrangler",
      argumentsList: ["d1", "info", target.databaseName, "--json"],
      options: { capture: true },
    },
    {
      type: "wrangler",
      argumentsList: ["d1", "time-travel", "info", target.databaseName, "--json"],
      options: { capture: true },
    },
    {
      type: "wrangler",
      argumentsList: ["deployments", "list", "--name", "classin-edb-reports", "--json"],
      options: { capture: true },
    },
    {
      type: "wrangler",
      argumentsList: ["d1", "migrations", "apply", target.databaseName, "--remote"],
      options: {},
    },
    { type: "verify", phase: "schema" },
    {
      type: "wrangler",
      argumentsList: [
        "deploy",
        "--strict",
        "--message",
        "reports storage schema v3",
      ],
      options: {},
    },
    { type: "verify", phase: "post" },
  ]);
});
