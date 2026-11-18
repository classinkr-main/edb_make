from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
import os
from typing import Any


GEMINI_TOKEN_FIELDS = {
    "promptTokenCount": "prompt_token_count",
    "candidatesTokenCount": "candidates_token_count",
    "thoughtsTokenCount": "thoughts_token_count",
    "cachedContentTokenCount": "cached_content_token_count",
    "toolUsePromptTokenCount": "tool_use_prompt_token_count",
    "totalTokenCount": "total_token_count",
}

AI_PRICING_VERSION = "2026-07-27"
DEFAULT_USD_KRW_RATE = Decimal("1400")

# USD per one million tokens. Values are deliberately versioned and embedded
# so historical run summaries do not silently change when a provider updates
# its public price sheet.
MODEL_TOKEN_PRICING_USD_PER_MILLION: dict[str, dict[str, Decimal]] = {
    "gemini-3.5-flash-lite": {
        "input": Decimal("0.30"),
        "cached_input": Decimal("0.03"),
        "output": Decimal("2.50"),
    },
    "gemini-3.5-flash": {
        "input": Decimal("1.50"),
        "cached_input": Decimal("0.15"),
        "output": Decimal("9.00"),
    },
    "gemini-3.6-flash": {
        "input": Decimal("1.50"),
        "cached_input": Decimal("0.15"),
        "output": Decimal("7.50"),
    },
    "gemini-3.7-flash": {
        "input": Decimal("1.50"),
        "cached_input": Decimal("0.15"),
        "output": Decimal("7.50"),
    },
    "gemini-3.8-flash": {
        "input": Decimal("1.50"),
        "cached_input": Decimal("0.15"),
        "output": Decimal("7.50"),
    },
    "gemini-3.1-pro-preview": {
        "input": Decimal("2.00"),
        "cached_input": Decimal("0.20"),
        "output": Decimal("12.00"),
    },
    "gemini-2.5-pro": {
        "input": Decimal("1.25"),
        "cached_input": Decimal("0.125"),
        "output": Decimal("10.00"),
    },
}

# USD for one generated image. Only actively supported sizes are listed.
MODEL_IMAGE_PRICING_USD: dict[str, dict[str, Decimal]] = {
    "gemini-3.1-flash-image-preview": {
        "512": Decimal("0.045"),
        "1K": Decimal("0.067"),
        "2K": Decimal("0.101"),
        "4K": Decimal("0.151"),
    },
    "gemini-3.1-flash-image": {
        "512": Decimal("0.045"),
        "1K": Decimal("0.067"),
        "2K": Decimal("0.101"),
        "4K": Decimal("0.151"),
    },
}


def _nonnegative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def normalize_gemini_token_usage(response: Mapping[str, Any] | None) -> dict[str, int]:
    """Extract provider-reported token counts from a Gemini response.

    The response may contain extra per-modality details. The stable aggregate
    counters are enough for per-run cost reporting and avoid copying request or
    response content into diagnostic artifacts.
    """
    if not isinstance(response, Mapping):
        return {}
    raw_usage = response.get("usageMetadata")
    if not isinstance(raw_usage, Mapping):
        return {}

    usage = {
        output_name: _nonnegative_int(raw_usage.get(provider_name))
        for provider_name, output_name in GEMINI_TOKEN_FIELDS.items()
    }
    usage["request_count"] = 1
    return usage


def aggregate_token_usage(
    events: Iterable[Mapping[str, Any] | None],
) -> dict[str, int]:
    totals = {
        "request_count": 0,
        **{output_name: 0 for output_name in GEMINI_TOKEN_FIELDS.values()},
    }
    for event in events:
        if not isinstance(event, Mapping):
            continue
        for field in totals:
            totals[field] += _nonnegative_int(event.get(field))
    return totals


def summarize_token_efficiency(
    events: Iterable[Mapping[str, Any] | None],
) -> dict[str, Any]:
    """Summarize token mix without retaining prompts or model responses."""
    rows = [event for event in events if isinstance(event, Mapping)]

    def _summarize(rows_for_bucket: list[Mapping[str, Any]]) -> dict[str, Any]:
        request_count = sum(
            max(1, _nonnegative_int(event.get("request_count")))
            for event in rows_for_bucket
        )
        prompt_tokens = sum(
            _nonnegative_int(event.get("prompt_token_count"))
            for event in rows_for_bucket
        )
        candidate_tokens = sum(
            _nonnegative_int(event.get("candidates_token_count"))
            for event in rows_for_bucket
        )
        thought_tokens = sum(
            _nonnegative_int(event.get("thoughts_token_count"))
            for event in rows_for_bucket
        )
        cached_tokens = sum(
            min(
                _nonnegative_int(event.get("prompt_token_count")),
                _nonnegative_int(event.get("cached_content_token_count")),
            )
            for event in rows_for_bucket
        )
        total_tokens = sum(
            _nonnegative_int(event.get("total_token_count"))
            for event in rows_for_bucket
        )
        if total_tokens <= 0:
            total_tokens = prompt_tokens + candidate_tokens + thought_tokens
        generated_tokens = candidate_tokens + thought_tokens
        return {
            "request_count": request_count,
            "prompt_token_count": prompt_tokens,
            "candidate_token_count": candidate_tokens,
            "thought_token_count": thought_tokens,
            "cached_prompt_token_count": cached_tokens,
            "generated_token_count": generated_tokens,
            "total_token_count": total_tokens,
            "avg_total_tokens_per_request": (
                round(total_tokens / request_count, 2) if request_count else 0.0
            ),
            "cache_hit_token_ratio": (
                round(cached_tokens / prompt_tokens, 4) if prompt_tokens else 0.0
            ),
            "thought_share_of_generated": (
                round(thought_tokens / generated_tokens, 4)
                if generated_tokens
                else 0.0
            ),
        }

    by_stage_rows: dict[str, list[Mapping[str, Any]]] = {}
    by_model_rows: dict[str, list[Mapping[str, Any]]] = {}
    for event in rows:
        stage = str(event.get("stage") or "unknown")
        model = _normalized_model(event.get("model")) or "unknown"
        by_stage_rows.setdefault(stage, []).append(event)
        by_model_rows.setdefault(model, []).append(event)

    return {
        **_summarize(rows),
        "by_stage": {
            key: _summarize(bucket)
            for key, bucket in sorted(by_stage_rows.items())
        },
        "by_model": {
            key: _summarize(bucket)
            for key, bucket in sorted(by_model_rows.items())
        },
    }


def _normalized_model(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalized_image_size(value: Any) -> str:
    normalized = str(value or "1K").strip().upper().replace("PX", "")
    if normalized in {"1024", "AUTO"}:
        return "1K"
    if normalized == "2048":
        return "2K"
    if normalized == "4096":
        return "4K"
    return normalized


def _usd_krw_rate(value: Any = None) -> Decimal:
    raw = value if value is not None else os.environ.get("EDB_USD_KRW_RATE")
    try:
        rate = Decimal(str(raw)) if raw not in {None, ""} else DEFAULT_USD_KRW_RATE
    except Exception:
        rate = DEFAULT_USD_KRW_RATE
    return rate if rate > 0 else DEFAULT_USD_KRW_RATE


def estimate_ai_event_cost(event: Mapping[str, Any] | None) -> dict[str, Any]:
    """Estimate one provider event without retaining prompt or response data."""
    if not isinstance(event, Mapping):
        return {"priced": False, "estimated_usd": 0.0, "reason": "invalid_event"}

    model = _normalized_model(event.get("model"))
    stage = str(event.get("stage") or "unknown")
    token_pricing = MODEL_TOKEN_PRICING_USD_PER_MILLION.get(model)
    image_pricing = MODEL_IMAGE_PRICING_USD.get(model)
    token_cost = Decimal("0")
    image_cost = Decimal("0")
    priced_components: list[str] = []

    if token_pricing:
        prompt_tokens = _nonnegative_int(event.get("prompt_token_count"))
        cached_tokens = min(prompt_tokens, _nonnegative_int(event.get("cached_content_token_count")))
        uncached_tokens = max(0, prompt_tokens - cached_tokens)
        output_tokens = (
            _nonnegative_int(event.get("candidates_token_count"))
            + _nonnegative_int(event.get("thoughts_token_count"))
        )
        if prompt_tokens or output_tokens:
            token_cost = (
                Decimal(uncached_tokens) * token_pricing["input"]
                + Decimal(cached_tokens) * token_pricing["cached_input"]
                + Decimal(output_tokens) * token_pricing["output"]
            ) / Decimal("1000000")
            priced_components.append("tokens")

    image_count = _nonnegative_int(event.get("image_output_count"))
    image_size = _normalized_image_size(event.get("image_size"))
    if image_count and image_pricing and image_size in image_pricing:
        image_cost = Decimal(image_count) * image_pricing[image_size]
        priced_components.append("images")

    estimated_usd = token_cost + image_cost
    return {
        "priced": bool(priced_components),
        "model": model or "unknown",
        "stage": stage,
        "estimated_usd": round(float(estimated_usd), 8),
        "token_cost_usd": round(float(token_cost), 8),
        "image_cost_usd": round(float(image_cost), 8),
        "priced_components": priced_components,
        "image_size": image_size if image_count else None,
    }


def summarize_ai_cost(
    events: Iterable[Mapping[str, Any] | None],
    *,
    usd_krw_rate: Any = None,
) -> dict[str, Any]:
    rows = [event for event in events if isinstance(event, Mapping)]
    total_usd = Decimal("0")
    priced_requests = 0
    unpriced_requests = 0
    by_model: dict[str, dict[str, Any]] = {}
    by_stage: dict[str, dict[str, Any]] = {}

    for event in rows:
        estimate = estimate_ai_event_cost(event)
        usd = Decimal(str(estimate["estimated_usd"]))
        total_usd += usd
        request_count = max(1, _nonnegative_int(event.get("request_count")))
        if estimate["priced"]:
            priced_requests += request_count
        else:
            unpriced_requests += request_count
        for bucket, key in (
            (by_model, str(estimate.get("model") or "unknown")),
            (by_stage, str(estimate.get("stage") or "unknown")),
        ):
            entry = bucket.setdefault(
                key,
                {"request_count": 0, "estimated_usd": Decimal("0")},
            )
            entry["request_count"] += request_count
            entry["estimated_usd"] += usd

    rate = _usd_krw_rate(usd_krw_rate)

    def _serialize_buckets(bucket: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            key: {
                "request_count": int(value["request_count"]),
                "estimated_usd": round(float(value["estimated_usd"]), 8),
                "estimated_krw": round(float(value["estimated_usd"] * rate), 2),
            }
            for key, value in sorted(bucket.items())
        }

    return {
        "pricing_version": AI_PRICING_VERSION,
        "estimate_only": True,
        "currency": "USD",
        "usd_krw_rate": float(rate),
        "event_count": len(rows),
        "priced_request_count": priced_requests,
        "unpriced_request_count": unpriced_requests,
        "estimated_usd": round(float(total_usd), 8),
        "estimated_krw": round(float(total_usd * rate), 2),
        "by_model": _serialize_buckets(by_model),
        "by_stage": _serialize_buckets(by_stage),
    }


def image_generation_usage_event(
    *,
    provider: str,
    model: str,
    usage: Mapping[str, Any] | None,
    image_size: str,
    stage: str = "image_reconstruction",
) -> dict[str, Any]:
    """Normalize image-generation metadata into the common cost event shape."""
    raw = usage if isinstance(usage, Mapping) else {}
    event = {
        "provider": str(provider or ""),
        "model": str(model or ""),
        "stage": stage,
        "request_count": 1,
        "prompt_token_count": _nonnegative_int(
            raw.get("prompt_token_count")
            or raw.get("promptTokenCount")
            or raw.get("input_tokens")
        ),
        "candidates_token_count": _nonnegative_int(
            raw.get("candidates_token_count")
            or raw.get("candidatesTokenCount")
            or raw.get("output_tokens")
        ),
        "thoughts_token_count": _nonnegative_int(
            raw.get("thoughts_token_count") or raw.get("thoughtsTokenCount")
        ),
        "cached_content_token_count": _nonnegative_int(
            raw.get("cached_content_token_count")
            or raw.get("cachedContentTokenCount")
            or raw.get("cached_input_tokens")
        ),
        "image_output_count": 1,
        "image_size": _normalized_image_size(image_size),
    }
    return event


def token_usage_has_requests(usage: Mapping[str, Any] | None) -> bool:
    return bool(isinstance(usage, Mapping) and _nonnegative_int(usage.get("request_count")) > 0)
