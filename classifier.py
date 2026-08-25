"""LLM-based sentiment and category classification for reviews.

Fallback chain: OpenRouter (Claude Haiku) → Groq (gpt-oss-20b)

Usage:
    # Normal mode (called by pipeline.py — classifies new unclassified reviews)
    from classifier import classify_pending

    # Backfill mode (run manually)
    python classifier.py --backfill
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any, Optional

import requests

import config
import supabase_client

logger = logging.getLogger(__name__)

# ── Prompt ────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a product review classifier for an Amazon ecommerce analytics system.

Classify each review into exactly one sentiment and one primary category.

Sentiment values: positive, negative, neutral

Categories (pick the DOMINANT topic):
- Product Quality: performance, durability, defects, materials, safety
- Usability & Design: setup, ergonomics, aesthetics, sizing, fit
- Value: price vs. quality, worth it, overpriced, bargain
- Customer Service: support interactions, warranty, returns, response time
- Fulfillment: shipping speed, packaging damage, missing parts, wrong item
- Other: general praise/complaints that don't fit any category above

Rules:
- If category confidence < 0.60, use "Other"
- Return ONLY valid JSON — no markdown, no explanation

Return a JSON array:
[
  {
    "review_id": <integer>,
    "sentiment": "positive|negative|neutral",
    "sentiment_confidence": <0.0-1.0>,
    "review_category": "<category name>",
    "category_confidence": <0.0-1.0>
  }
]"""


def _build_user_message(batch: list[dict[str, Any]]) -> str:
    lines = ["Classify these reviews:"]
    for r in batch:
        title = (r.get("title") or "").strip()
        body = (r.get("review_text") or "").strip()
        combined = f"{title}. {body}".strip(". ") or "(no text)"
        lines.append(f'\n[review_id: {r["id"]}]\n{combined[:600]}')
    return "\n".join(lines)


# Models — single source of truth so logs/errors name exactly what was called.
OPENROUTER_MODEL = "anthropic/claude-haiku-4.5"
GROQ_MODEL = "openai/gpt-oss-20b"


# ── Provider implementations ──────────────────────────────────────────────────

def _short_error(exc: Exception) -> str:
    """Stable, log-friendly reason used for aggregation (e.g. '401 Unauthorized')."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        return f"{resp.status_code} {(resp.reason or '').strip()}".strip()
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "connection error"
    return type(exc).__name__


def _error_body(exc: Exception) -> str:
    """Extra detail from a provider's error response body, if any (for logs only)."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return ""
    try:
        data = resp.json()
        detail = (data.get("error") or {}).get("message") or data.get("message") or ""
    except Exception:
        detail = resp.text or ""
    return detail.strip()[:200]


def _call_openrouter(messages: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """Returns (content, error_reason). On success error_reason is None."""
    if not config.OPENROUTER_API_KEY:
        return None, "OPENROUTER_API_KEY not configured"
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 1024,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"], None
    except Exception as exc:
        reason = _short_error(exc)
        body = _error_body(exc)
        logger.warning("OpenRouter (%s) failed: %s%s", OPENROUTER_MODEL, reason,
                       f" — {body}" if body else "")
        return None, reason


def _call_groq(messages: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """Returns (content, error_reason). On success error_reason is None."""
    if not config.GROQ_API_KEY:
        return None, "GROQ_API_KEY not configured"
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config.GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 1024,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"], None
    except Exception as exc:
        reason = _short_error(exc)
        body = _error_body(exc)
        logger.warning("Groq (%s) failed: %s%s", GROQ_MODEL, reason,
                       f" — {body}" if body else "")
        return None, reason


# ── LLM dispatch ──────────────────────────────────────────────────────────────

def _llm_classify(
    batch: list[dict[str, Any]],
) -> tuple[Optional[list[dict[str, Any]]], dict[str, str]]:
    """Send batch to LLM, trying each provider in order.

    Returns (parsed_results, provider_errors). On success provider_errors is empty;
    on failure it maps each provider label → the reason it failed, so callers can
    report exactly what went wrong (e.g. {"OpenRouter": "401 Unauthorized"}).
    """
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(batch)},
    ]

    provider_errors: dict[str, str] = {}

    for label, provider_fn in (("OpenRouter", _call_openrouter), ("Groq", _call_groq)):
        raw, error = provider_fn(messages)
        if raw is None:
            provider_errors[label] = error or "unknown error"
            continue
        try:
            # Strip markdown code fences if present
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            parsed = json.loads(text.strip())
            if isinstance(parsed, list):
                # Return any earlier-provider failures too: if the primary is down
                # but a fallback saved the batch, we still want that surfaced as an
                # early warning rather than hidden behind the success.
                return parsed, provider_errors
            provider_errors[label] = "response was not a JSON array"
        except (json.JSONDecodeError, IndexError, KeyError) as exc:
            provider_errors[label] = f"unparseable response ({type(exc).__name__})"
            logger.warning("%s returned an unparseable response: %s\nRaw: %s",
                           label, exc, raw[:200])
            continue

    logger.error(
        "All LLM providers failed for this batch — %s",
        ", ".join(f"{k}: {v}" for k, v in provider_errors.items()),
    )
    return None, provider_errors


# ── Batch processing ──────────────────────────────────────────────────────────

def _process_batch(batch: list[dict[str, Any]]) -> tuple[int, dict[str, str]]:
    """Classify a single batch and write results to Supabase.

    Returns (rows_updated, provider_errors). provider_errors is empty on success.
    """
    results, provider_errors = _llm_classify(batch)
    if not results:
        return 0, provider_errors

    id_map = {r["id"]: r for r in batch}
    updates: list[dict[str, Any]] = []

    for r in results:
        rid = r.get("review_id")
        if rid not in id_map:
            continue

        sentiment = r.get("sentiment", "neutral").lower()
        if sentiment not in config.SENTIMENTS:
            sentiment = "neutral"

        category = r.get("review_category", "Other")
        if category not in config.REVIEW_CATEGORIES:
            category = "Other"

        s_conf = float(r.get("sentiment_confidence", 0.5))
        c_conf = float(r.get("category_confidence", 0.5))

        # Downgrade low-confidence categories to Other
        if c_conf < config.CLASSIFY_MIN_CONFIDENCE:
            category = "Other"

        updates.append(
            {
                "id": rid,
                "sentiment": sentiment,
                "sentiment_confidence": round(s_conf, 4),
                "review_category": category,
                "category_confidence": round(c_conf, 4),
            }
        )

    # Pass through any provider failures even on success (a fallback may have covered them).
    return supabase_client.bulk_update_classifications(updates), provider_errors


def classify_pending(max_reviews: int = 500) -> dict[str, Any]:
    """Classify up to `max_reviews` unclassified reviews.

    Called by pipeline.py after each scrape run.
    Returns stats: {
        'found': int,              # reviews needing classification
        'classified': int,         # successfully classified
        'failed': int,             # found - classified
        'provider_errors': dict,   # "Provider: reason" → count, aggregated across batches
    }
    """
    reviews = supabase_client.get_unclassified_review_ids(limit=max_reviews)
    if not reviews:
        logger.info("No unclassified reviews found")
        return {"found": 0, "classified": 0, "failed": 0, "provider_errors": {}}

    logger.info("Classifying %d reviews…", len(reviews))
    classified = 0
    error_counts: dict[str, int] = {}

    for i in range(0, len(reviews), config.CLASSIFY_BATCH_SIZE):
        batch = reviews[i: i + config.CLASSIFY_BATCH_SIZE]
        n, provider_errors = _process_batch(batch)
        classified += n
        for label, reason in provider_errors.items():
            key = f"{label}: {reason}"
            error_counts[key] = error_counts.get(key, 0) + 1
        logger.info("Batch %d/%d: %d classified", i // config.CLASSIFY_BATCH_SIZE + 1,
                    (len(reviews) + config.CLASSIFY_BATCH_SIZE - 1) // config.CLASSIFY_BATCH_SIZE, n)
        # Small pause to avoid rate limits
        time.sleep(0.5)

    if error_counts:
        logger.error(
            "LLM provider errors this run — %s",
            "; ".join(f"{k} (×{v})" for k, v in error_counts.items()),
        )
    logger.info("Classification complete: %d/%d reviews classified", classified, len(reviews))
    return {
        "found": len(reviews),
        "classified": classified,
        "failed": len(reviews) - classified,
        "provider_errors": error_counts,
    }


# ── CLI backfill mode ─────────────────────────────────────────────────────────

def _run_backfill() -> None:
    total_unclassified = supabase_client.get_unclassified_review_count()
    logger.info("Backfill: %d unclassified reviews in DB", total_unclassified)

    if total_unclassified == 0:
        print("Nothing to backfill — all reviews are classified.")
        return

    print(f"Found {total_unclassified:,} unclassified reviews.")
    if total_unclassified > 50_000:
        ans = input(
            f"This is a large backfill ({total_unclassified:,} reviews). Continue? [y/N] "
        ).strip().lower()
        if ans != "y":
            print("Aborted.")
            return

    batch_size = 500
    total_classified = 0
    rounds = 0

    while True:
        stats = classify_pending(max_reviews=batch_size)
        if stats["found"] == 0:
            break
        total_classified += stats["classified"]
        rounds += 1
        logger.info("Round %d: %d classified (total so far: %d)", rounds, stats["classified"], total_classified)

    print(f"Backfill complete: {total_classified:,} reviews classified in {rounds} rounds.")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Review classifier")
    parser.add_argument("--backfill", action="store_true", help="Classify all unclassified reviews")
    args = parser.parse_args()

    if args.backfill:
        _run_backfill()
    else:
        stats = classify_pending()
        print(f"Classified {stats['classified']} of {stats['found']} pending reviews.")
