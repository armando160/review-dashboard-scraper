"""LLM-based sentiment and category classification for reviews.

Fallback chain: OpenRouter (Claude Haiku) → Gemini 2.5 Flash → Groq (Llama 3.3 70B)

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


# ── Provider implementations ──────────────────────────────────────────────────

def _call_openrouter(messages: list[dict]) -> Optional[str]:
    if not config.OPENROUTER_API_KEY:
        return None
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "anthropic/claude-haiku-4.5",
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 1024,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("OpenRouter call failed: %s", exc)
        return None


def _call_gemini(messages: list[dict]) -> Optional[str]:
    if not config.GEMINI_API_KEY:
        return None
    try:
        # Combine system + user messages into Gemini's format
        system_text = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_text = next((m["content"] for m in messages if m["role"] == "user"), "")
        combined = f"{system_text}\n\n{user_text}" if system_text else user_text

        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={config.GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [{"text": combined}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1024},
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:
        logger.warning("Gemini call failed: %s", exc)
        return None


def _call_groq(messages: list[dict]) -> Optional[str]:
    if not config.GROQ_API_KEY:
        return None
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config.GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 1024,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("Groq call failed: %s", exc)
        return None


# ── LLM dispatch ──────────────────────────────────────────────────────────────

def _llm_classify(batch: list[dict[str, Any]]) -> Optional[list[dict[str, Any]]]:
    """Send batch to LLM, trying each provider in order. Returns parsed results or None."""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(batch)},
    ]

    for provider_fn in (_call_openrouter, _call_gemini, _call_groq):
        raw = provider_fn(messages)
        if raw is None:
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
                return parsed
        except (json.JSONDecodeError, IndexError, KeyError) as exc:
            logger.warning("Failed to parse LLM response: %s\nRaw: %s", exc, raw[:200])
            continue

    logger.error("All LLM providers failed for this batch")
    return None


# ── Batch processing ──────────────────────────────────────────────────────────

def _process_batch(batch: list[dict[str, Any]]) -> int:
    """Classify a single batch and write results to Supabase. Returns rows updated."""
    results = _llm_classify(batch)
    if not results:
        return 0

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

    return supabase_client.bulk_update_classifications(updates)


def classify_pending(max_reviews: int = 500) -> dict[str, int]:
    """Classify up to `max_reviews` unclassified reviews.

    Called by pipeline.py after each scrape run.
    Returns stats: {'found': int, 'classified': int}
    """
    reviews = supabase_client.get_unclassified_review_ids(limit=max_reviews)
    if not reviews:
        logger.info("No unclassified reviews found")
        return {"found": 0, "classified": 0}

    logger.info("Classifying %d reviews…", len(reviews))
    classified = 0

    for i in range(0, len(reviews), config.CLASSIFY_BATCH_SIZE):
        batch = reviews[i: i + config.CLASSIFY_BATCH_SIZE]
        n = _process_batch(batch)
        classified += n
        logger.info("Batch %d/%d: %d classified", i // config.CLASSIFY_BATCH_SIZE + 1,
                    (len(reviews) + config.CLASSIFY_BATCH_SIZE - 1) // config.CLASSIFY_BATCH_SIZE, n)
        # Small pause to avoid rate limits
        time.sleep(0.5)

    logger.info("Classification complete: %d/%d reviews classified", classified, len(reviews))
    return {"found": len(reviews), "classified": classified}


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
