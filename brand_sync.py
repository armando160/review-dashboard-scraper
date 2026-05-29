"""Sync brand and product metadata from Monday.com into Supabase products table."""

from __future__ import annotations

import logging

import monday_reader
import supabase_client

logger = logging.getLogger(__name__)


def run() -> dict[str, int]:
    """Fetch all Monday items and upsert into products table.

    Returns stats dict with 'fetched' and 'upserted' counts.
    """
    logger.info("Starting brand sync from Monday.com…")
    products = monday_reader.get_all_products()
    upserted = supabase_client.upsert_products(products)
    logger.info("Brand sync complete: %d products upserted", upserted)
    return {"fetched": len(products), "upserted": upserted}
