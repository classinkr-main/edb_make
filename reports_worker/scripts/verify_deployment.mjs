#!/usr/bin/env node
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { REPORT_CONTRACT } from "../src/index.js";


const DEFAULT_HEALTH_URL = "https://reports.classin.cloud/health";
const WORKER_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const WRANGLER_CONFIG_PATH = resolve(WORKER_ROOT, "wrangler.toml");
const WRANGLER_BIN = resolve(WORKER_ROOT, "node_modules", "wrangler", "bin", "wrangler.js");
const MIGRATIONS_DIR = resolve(WORKER_ROOT, "migrations");
const MIGRATION_MANIFEST_PATH = resolve(MIGRATIONS_DIR, "manifest.json");
const MIGRATION_MANIFEST_VERSION = 1;
const HEALTH_ATTEMPT_TIMEOUT_MS = 5_000;
const BASE_COLUMNS = Object.freeze([
  "id",
  "created_at",
  "app_id",
  "app_version",
  "platform",
  "category",
  "description",
  "context_json",
  "diagnostics_json",
  "status",
]);
const REPORTER_AND_RESOLUTION_COLUMNS = Object.freeze([
  "reporter_contact",
  "consent_to_contact",
  "error_code",
  "failed_operation",
  "resolution_note",
  "resolved_at",
]);
const DEDUPLICATION_COLUMNS = Object.freeze(["payload_hash"]);
const REQUIRED_COLUMNS = Object.freeze([
  ...BASE_COLUMNS,
  ...REPORTER_AND_RESOLUTION_COLUMNS,
  ...DEDUPLICATION_COLUMNS,
]);
const EXPECTED_MIGRATIONS = Object.freeze([
  "0001_create_bug_reports.sql",
  "0002_add_reporter_and_resolution.sql",
  "0003_add_payload_deduplication.sql",
]);
const REQUIRED_UNIQUE_INDEXES = Object.freeze([
  "bug_reports_payload_hash_unique_idx",
]);
const SCHEMA_QUERY = [
  "SELECT 'table' AS kind, name, NULL AS is_unique, NULL AS is_partial,",
  "NULL AS indexed_column, sql AS index_sql",
  "FROM sqlite_master",
  "WHERE type = 'table' AND name IN ('bug_reports', 'd1_migrations')",
  "UNION ALL",
  "SELECT 'column' AS kind, name, 0 AS is_unique, 0 AS is_partial,",
  "NULL AS indexed_column, NULL AS index_sql",
  "FROM pragma_table_info('bug_reports')",
  "UNION ALL",
  "SELECT 'index' AS kind, indexes.name, indexes.\"unique\" AS is_unique,",
  "indexes.partial AS is_partial, index_info.name AS indexed_column, schema.sql AS index_sql",
  "FROM pragma_index_list('bug_reports') AS indexes",
  "LEFT JOIN pragma_index_info(indexes.name) AS index_info ON TRUE",
  "LEFT JOIN sqlite_master AS schema ON schema.type = 'index' AND schema.name = indexes.name",
  "ORDER BY kind, name",
].join(" ");
const MIGRATION_QUERY = [
  "SELECT 'migration' AS kind, name, NULL AS is_unique, NULL AS is_partial,",
  "NULL AS indexed_column, NULL AS index_sql",
  "FROM d1_migrations ORDER BY id",
].join(" ");


function unwrapD1Rows(payload) {
  const results = Array.isArray(payload) ? payload : [payload];
  const rows = [];
  for (const result of results) {
    if (!result || result.success === false) {
      throw new Error(`D1 read-only schema query failed: ${JSON.stringify(result)}`);
    }
    if (Array.isArray(result.results)) rows.push(...result.results);
  }
  return rows;
}


function rowNames(rows, kind) {
  return rows.filter(row => row?.kind === kind).map(row => String(row.name || ""));
}


function normalizeIndexSql(value) {
  return String(value || "")
    .replace(/["`\[\]]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}


function collectPayloadIndexErrors(rows, { required }) {
  const index = REQUIRED_UNIQUE_INDEXES[0];
  const indexRows = rows.filter(row => row?.kind === "index" && row.name === index);
  if (!indexRows.length) {
    return required ? [`D1 bug_reports is missing unique index: ${index}`] : [];
  }
  const errors = [];
  if (!required) {
    errors.push(`D1 ${index} exists before its migration is recorded`);
    return errors;
  }
  if (!indexRows.some(row => Number(row.is_unique) === 1)) {
    errors.push(`D1 bug_reports is missing unique index: ${index}`);
    return errors;
  }
  const indexedColumns = new Set(
    indexRows.map(row => String(row.indexed_column || "").trim()).filter(Boolean),
  );
  if (indexedColumns.size !== 1 || !indexedColumns.has("payload_hash")) {
    errors.push(`D1 ${index} must index only payload_hash`);
  }
  if (!indexRows.some(row => Number(row.is_partial) === 1)) {
    errors.push(`D1 ${index} must be a partial index`);
  }
  const sql = normalizeIndexSql(indexRows.find(row => row.index_sql)?.index_sql);
  const requiredSql = new RegExp(
    "^CREATE\\s+UNIQUE\\s+INDEX\\s+(?:IF\\s+NOT\\s+EXISTS\\s+)?"
    + "bug_reports_payload_hash_unique_idx\\s+ON\\s+bug_reports\\s*"
    + "\\(\\s*payload_hash\\s*\\)\\s+WHERE\\s+payload_hash\\s+IS\\s+NOT\\s+NULL\\s*;?$",
    "i",
  );
  if (!requiredSql.test(sql)) {
    errors.push(
      `D1 ${index} SQL must exactly define the payload_hash IS NOT NULL partial predicate`,
    );
  }
  return errors;
}


function collectPhysicalSchemaErrors(rows, stage) {
  if (!Array.isArray(rows)) return ["D1 schema query did not return rows"];
  const tables = new Set(rowNames(rows, "table"));
  const columns = new Set(rowNames(rows, "column"));
  const expectedColumns = [
    ...(stage >= 1 ? BASE_COLUMNS : []),
    ...(stage >= 2 ? REPORTER_AND_RESOLUTION_COLUMNS : []),
    ...(stage >= 3 ? DEDUPLICATION_COLUMNS : []),
  ];
  const errors = [];

  if (stage === 0) {
    if (tables.has("bug_reports") || columns.size) {
      errors.push("D1 bug_reports exists without a recorded 0001 migration");
    }
    errors.push(...collectPayloadIndexErrors(rows, { required: false }));
    return errors;
  }
  if (!tables.has("bug_reports")) {
    return ["D1 migration history exists but bug_reports table is missing"];
  }
  for (const column of expectedColumns) {
    if (!columns.has(column)) {
      errors.push(`D1 bug_reports is missing column for recorded migration stage ${stage}: ${column}`);
    }
  }
  for (const column of columns) {
    if (!expectedColumns.includes(column)) {
      errors.push(`D1 bug_reports has an untracked column at migration stage ${stage}: ${column}`);
    }
  }
  errors.push(...collectPayloadIndexErrors(rows, { required: stage >= 3 }));
  return errors;
}


function collectSchemaErrors(rows) {
  if (!Array.isArray(rows)) return ["D1 schema query did not return rows"];
  const errors = collectPhysicalSchemaErrors(rows, EXPECTED_MIGRATIONS.length);
  const tables = new Set(rowNames(rows, "table"));
  const migrations = rowNames(rows, "migration");
  if (!tables.has("d1_migrations")) {
    errors.push("D1 final schema is missing the d1_migrations ledger");
  }
  if (JSON.stringify(migrations) !== JSON.stringify(EXPECTED_MIGRATIONS)) {
    errors.push("D1 final migration ledger does not exactly match the checked-in migrations");
  }
  return [...new Set(errors)];
}


function collectPreMigrationErrors(rows) {
  if (!Array.isArray(rows)) return ["D1 schema query did not return rows"];
  const tables = new Set(rowNames(rows, "table"));
  const migrations = rowNames(rows, "migration");
  const errors = [];
  if (!tables.has("d1_migrations")) {
    if (tables.has("bug_reports")) {
      errors.push("D1 bug_reports exists without a d1_migrations ledger; do not auto-repair it");
    }
    errors.push(...collectPhysicalSchemaErrors(rows, 0));
    return [...new Set(errors)];
  }
  if (new Set(migrations).size !== migrations.length) {
    errors.push("D1 migration ledger contains duplicate migration names");
  }
  const unknown = migrations.filter(name => !EXPECTED_MIGRATIONS.includes(name));
  for (const name of unknown) {
    errors.push(`D1 migration ledger contains an unknown migration: ${name}`);
  }
  const expectedPrefix = EXPECTED_MIGRATIONS.slice(0, migrations.length);
  if (JSON.stringify(migrations) !== JSON.stringify(expectedPrefix)) {
    errors.push("D1 migration ledger is not an ordered prefix of the checked-in migrations");
  }
  if (!errors.length) {
    errors.push(...collectPhysicalSchemaErrors(rows, migrations.length));
  }
  return errors;
}


function collectHealthErrors(payload) {
  const errors = [];
  if (!payload || payload.service !== "classin-edb-reports") {
    return ["Worker health response does not identify classin-edb-reports"];
  }
  if (payload.ok !== true || payload.ready !== true || payload.readiness?.ready !== true) {
    errors.push("Worker health response is not deployment-ready");
  }
  for (const binding of REPORT_CONTRACT.requiredBindings) {
    if (payload.readiness?.bindings?.[binding] !== true) {
      errors.push(`Worker health response reports unavailable binding: ${binding}`);
    }
  }
  const contract = payload.reportContract;
  if (!contract || typeof contract !== "object" || Array.isArray(contract)) {
    return ["Worker health response is missing reportContract"];
  }
  for (const field of [
    "reportSchemaVersion",
    "receiptSchemaVersion",
    "storageSchemaVersion",
    "contactAccepted",
    "idempotency",
  ]) {
    if (contract[field] !== REPORT_CONTRACT[field]) {
      errors.push(`Worker reportContract.${field} mismatch`);
    }
  }
  for (const field of ["operationErrorFields", "requiredMigrations", "requiredBindings"]) {
    if (JSON.stringify(contract[field]) !== JSON.stringify(REPORT_CONTRACT[field])) {
      errors.push(`Worker reportContract.${field} mismatch`);
    }
  }
  return errors;
}


function parseWranglerJson(stdout) {
  const text = String(stdout || "").trim();
  try {
    return JSON.parse(text);
  } catch {
    const candidates = [text.indexOf("["), text.indexOf("{")]
      .filter(index => index >= 0)
      .sort((a, b) => a - b);
    for (const index of candidates) {
      try {
        return JSON.parse(text.slice(index));
      } catch {
        // Try the next JSON-looking section.
      }
    }
    throw new Error(`Wrangler did not return JSON: ${text.slice(-1_000)}`);
  }
}


function configuredReportsDatabase(configText) {
  const sections = String(configText || "")
    .split(/^\[\[d1_databases\]\]\s*$/m)
    .slice(1)
    .map(section => section.split(/^\[\[/m, 1)[0]);
  const values = sections.map(section => {
    const readString = key => {
      const match = section.match(new RegExp(`^\\s*${key}\\s*=\\s*["']([^"']+)["']\\s*$`, "m"));
      return match?.[1]?.trim() || "";
    };
    return {
      binding: readString("binding"),
      databaseName: readString("database_name"),
      databaseId: readString("database_id"),
    };
  });
  const targets = values.filter(value => value.binding === "REPORTS_DB");
  if (targets.length !== 1) {
    throw new Error("wrangler.toml must define exactly one REPORTS_DB D1 binding");
  }
  const target = targets[0];
  if (!target.databaseName || !target.databaseId) {
    throw new Error("REPORTS_DB must define database_name and database_id in wrangler.toml");
  }
  return target;
}


function collectMigrationFileErrors({ migrationsDir = MIGRATIONS_DIR, manifestPath = MIGRATION_MANIFEST_PATH } = {}) {
  const errors = [];
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  } catch (error) {
    return [`migration manifest cannot be read: ${error.message}`];
  }
  const files = readdirSync(migrationsDir).filter(name => name.endsWith(".sql")).sort();
  const manifestEntries = Array.isArray(manifest?.migrations) ? manifest.migrations : [];
  const manifestNames = manifestEntries.map(item => item?.name);
  if (manifest?.version !== MIGRATION_MANIFEST_VERSION) {
    errors.push(`migration manifest version must be exactly ${MIGRATION_MANIFEST_VERSION}`);
  }
  if (JSON.stringify(files) !== JSON.stringify(EXPECTED_MIGRATIONS)) {
    errors.push("checked-in SQL migrations do not exactly match the expected ordered set");
  }
  if (JSON.stringify(manifestNames) !== JSON.stringify(EXPECTED_MIGRATIONS)) {
    errors.push("migration manifest does not exactly match the expected ordered set");
  }
  for (const [index, expectedName] of EXPECTED_MIGRATIONS.entries()) {
    const entry = manifestEntries[index];
    if (!entry || entry.name !== expectedName) continue;
    if (typeof entry.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(entry.sha256)) {
      errors.push(`migration manifest requires a lowercase 64-character SHA-256: ${expectedName}`);
      continue;
    }
    try {
      const text = readFileSync(resolve(migrationsDir, expectedName), "utf8").replace(/\r\n/g, "\n");
      const actual = createHash("sha256").update(text).digest("hex");
      if (actual !== entry.sha256) {
        errors.push(`applied migration file was modified: ${expectedName}`);
      }
    } catch (error) {
      errors.push(`migration file cannot be read: ${expectedName}: ${error.message}`);
    }
  }
  return errors;
}


function runWranglerJson(argumentsList) {
  const completed = spawnSync(process.execPath, [WRANGLER_BIN, ...argumentsList], {
    cwd: WORKER_ROOT,
    encoding: "utf8",
    maxBuffer: 2 * 1024 * 1024,
  });
  if (completed.error || completed.status !== 0) {
    throw new Error(
      `Wrangler D1 read-only verification failed: ${completed.error || completed.stderr || completed.stdout}`,
    );
  }
  return parseWranglerJson(completed.stdout);
}


function querySchema(mode, { persistTo = "" } = {}) {
  const target = configuredReportsDatabase(readFileSync(WRANGLER_CONFIG_PATH, "utf8"));
  const baseArguments = [
    "d1",
    "execute",
    target.databaseName,
    mode,
    "--command",
    SCHEMA_QUERY,
    "--json",
  ];
  if (persistTo) baseArguments.push("--persist-to", persistTo);
  const rows = unwrapD1Rows(runWranglerJson(baseArguments));
  if (rowNames(rows, "table").includes("d1_migrations")) {
    const migrationArguments = [
      "d1",
      "execute",
      target.databaseName,
      mode,
      "--command",
      MIGRATION_QUERY,
      "--json",
    ];
    if (persistTo) migrationArguments.push("--persist-to", persistTo);
    rows.push(...unwrapD1Rows(runWranglerJson(migrationArguments)));
  }
  return rows;
}


async function verifyHealth(
  endpoint,
  {
    attempts = 1,
    delayMs = 5_000,
    timeoutMs = HEALTH_ATTEMPT_TIMEOUT_MS,
    validate = () => [],
  } = {},
) {
  const url = new URL(endpoint);
  if (
    url.protocol !== "https:"
    && !(url.protocol === "http:" && ["localhost", "127.0.0.1", "::1"].includes(url.hostname))
  ) {
    throw new Error("health endpoint must use HTTPS or loopback HTTP");
  }
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    const controller = new AbortController();
    const timeout = setTimeout(() => {
      controller.abort(new Error(`Worker health check timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    try {
      const response = await fetch(url, {
        method: "GET",
        headers: { accept: "application/json", "cache-control": "no-cache" },
        redirect: "error",
        signal: controller.signal,
      });
      const body = await response.text();
      if (!response.ok) {
        throw new Error(`Worker health check returned HTTP ${response.status}: ${body.slice(0, 500)}`);
      }
      const payload = JSON.parse(body);
      const contractErrors = validate(payload);
      if (contractErrors.length) {
        throw new Error(contractErrors.join("\n"));
      }
      return payload;
    } catch (error) {
      lastError = controller.signal.aborted
        ? controller.signal.reason || new Error(`Worker health check timed out after ${timeoutMs}ms`)
        : error;
      if (attempt < attempts) {
        await new Promise(resolveDelay => setTimeout(resolveDelay, delayMs));
      }
    } finally {
      clearTimeout(timeout);
    }
  }
  throw lastError;
}


function parseArgs(argv) {
  const mode = argv.includes("--remote") ? "--remote" : argv.includes("--local") ? "--local" : "";
  if (!mode || (argv.includes("--remote") && argv.includes("--local"))) {
    throw new Error("choose exactly one of --remote or --local");
  }
  const phaseIndex = argv.indexOf("--phase");
  const phase = phaseIndex >= 0 ? argv[phaseIndex + 1] : "post";
  if (!["pre-migration", "schema", "post"].includes(phase)) {
    throw new Error("--phase must be pre-migration, schema, or post");
  }
  const endpointIndex = argv.indexOf("--endpoint");
  const endpoint = endpointIndex >= 0 ? argv[endpointIndex + 1] : DEFAULT_HEALTH_URL;
  if (!endpoint) throw new Error("--endpoint requires a value");
  const persistIndex = argv.indexOf("--persist-to");
  const persistTo = persistIndex >= 0 ? argv[persistIndex + 1] : "";
  if (persistIndex >= 0 && !persistTo) throw new Error("--persist-to requires a value");
  if (persistTo && mode !== "--local") throw new Error("--persist-to is only valid with --local");
  return { mode, phase, endpoint, persistTo };
}


async function verifyDeployment({ mode, phase, endpoint = DEFAULT_HEALTH_URL, persistTo = "" }) {
  const errors = collectMigrationFileErrors();
  const rows = querySchema(mode, { persistTo });
  errors.push(...(
    phase === "pre-migration"
      ? collectPreMigrationErrors(rows)
      : collectSchemaErrors(rows)
  ));
  if (phase === "post") {
    await verifyHealth(endpoint, {
      attempts: 7,
      delayMs: 5_000,
      validate: collectHealthErrors,
    });
  }
  if (errors.length) throw new Error(errors.join("\n"));
  return { rows };
}


async function main(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  await verifyDeployment(options);
  console.log(`[reports-deployment] OK: ${options.mode.slice(2)} ${options.phase} verification`);
}


if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    console.error(`[reports-deployment] ERROR: ${error.message}`);
    process.exitCode = 1;
  });
}


export {
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
  parseArgs,
  parseWranglerJson,
  unwrapD1Rows,
  verifyDeployment,
  verifyHealth,
};
