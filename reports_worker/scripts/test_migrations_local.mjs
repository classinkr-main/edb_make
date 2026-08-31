#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { configuredReportsDatabase, parseWranglerJson, unwrapD1Rows } from "./verify_deployment.mjs";
import { HEALTH_DATABASE_QUERY } from "../src/index.js";


const WORKER_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const WRANGLER_CONFIG_PATH = resolve(WORKER_ROOT, "wrangler.toml");
const WRANGLER_BIN = resolve(WORKER_ROOT, "node_modules", "wrangler", "bin", "wrangler.js");


function run(command, argumentsList, { capture = false } = {}) {
  const completed = spawnSync(command, argumentsList, {
    cwd: WORKER_ROOT,
    encoding: "utf8",
    maxBuffer: 4 * 1024 * 1024,
    stdio: capture ? "pipe" : "inherit",
  });
  if (completed.error || completed.status !== 0) {
    const detail = completed.error || completed.stderr || completed.stdout || `exit ${completed.status}`;
    throw new Error(`${command} ${argumentsList.join(" ")} failed: ${detail}`);
  }
  return completed.stdout || "";
}


function wrangler(argumentsList, options = {}) {
  return run(process.execPath, [WRANGLER_BIN, ...argumentsList], options);
}


function readHealthSchemaReady(databaseName, persistTo) {
  const payload = parseWranglerJson(wrangler([
    "d1", "execute", databaseName,
    "--local", "--persist-to", persistTo,
    "--command", HEALTH_DATABASE_QUERY,
    "--json",
  ], { capture: true }));
  return Number(unwrapD1Rows(payload)[0]?.schema_ready || 0);
}


function main() {
  const target = configuredReportsDatabase(readFileSync(WRANGLER_CONFIG_PATH, "utf8"));
  const temporaryRoot = mkdtempSync(join(tmpdir(), "classin-edb-reports-migrations-"));
  const phaseOneMigrations = join(temporaryRoot, "phase-one-migrations");
  const persistTo = join(temporaryRoot, "state");
  const phaseOneConfig = join(temporaryRoot, "wrangler.json");
  mkdirSync(phaseOneMigrations, { recursive: true });
  copyFileSync(
    resolve(WORKER_ROOT, "migrations", "0001_create_bug_reports.sql"),
    join(phaseOneMigrations, "0001_create_bug_reports.sql"),
  );
  writeFileSync(phaseOneConfig, JSON.stringify({
    name: "classin-edb-reports-migration-test",
    main: resolve(WORKER_ROOT, "src", "index.js"),
    compatibility_date: "2026-07-27",
    d1_databases: [{
      binding: "REPORTS_DB",
      database_name: target.databaseName,
      database_id: target.databaseId,
      migrations_dir: phaseOneMigrations,
    }],
  }, null, 2));

  try {
    wrangler([
      "d1", "migrations", "apply", target.databaseName,
      "--local", "--persist-to", persistTo, "--config", phaseOneConfig,
    ]);
    run(process.execPath, [
      "scripts/verify_deployment.mjs",
      "--local",
      "--phase", "pre-migration",
      "--persist-to", persistTo,
    ]);
    if (readHealthSchemaReady(target.databaseName, persistTo) !== 0) {
      throw new Error("v1 schema unexpectedly passed the v3 health readiness query");
    }
    wrangler([
      "d1", "execute", target.databaseName,
      "--local", "--persist-to", persistTo,
      "--command",
      "INSERT INTO bug_reports (id, created_at, app_id, app_version, platform, description) "
        + "VALUES ('migration-sentinel', '2026-08-24T00:00:00Z', 'ClassInEDBMVP', 'old', 'windows', 'sentinel')",
    ]);

    wrangler([
      "d1", "migrations", "apply", target.databaseName,
      "--local", "--persist-to", persistTo,
    ]);
    run(process.execPath, [
      "scripts/verify_deployment.mjs",
      "--local",
      "--phase", "schema",
      "--persist-to", persistTo,
    ]);
    if (readHealthSchemaReady(target.databaseName, persistTo) !== 1) {
      throw new Error("migrated schema did not pass the v3 health readiness query");
    }
    wrangler([
      "d1", "migrations", "apply", target.databaseName,
      "--local", "--persist-to", persistTo,
    ]);

    const sentinelPayload = parseWranglerJson(wrangler([
      "d1", "execute", target.databaseName,
      "--local", "--persist-to", persistTo,
      "--command", "SELECT COUNT(*) AS count FROM bug_reports WHERE id = 'migration-sentinel'",
      "--json",
    ], { capture: true }));
    const rows = unwrapD1Rows(sentinelPayload);
    if (Number(rows[0]?.count) !== 1) {
      throw new Error("migration chain did not preserve the existing bug report row");
    }
    console.log(
      `[reports-migrations] OK: ${basename(phaseOneConfig)} 0001 -> 0002/0003 -> no-op replay`,
    );
  } finally {
    rmSync(temporaryRoot, { recursive: true, force: true });
  }
}


main();
