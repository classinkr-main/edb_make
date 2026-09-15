"""Web trial server (Vercel Python function).

Plan 1 only exposes a health check and a token-guarded spike endpoint that
measures the parser on Vercel. Plan 2 replaces the spike with /api/parse.
"""

from __future__ import annotations

import time

IMPORT_STARTED_AT = time.perf_counter()

import hmac
import os
import resource
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from problem_parser import PdfUnreadableError, inspect_pdf, parse_problems, parser_version

SPIKE_MAX_BYTES = 4_000_000
SPIKE_MAX_PAGES = 20

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_instance_started_at = time.time()
_request_count = 0
_import_ms = int(round((time.perf_counter() - IMPORT_STARTED_AT) * 1000))


def _max_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes, macOS reports bytes.
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(usage / divisor, 1)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "commit": parser_version()}


@app.post("/api/spike")
async def spike(request: Request) -> JSONResponse:
    global _request_count
    expected = os.environ.get("TRIAL_SPIKE_TOKEN", "")
    provided = request.headers.get("x-spike-token", "")
    if not expected or not hmac.compare_digest(provided.encode(), expected.encode()):
        return JSONResponse({"error": "not_found"}, status_code=404)

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > SPIKE_MAX_BYTES:
            return JSONResponse({"error": "too_large"}, status_code=413)

    _request_count += 1
    request_index = _request_count
    with tempfile.TemporaryDirectory(prefix="trial-spike-") as temp_dir:
        source = Path(temp_dir) / "input.pdf"
        source.write_bytes(bytes(body))
        try:
            info = inspect_pdf(source, max_pages=SPIKE_MAX_PAGES)
        except PdfUnreadableError:
            return JSONResponse({"error": "bad_pdf"}, status_code=415)
        parse_started_at = time.perf_counter()
        result = await run_in_threadpool(parse_problems, source, work_dir=Path(temp_dir) / "work")
        parse_ms = int(round((time.perf_counter() - parse_started_at) * 1000))

    return JSONResponse(
        {
            "commit": result.parser_version,
            "bytes": len(body),
            "page_count": info.page_count,
            "pages_without_text": info.pages_without_text,
            "problem_numbers": [problem.number for problem in result.problems],
            "import_ms": _import_ms,
            "parse_ms": parse_ms,
            "timing_ms": result.timing_ms,
            "max_rss_mb": _max_rss_mb(),
            "instance_age_s": round(time.time() - _instance_started_at, 1),
            "request_index": request_index,
            "python": sys.version.split()[0],
        }
    )
