#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import {
  configuredReportsDatabase,
  parseWranglerJson,
} from "./verify_deployment.mjs";


const WORKER_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const WRANGLER_CONFIG_PATH = resolve(WORKER_ROOT, "wrangler.toml");
const WRANGLER_BIN = resolve(WORKER_ROOT, "node_modules", "wrangler", "bin", "wrangler.js");
const NODE = process.execPath;


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
  return run(NODE, [WRANGLER_BIN, ...argumentsList], options);
}


function findObjectValue(value, key) {
  if (!value || typeof value !== "object") return undefined;
  if (Object.hasOwn(value, key)) return value[key];
  for (const child of Array.isArray(value) ? value : Object.values(value)) {
    const found = findObjectValue(child, key);
    if (found !== undefined) return found;
  }
  return undefined;
}


function extractBookmark(payload) {
  const bookmark = findObjectValue(payload, "bookmark");
  if (typeof bookmark !== "string" || !bookmark.trim()) {
    throw new Error("D1 Time Travel did not return a recovery bookmark");
  }
  return bookmark.trim();
}


function assertConfiguredDatabase(payload, target) {
  // Wrangler 4.125.0 intentionally removes the legacy `version` field from
  // `d1 info --json`. Verify the immutable database UUID instead; the
  // subsequent Time Travel bookmark call is the capability check we need.
  const databaseId = findObjectValue(payload, "uuid") || findObjectValue(payload, "database_id");
  const databaseName = findObjectValue(payload, "name") || findObjectValue(payload, "database_name");
  if (databaseId !== target.databaseId) {
    throw new Error(
      `D1 info returned an unexpected database UUID; expected ${target.databaseId}, found ${databaseId || "unknown"}`,
    );
  }
  if (databaseName && databaseName !== target.databaseName) {
    throw new Error(
      `D1 info returned an unexpected database name; expected ${target.databaseName}, found ${databaseName}`,
    );
  }
}


function extractPreviousWorkerVersion(payload) {
  const deployments = Array.isArray(payload) ? payload : payload?.deployments;
  if (!Array.isArray(deployments) || !deployments.length) {
    throw new Error("could not determine the currently deployed Worker version for rollback");
  }
  // Wrangler 4.125.0 emits deployments in created_on ascending order. Sort
  // explicitly when timestamps are present and otherwise preserve that
  // contract by selecting the final entry, never the oldest entry.
  const datedDeployments = deployments.filter(deployment => (
    Number.isFinite(Date.parse(String(deployment?.created_on || "")))
  ));
  const current = datedDeployments.length
    ? [...datedDeployments].sort((left, right) => (
      Date.parse(String(right.created_on)) - Date.parse(String(left.created_on))
    ))[0]
    : deployments[deployments.length - 1];
  const versions = current?.versions;
  if (!Array.isArray(versions) || !versions.length) {
    throw new Error("could not determine the currently deployed Worker version for rollback");
  }
  const selected = [...versions].sort(
    (left, right) => Number(right?.percentage || 0) - Number(left?.percentage || 0),
  )[0];
  const version = selected?.version_id || selected?.versionId || selected?.id;
  if (typeof version !== "string" || !version.trim()) {
    throw new Error("current Worker deployment did not contain a rollback version ID");
  }
  return version.trim();
}


function verify(phase) {
  run(NODE, ["scripts/verify_deployment.mjs", "--remote", "--phase", phase]);
}


function printRecoveryPlan({ databaseName, bookmark, previousWorkerVersion }) {
  console.error("[reports-deployment] Deployment did not complete its post-deploy contract check.");
  if (previousWorkerVersion) {
    console.error(
      `[reports-deployment] Worker rollback: npx wrangler rollback ${previousWorkerVersion} `
      + '--message "rollback failed reports deployment"',
    );
  }
  if (bookmark) {
    console.error(
      `[reports-deployment] D1 restore (destructive; review accepted reports first): `
      + `npx wrangler d1 time-travel restore ${databaseName} --bookmark=${bookmark}`,
    );
  }
}


async function orchestrateDeployment({
  target,
  migrateOnly = false,
  wranglerCommand = wrangler,
  verifyPhase = verify,
  parseJson = parseWranglerJson,
  log = console.log,
  reportRecovery = printRecoveryPlan,
}) {
  let bookmark = "";
  let previousWorkerVersion = "";
  let d1MutationAttempted = false;

  try {
    verifyPhase("pre-migration");
    const databaseInfo = parseJson(wranglerCommand(
      ["d1", "info", target.databaseName, "--json"],
      { capture: true },
    ));
    assertConfiguredDatabase(databaseInfo, target);
    bookmark = extractBookmark(parseJson(wranglerCommand(
      ["d1", "time-travel", "info", target.databaseName, "--json"],
      { capture: true },
    )));
    log(`[reports-deployment] Pre-migration D1 bookmark: ${bookmark}`);

    if (!migrateOnly) {
      previousWorkerVersion = extractPreviousWorkerVersion(parseJson(wranglerCommand(
        ["deployments", "list", "--name", "classin-edb-reports", "--json"],
        { capture: true },
      )));
      log(`[reports-deployment] Previous Worker version: ${previousWorkerVersion}`);
    }

    d1MutationAttempted = true;
    wranglerCommand(["d1", "migrations", "apply", target.databaseName, "--remote"]);
    verifyPhase("schema");
    if (migrateOnly) {
      log("[reports-deployment] D1 migration completed; Worker was not deployed.");
      return;
    }

    wranglerCommand([
      "deploy",
      "--strict",
      "--message",
      `reports storage schema v${REPORT_STORAGE_SCHEMA_VERSION}`,
    ]);
    verifyPhase("post");
    log("[reports-deployment] Remote migration, Worker deploy, and health contract verified.");
  } catch (error) {
    reportRecovery({
      databaseName: target.databaseName,
      bookmark: d1MutationAttempted ? bookmark : "",
      previousWorkerVersion,
    });
    throw error;
  }
}


async function main(argv = process.argv.slice(2)) {
  const unknown = argv.filter(argument => argument !== "--migrate-only");
  if (unknown.length) throw new Error(`unknown argument: ${unknown[0]}`);
  const target = configuredReportsDatabase(readFileSync(WRANGLER_CONFIG_PATH, "utf8"));
  await orchestrateDeployment({
    target,
    migrateOnly: argv.includes("--migrate-only"),
  });
}


const REPORT_STORAGE_SCHEMA_VERSION = 3;


if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    console.error(`[reports-deployment] ERROR: ${error.message}`);
    process.exitCode = 1;
  });
}


export {
  assertConfiguredDatabase,
  extractBookmark,
  extractPreviousWorkerVersion,
  orchestrateDeployment,
};
