"""All Supabase read/write operations for the scraper pipeline."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from supabase import create_client, Client

import config

logger = logging.getLogger(__name__)

_client: Optional[Client] = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    return _client


# ── Products ──────────────────────────────────────────────────────────────────

def upsert_products(rows: list[dict[str, Any]]) -> int:
    """Upsert product metadata. Returns number of rows processed."""
    if not rows:
        return 0
    sb = get_client()
    sb.table("products").upsert(rows, on_conflict="asin").execute()
    return len(rows)


def get_asins_due_for_scraping(limit: int = config.ASINS_PER_RUN) -> list[dict[str, Any]]:
    """Return up to `limit` products due for scraping, ordered by next_scrape_at."""
    sb = get_client()
    now = datetime.now(timezone.utc).isoformat()
    result = (
        sb.table("products")
        .select("asin, product_name, brand, rating, scrape_tier, next_scrape_at")
        .or_(f"next_scrape_at.is.null,next_scrape_at.lte.{now}")
        .order("next_scrape_at", desc=False, nullsfirst=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def update_next_scrape(asin: str, tier: int) -> None:
    """Set next_scrape_at and scrape_tier for an ASIN based on tier hours."""
    from datetime import timedelta

    hours = config.TIER_INTERVALS.get(tier, 48)
    next_at = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    sb = get_client()
    sb.table("products").update(
        {
            "scrape_tier": tier,
            "next_scrape_at": next_at,
            "last_scraped_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("asin", asin).execute()


# ── Reviews ───────────────────────────────────────────────────────────────────

def insert_reviews(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """Insert reviews, skipping duplicates via review_key conflict.

    Returns (attempted, inserted) counts.
    """
    if not rows:
        return 0, 0

    sb = get_client()
    # Supabase upsert with ignoreDuplicates skips conflicting rows
    result = (
        sb.table("reviews")
        .upsert(rows, on_conflict="review_key", ignore_duplicates=True)
        .execute()
    )
    inserted = len(result.data) if result.data else 0
    return len(rows), inserted


def get_unclassified_review_ids(limit: int = 500) -> list[dict[str, Any]]:
    """Return reviews where sentiment IS NULL."""
    sb = get_client()
    result = (
        sb.table("reviews")
        .select("id, title, review_text")
        .is_("sentiment", "null")
        .order("id", desc=False)
        .limit(limit)
        .execute()
    )
    return result.data or []


def get_unclassified_review_count() -> int:
    sb = get_client()
    result = (
        sb.table("reviews")
        .select("id", count="exact", head=True)
        .is_("sentiment", "null")
        .execute()
    )
    return result.count or 0


def update_review_classification(
    review_id: int,
    sentiment: str,
    sentiment_confidence: float,
    review_category: str,
    category_confidence: float,
) -> None:
    sb = get_client()
    sb.table("reviews").update(
        {
            "sentiment": sentiment,
            "sentiment_confidence": sentiment_confidence,
            "review_category": review_category,
            "category_confidence": category_confidence,
        }
    ).eq("id", review_id).execute()


def bulk_update_classifications(updates: list[dict[str, Any]]) -> int:
    """Update multiple review classifications. Each dict must have 'id' key.

    Returns count of updates.
    """
    if not updates:
        return 0
    sb = get_client()
    for row in updates:
        rid = row.pop("id")
        sb.table("reviews").update(row).eq("id", rid).execute()
        row["id"] = rid  # restore for caller reference
    return len(updates)


# ── Scrape log ────────────────────────────────────────────────────────────────

def log_scrape_start(asin: str) -> int:
    """Insert a running scrape_log entry. Returns the new log row id."""
    sb = get_client()
    result = (
        sb.table("scrape_log")
        .insert(
            {
                "asin": asin,
                "mode": "max",
                "reviews_found": 0,
                "new_reviews": 0,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "status": "running",
            }
        )
        .execute()
    )
    return result.data[0]["id"]


def log_scrape_complete(
    log_id: int,
    reviews_found: int,
    new_reviews: int,
    status: str = "completed",
    error_message: Optional[str] = None,
) -> None:
    sb = get_client()
    sb.table("scrape_log").update(
        {
            "reviews_found": reviews_found,
            "new_reviews": new_reviews,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "error_message": error_message,
        }
    ).eq("id", log_id).execute()
