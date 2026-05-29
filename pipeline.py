"""Main pipeline orchestrator.

Steps:
  1. Brand Sync — sync Monday metadata → Supabase products
  2. Select ASINs — pick up to 20 due for scraping
  3. Scrape Reviews — hit Woot API, insert new reviews
  4. Classify — run LLM classification on unclassified reviews
  5. Update next_scrape_at — schedule next run per tier

Usage:
    python pipeline.py
"""

from __future__ import annotations

import logging
import sys
import time

# Load .env for local development (no-op in GitHub Actions where secrets are env vars)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import brand_sync
import config
import scraper
import supabase_client
from classifier import classify_pending

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("pipeline")


def main() -> None:
    logger.info("=" * 60)
    logger.info("Review Dashboard Scraper — pipeline start")
    logger.info("=" * 60)

    # ── Step 1: Brand Sync ────────────────────────────────────────────────────
    logger.info("[1/4] Brand sync from Monday.com…")
    try:
        sync_stats = brand_sync.run()
        logger.info("Brand sync: %s", sync_stats)
    except Exception as exc:
        logger.error("Brand sync failed (non-fatal): %s", exc)

    # ── Step 2: Select ASINs ──────────────────────────────────────────────────
    logger.info("[2/4] Selecting ASINs due for scraping…")
    products = supabase_client.get_asins_due_for_scraping(limit=config.ASINS_PER_RUN)
    if not products:
        logger.info("No ASINs due for scraping. Exiting.")
        return

    logger.info("Selected %d ASINs", len(products))

    # ── Step 3: Scrape ────────────────────────────────────────────────────────
    logger.info("[3/4] Scraping reviews…")
    total_found = 0
    total_new = 0

    for i, product in enumerate(products, 1):
        asin = product["asin"]
        rating = product.get("rating")
        logger.info("  [%d/%d] Scraping ASIN %s…", i, len(products), asin)

        log_id = supabase_client.log_scrape_start(asin)

        try:
            reviews = scraper.scrape_asin(asin)
            attempted, inserted = supabase_client.insert_reviews(reviews)
            total_found += attempted
            total_new += inserted

            tier = config.assign_tier(rating)
            supabase_client.update_next_scrape(asin, tier)
            supabase_client.log_scrape_complete(log_id, attempted, inserted)

            logger.info("  ASIN %s: %d found, %d new (tier %d)", asin, attempted, inserted, tier)

        except Exception as exc:
            logger.error("  ASIN %s failed: %s", asin, exc)
            supabase_client.log_scrape_complete(log_id, 0, 0, status="error", error_message=str(exc))

        # Delay between ASINs (not between API calls per ASIN — scraper.py handles none there)
        if i < len(products):
            time.sleep(config.WOOT_DELAY_BETWEEN_ASINS)

    logger.info("Scraping complete: %d reviews found, %d new", total_found, total_new)

    # ── Step 4: Classify ──────────────────────────────────────────────────────
    logger.info("[4/4] Classifying new reviews…")
    try:
        classify_stats = classify_pending(max_reviews=500)
        logger.info("Classification: %s", classify_stats)
    except Exception as exc:
        logger.error("Classification failed (non-fatal): %s", exc)

    logger.info("=" * 60)
    logger.info("Pipeline complete. %d ASINs scraped, %d new reviews.", len(products), total_new)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
