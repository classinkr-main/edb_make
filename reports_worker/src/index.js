const MAX_BODY_BYTES = 64 * 1024;
const MAX_DESCRIPTION_CHARS = 4_000;
const MAX_CONTACT_CHARS = 240;
const MAX_CONTEXT_JSON_CHARS = 24_000;
const MAX_DIAGNOSTICS_JSON_CHARS = 24_000;
const MAX_JSON_DEPTH = 12;
const MAX_JSON_NODES = 2_000;
const ALLOWED_APP_IDS = new Set(["ClassInEDBMVP"]);
const HEALTH_REQUIRED_COLUMNS = Object.freeze([
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
  "reporter_contact",
  "consent_to_contact",
  "error_code",
  "failed_operation",
  "resolution_note",
  "resolved_at",
  "payload_hash",
]);
const HEALTH_DATABASE_QUERY = [
  "SELECT CASE WHEN",
  `(SELECT COUNT(DISTINCT name) FROM pragma_table_info('bug_reports') WHERE name IN (${(
    HEALTH_REQUIRED_COLUMNS.map(name => `'${name}'`).join(", ")
  )})) = ${HEALTH_REQUIRED_COLUMNS.length}`,
  "AND EXISTS (",
  "SELECT 1 FROM pragma_index_list('bug_reports')",
  "WHERE name = 'bug_reports_payload_hash_unique_idx' AND \"unique\" = 1 AND partial = 1",
  ")",
  "AND (SELECT group_concat(name, ',') FROM (",
  "SELECT name FROM pragma_index_info('bug_reports_payload_hash_unique_idx') ORDER BY seqno",
  ")) = 'payload_hash'",
  "THEN 1 ELSE 0 END AS schema_ready",
].join(" ");
const HEALTH_RATE_LIMIT_KEY = "edb-report-health-readiness";
const REPORT_CONTRACT = Object.freeze({
  reportSchemaVersion: 1,
  receiptSchemaVersion: 1,
  storageSchemaVersion: 3,
  contactAccepted: true,
  operationErrorFields: ["error_code", "failed_operation"],
  idempotency: "payload_sha256",
  requiredBindings: ["REPORTS_DB", "REPORT_RATE_LIMITER"],
  requiredMigrations: [
    "0002_add_reporter_and_resolution.sql",
    "0003_add_payload_deduplication.sql",
  ],
});

function json(payload, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
      "referrer-policy": "no-referrer",
      "x-content-type-options": "nosniff",
      "x-frame-options": "DENY",
      ...extraHeaders,
    },
  });
}

function text(value, status = 200) {
  return new Response(value, {
    status,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "no-store",
      "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
      "referrer-policy": "no-referrer",
      "x-content-type-options": "nosniff",
      "x-frame-options": "DENY",
    },
  });
}

function cleanText(value, maxChars) {
  return String(value ?? "").trim().slice(0, maxChars);
}

function boundedJson(value, maxChars, fallback = "{}") {
  try {
    const serialized = JSON.stringify(value ?? {});
    return serialized.length <= maxChars ? serialized : fallback;
  } catch {
    return fallback;
  }
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function hasSafeJsonComplexity(value) {
  const pending = [{ value, depth: 0 }];
  let nodes = 0;
  while (pending.length) {
    const current = pending.pop();
    nodes += 1;
    if (nodes > MAX_JSON_NODES || current.depth > MAX_JSON_DEPTH) return false;
    if (Array.isArray(current.value)) {
      for (const child of current.value) pending.push({ value: child, depth: current.depth + 1 });
    } else if (isPlainObject(current.value)) {
      for (const child of Object.values(current.value)) {
        pending.push({ value: child, depth: current.depth + 1 });
      }
    }
  }
  return true;
}

function canonicalJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map(item => canonicalJson(item)).join(",")}]`;
  }
  if (isPlainObject(value)) {
    return `{${Object.keys(value).sort().map(key => (
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`
    )).join(",")}}`;
  }
  return JSON.stringify(value);
}

async function payloadHash(payload) {
  const bytes = new TextEncoder().encode(canonicalJson(payload));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

function reportId(now = new Date()) {
  const date = now.toISOString().slice(0, 10).replaceAll("-", "");
  const bytes = new Uint8Array(5);
  crypto.getRandomValues(bytes);
  const suffix = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  return `EDB-${date}-${suffix.toUpperCase()}`;
}

async function readJson(request) {
  const contentType = request.headers.get("content-type") || "";
  if (!/^application\/json(?:\s*;.*)?$/i.test(contentType.trim())) {
    return { error: json({ ok: false, error: "content_type_required" }, 415) };
  }
  const rawLength = request.headers.get("content-length");
  if (rawLength != null) {
    if (!/^\d+$/.test(rawLength.trim())) {
      return { error: json({ ok: false, error: "invalid_content_length" }, 400) };
    }
    if (Number(rawLength) > MAX_BODY_BYTES) {
      return { error: json({ ok: false, error: "payload_too_large" }, 413) };
    }
  }
  const reader = request.body?.getReader();
  const chunks = [];
  let totalBytes = 0;
  if (reader) {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      totalBytes += value.byteLength;
      if (totalBytes > MAX_BODY_BYTES) {
        await reader.cancel();
        return { error: json({ ok: false, error: "payload_too_large" }, 413) };
      }
      chunks.push(value);
    }
  }
  const body = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let raw;
  try {
    raw = new TextDecoder("utf-8", { fatal: true }).decode(body);
  } catch {
    return { error: json({ ok: false, error: "invalid_encoding" }, 400) };
  }
  try {
    return { value: JSON.parse(raw) };
  } catch {
    return { error: json({ ok: false, error: "invalid_json" }, 400) };
  }
}

function validateReport(payload) {
  if (!isPlainObject(payload)) {
    return "object_required";
  }
  if (payload.schemaVersion !== 1) {
    return "unsupported_schema";
  }
  if (!isPlainObject(payload.app)) {
    return "invalid_app";
  }
  for (const field of ["id", "version", "platform"]) {
    if (typeof payload.app[field] !== "string" || !payload.app[field].trim()) {
      return `invalid_app_${field}`;
    }
    if (payload.app[field].length > 80) {
      return `app_${field}_too_long`;
    }
  }
  const appId = cleanText(payload.app.id, 80);
  if (!ALLOWED_APP_IDS.has(appId)) {
    return "unknown_app";
  }
  if (typeof payload.description !== "string") {
    return "description_required";
  }
  if (payload.description.length > MAX_DESCRIPTION_CHARS) {
    return "description_too_long";
  }
  const description = payload.description.trim();
  if (description.length < 5) {
    return "description_too_short";
  }
  if (
    !isPlainObject(payload.context)
    || !hasSafeJsonComplexity(payload.context)
    || boundedJson(payload.context, MAX_CONTEXT_JSON_CHARS, "").length === 0
  ) {
    return "invalid_context";
  }
  if (
    payload.diagnostics != null
    && (!isPlainObject(payload.diagnostics)
      || !hasSafeJsonComplexity(payload.diagnostics)
      || boundedJson(payload.diagnostics, MAX_DIAGNOSTICS_JSON_CHARS, "").length === 0)
  ) {
    return "invalid_diagnostics";
  }
  if (payload.category != null && payload.category !== "bug") {
    return "invalid_category";
  }
  if (payload.submittedAt != null) {
    if (typeof payload.submittedAt !== "string" || !/^\d{4}-\d{2}-\d{2}T/.test(payload.submittedAt)) {
      return "invalid_submitted_at";
    }
    if (!Number.isFinite(Date.parse(payload.submittedAt))) {
      return "invalid_submitted_at";
    }
  }
  if (payload.reporter != null && !isPlainObject(payload.reporter)) {
    return "invalid_reporter";
  }
  if (payload.reporter?.contact != null && typeof payload.reporter.contact !== "string") {
    return "invalid_contact";
  }
  if (String(payload.reporter?.contact ?? "").length > MAX_CONTACT_CHARS) {
    return "contact_too_long";
  }
  const contact = cleanText(payload.reporter?.contact, MAX_CONTACT_CHARS);
  const consentToContact = payload.reporter?.consentToContact === true;
  if (contact && !consentToContact) {
    return "contact_consent_required";
  }
  if (consentToContact && !contact) {
    return "contact_required";
  }
  const operationError = payload.context.lastOperationError;
  if (operationError != null) {
    if (!isPlainObject(operationError)) {
      return "invalid_operation_error";
    }
    for (const [field, maxChars] of [["code", 120], ["operation", 80]]) {
      if (operationError[field] != null) {
        if (typeof operationError[field] !== "string" || operationError[field].length > maxChars) {
          return `invalid_operation_${field}`;
        }
      }
    }
  }
  return "";
}

async function existingReport(database, hash) {
  return database.prepare(
    `SELECT id, created_at, consent_to_contact
       FROM bug_reports
      WHERE payload_hash = ?
      LIMIT 1`
  ).bind(hash).first();
}

function receiptForStoredReport(row) {
  return {
    ok: true,
    reportId: row.id,
    receivedAt: row.created_at,
    contactAccepted: Boolean(Number(row.consent_to_contact || 0)),
    duplicate: true,
  };
}

async function createReport(request, env) {
  const parsed = await readJson(request);
  if (parsed.error) return parsed.error;
  const validationError = validateReport(parsed.value);
  if (validationError) {
    return json({ ok: false, error: validationError }, 400);
  }

  const payload = parsed.value;
  const hash = await payloadHash(payload);
  const duplicate = await existingReport(env.REPORTS_DB, hash);
  if (duplicate) {
    return json(receiptForStoredReport(duplicate), 200);
  }
  const createdAt = new Date().toISOString();
  const id = reportId(new Date(createdAt));
  const reporterContact = cleanText(payload.reporter?.contact, MAX_CONTACT_CHARS);
  const consentToContact = reporterContact && payload.reporter?.consentToContact === true;
  const operationError = payload.context?.lastOperationError;
  const statement = env.REPORTS_DB.prepare(
    `INSERT INTO bug_reports (
      id, created_at, app_id, app_version, platform, category,
      description, context_json, diagnostics_json, status,
      reporter_contact, consent_to_contact, error_code, failed_operation,
      payload_hash
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?)`
  ).bind(
    id,
    createdAt,
    cleanText(payload.app.id, 80),
    cleanText(payload.app.version, 80) || "unknown",
    cleanText(payload.app.platform, 40) || "unknown",
    cleanText(payload.category, 40) || "bug",
    cleanText(payload.description, MAX_DESCRIPTION_CHARS),
    boundedJson(payload.context, MAX_CONTEXT_JSON_CHARS),
    payload.diagnostics == null ? null : boundedJson(payload.diagnostics, MAX_DIAGNOSTICS_JSON_CHARS),
    reporterContact || null,
    consentToContact ? 1 : 0,
    cleanText(operationError?.code, 120) || null,
    cleanText(operationError?.operation, 80) || null,
    hash,
  );
  try {
    await statement.run();
  } catch (error) {
    // The unique payload hash closes the race between the read above and two
    // concurrent inserts.  Return the winning receipt instead of creating a
    // second report or surfacing a retryable storage error.
    const racedDuplicate = await existingReport(env.REPORTS_DB, hash);
    if (racedDuplicate) {
      return json(receiptForStoredReport(racedDuplicate), 200);
    }
    throw error;
  }
  return json({
    ok: true,
    reportId: id,
    receivedAt: createdAt,
    contactAccepted: Boolean(reporterContact && consentToContact),
    duplicate: false,
  }, 201);
}

async function enforceRateLimit(request, env) {
  if (!env.REPORT_RATE_LIMITER || typeof env.REPORT_RATE_LIMITER.limit !== "function") {
    return json(
      { ok: false, error: "rate_limiter_unavailable" },
      503,
      { "retry-after": "60" },
    );
  }
  const clientAddress = request.headers.get("cf-connecting-ip") || "unknown-client";
  try {
    const result = await env.REPORT_RATE_LIMITER.limit({ key: `edb-report:${clientAddress}` });
    if (!result || typeof result.success !== "boolean") {
      return json(
        { ok: false, error: "rate_limiter_unavailable" },
        503,
        { "retry-after": "60" },
      );
    }
    if (result.success === false) {
      return json(
        { ok: false, error: "rate_limited" },
        429,
        { "retry-after": "60" },
      );
    }
  } catch (error) {
    console.error("bug report rate limiter unavailable", error);
    return json(
      { ok: false, error: "rate_limiter_unavailable" },
      503,
      { "retry-after": "60" },
    );
  }
  return null;
}

async function bindingReadiness(env) {
  const bindings = {
    REPORTS_DB: false,
    REPORT_RATE_LIMITER: false,
  };
  try {
    const statement = env?.REPORTS_DB?.prepare?.(HEALTH_DATABASE_QUERY);
    if (statement && typeof statement.first === "function") {
      const row = await statement.first();
      bindings.REPORTS_DB = Number(row?.schema_ready) === 1;
    }
  } catch (error) {
    console.error("bug report health D1 probe failed", error);
  }
  try {
    if (typeof env?.REPORT_RATE_LIMITER?.limit === "function") {
      const result = await env.REPORT_RATE_LIMITER.limit({ key: HEALTH_RATE_LIMIT_KEY });
      // A limited health key still proves that the configured binding is live;
      // only malformed responses or thrown calls indicate readiness failure.
      bindings.REPORT_RATE_LIMITER = Boolean(result && typeof result.success === "boolean");
    }
  } catch (error) {
    console.error("bug report health rate limiter probe failed", error);
  }
  return {
    ready: Object.values(bindings).every(Boolean),
    bindings,
  };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      const readiness = await bindingReadiness(env);
      return json({
        ok: readiness.ready,
        ready: readiness.ready,
        service: "classin-edb-reports",
        readiness,
        reportContract: REPORT_CONTRACT,
      }, readiness.ready ? 200 : 503);
    }
    if (request.method === "GET" && url.pathname === "/") {
      return text("ClassIn EDB report collector");
    }
    if (request.method === "POST" && url.pathname === "/v1/edb-reports") {
      if (!env.REPORTS_DB || typeof env.REPORTS_DB.prepare !== "function") {
        return json({ ok: false, error: "storage_unavailable" }, 503);
      }
      try {
        const rateLimited = await enforceRateLimit(request, env);
        if (rateLimited) return rateLimited;
        return await createReport(request, env);
      } catch (error) {
        console.error("bug report storage failed", error);
        return json({ ok: false, error: "storage_failed" }, 500);
      }
    }
    return json({ ok: false, error: "not_found" }, 404);
  },
};

export {
  HEALTH_DATABASE_QUERY,
  MAX_BODY_BYTES,
  REPORT_CONTRACT,
  bindingReadiness,
  createReport,
  payloadHash,
  reportId,
  validateReport,
};
