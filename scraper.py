"""Woot API scraper — max mode: 5 ratings × 4 sort orders = 20 calls per ASIN."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import date
from typing import Any, Optional

import requests

import config

logger = logging.getLogger(__name__)

# ── Date parsing ──────────────────────────────────────────────────────────────

_US_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),\s+(\d{4})",
    re.IGNORECASE,
)
_EU_DATE_RE = re.compile(
    r"(\d{1,2})\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{4})",
    re.IGNORECASE,
)
_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_date(origin_description: Optional[str]) -> Optional[date]:
    if not origin_description:
        return None
    m = _US_DATE_RE.search(origin_description)
    if m:
        month = _MONTH_MAP[m.group(1).lower()]
        day = int(m.group(2))
        year = int(m.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            return None
    m = _EU_DATE_RE.search(origin_description)
    if m:
        day = int(m.group(1))
        month = _MONTH_MAP[m.group(2).lower()]
        year = int(m.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


# ── Dedup hash ────────────────────────────────────────────────────────────────

def _review_key(author: str, title: str, text: str) -> str:
    raw = f"{author}|{title}|{text[:80]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── API fetching ──────────────────────────────────────────────────────────────

def _fetch_page(asin: str, rating: int, sort: str, page: int = 1) -> Optional[dict[str, Any]]:
    url = f"{config.WOOT_BASE_URL}/{asin}"
    params = {"filter": rating, "sort": sort, "page": page}
    try:
        resp = requests.get(url, params=params, timeout=config.WOOT_REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        logger.warning("Woot API error for %s (rating=%s, sort=%s, page=%d): %s", asin, rating, sort, page, exc)
        return None


def _parse_review(item: dict[str, Any], asin: str) -> Optional[dict[str, Any]]:
    author = (item.get("Author") or "").strip()
    title = (item.get("Title") or "").strip()
    text = (item.get("Text") or "").strip()

    if not author and not title and not text:
        return None

    raw_rating = item.get("OverallRating")
    try:
        star_rating = int(raw_rating) if raw_rating is not None else None
    except (ValueError, TypeError):
        star_rating = None

    review_date = _parse_date(item.get("OriginDescription"))

    image_urls = item.get("ImageUrls") or []
    media_urls = item.get("MediaUrls") or []

    helpful_votes: Optional[int] = None
    hv_raw = item.get("HelpfulVotes") or item.get("HelpfulVotesCount")
    if hv_raw is not None:
        try:
            helpful_votes = int(hv_raw)
        except (ValueError, TypeError):
            pass

    is_vine: Optional[bool] = None
    vine_raw = item.get("IsVineReview") or item.get("VineReview")
    if vine_raw is not None:
        is_vine = bool(vine_raw)

    return {
        "asin": asin,
        "review_key": _review_key(author, title, text),
        "author": author or None,
        "title": title or None,
        "review_text": text or None,
        "rating": star_rating,
        "review_date": review_date.isoformat() if review_date else None,
        "is_verified_purchase": bool(item.get("IsVerifiedPurchase")),
        "is_vine_review": is_vine,
        "helpful_votes": helpful_votes,
        "image_count": len(image_urls) if isinstance(image_urls, list) else 0,
        "video_count": len(media_urls) if isinstance(media_urls, list) else 0,
        "compliance_checked": False,
    }


# ── Main scrape function ──────────────────────────────────────────────────────

def scrape_asin(asin: str) -> list[dict[str, Any]]:
    """Scrape reviews for a single ASIN in max mode.

    Returns a list of review dicts ready for Supabase insert.
    Deduplication within the run is handled by review_key — the DB handles
    cross-run dedup via the UNIQUE constraint.
    """
    seen_keys: set[str] = set()
    reviews: list[dict[str, Any]] = []

    for rating in config.WOOT_RATINGS:
        for sort in config.WOOT_SORT_ORDERS:
            page = 1
            while True:
                data = _fetch_page(asin, rating, sort, page)
                if not data:
                    break

                items = data.get("Reviews") or data.get("reviews") or []
                if not items:
                    break

                added_this_page = 0
                for item in items:
                    parsed = _parse_review(item, asin)
                    if parsed and parsed["review_key"] not in seen_keys:
                        seen_keys.add(parsed["review_key"])
                        reviews.append(parsed)
                        added_this_page += 1

                # Woot paginates — check if there's a next page signal
                paging_next = data.get("PagingNext") or data.get("pagingNext")
                if not paging_next or added_this_page == 0:
                    break
                page += 1

    logger.info("ASIN %s: %d unique reviews found across all combinations", asin, len(reviews))
    return reviews
