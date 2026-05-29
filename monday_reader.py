"""Fetch all items from the Monday.com Amazon USA Products board.

Uses cursor-based pagination (500 items/page) via the Monday GraphQL API.
Returns a list of dicts ready for upsert into the Supabase products table.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

import config

logger = logging.getLogger(__name__)

HEADERS = {
    "Authorization": f"Bearer {config.MONDAY_TOKEN}",
    "Content-Type": "application/json",
    "API-Version": "2024-10",
}

PAGE_SIZE = 500


def _gql(query: str, variables: Optional[dict] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(
        config.MONDAY_API_URL,
        json=payload,
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Monday GraphQL errors: {data['errors']}")
    return data


def _col_value(columns: list[dict], col_id: str) -> Optional[str]:
    for col in columns:
        if col["id"] == col_id:
            return col.get("text") or col.get("value")
    return None


def fetch_all_items() -> list[dict[str, Any]]:
    """Return all items from the board, paginated via cursor."""
    items: list[dict[str, Any]] = []
    cursor: Optional[str] = None

    while True:
        if cursor:
            query = """
            query ($board_id: ID!, $page_size: Int!, $cursor: String!) {
              boards(ids: [$board_id]) {
                items_page(limit: $page_size, cursor: $cursor) {
                  cursor
                  items {
                    id
                    name
                    column_values {
                      id
                      text
                      value
                    }
                  }
                }
              }
            }
            """
            variables = {
                "board_id": config.MONDAY_BOARD_ID,
                "page_size": PAGE_SIZE,
                "cursor": cursor,
            }
        else:
            query = """
            query ($board_id: ID!, $page_size: Int!) {
              boards(ids: [$board_id]) {
                items_page(limit: $page_size) {
                  cursor
                  items {
                    id
                    name
                    column_values {
                      id
                      text
                      value
                    }
                  }
                }
              }
            }
            """
            variables = {
                "board_id": config.MONDAY_BOARD_ID,
                "page_size": PAGE_SIZE,
            }

        data = _gql(query, variables)
        page = data["data"]["boards"][0]["items_page"]
        items.extend(page["items"])
        logger.info("Fetched %d items so far (cursor: %s)", len(items), cursor)

        cursor = page.get("cursor")
        if not cursor:
            break

    return items


def parse_items(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert raw Monday items into product dicts for Supabase upsert."""
    products = []
    for item in raw_items:
        cols = item.get("column_values", [])

        asin = _col_value(cols, config.MONDAY_COL_ASIN)
        if not asin or not asin.strip():
            continue

        asin = asin.strip().upper()

        rating_str = _col_value(cols, config.MONDAY_COL_RATING)
        try:
            rating = float(rating_str) if rating_str else None
        except (ValueError, TypeError):
            rating = None

        reviews_str = _col_value(cols, config.MONDAY_COL_REVIEWS)
        try:
            review_count = int(float(reviews_str)) if reviews_str else None
        except (ValueError, TypeError):
            review_count = None

        tier = config.assign_tier(rating)

        products.append(
            {
                "asin": asin,
                "product_name": _col_value(cols, config.MONDAY_COL_PRODUCT) or item.get("name"),
                "monday_item_id": str(item["id"]),
                "status": _col_value(cols, config.MONDAY_COL_STATUS),
                "brand": _col_value(cols, config.MONDAY_COL_BRAND),
                "product_category": _col_value(cols, config.MONDAY_COL_CATEGORY),
                "rating": rating,
                "review_count": review_count,
                "scrape_tier": tier,
            }
        )

    # Deduplicate by ASIN — Monday board has color/size variants that share the
    # same ASIN. PostgreSQL's ON CONFLICT DO UPDATE rejects a batch that tries to
    # update the same row twice, so we must collapse duplicates here.
    # We keep the last occurrence so the most recently-entered Monday item wins.
    seen: dict[str, dict[str, Any]] = {}
    for p in products:
        seen[p["asin"]] = p
    deduped = list(seen.values())

    if len(deduped) < len(products):
        logger.warning(
            "Deduplicated %d duplicate ASINs (Monday items: %d → unique ASINs: %d)",
            len(products) - len(deduped),
            len(raw_items),
            len(deduped),
        )

    logger.info("Parsed %d valid products from %d Monday items", len(deduped), len(raw_items))
    return deduped


def get_all_products() -> list[dict[str, Any]]:
    """Public entry point — fetch + parse in one call."""
    raw = fetch_all_items()
    return parse_items(raw)
