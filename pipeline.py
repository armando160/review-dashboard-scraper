"""Main pipeline orchestrator.

Steps:
  1. Brand Sync — sync Monday metadata → Supabase products
  2. Select ASINs — pick up to 20 due for scraping
  3. Scrape Reviews — hit Woot API, insert new reviews
  4. Classify — run LLM classification on unclassified reviews
  5. Send summary email

Usage:
    python pipeline.py
"""

from __future__ import annotations

import logging
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText

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


# ── Email notification ────────────────────────────────────────────────────────

def _send_summary_email(stats: dict, errors: list[str]) -> None:
    """Send a one-line summary email via Gmail SMTP.

    Silently skips if NOTIFY_EMAIL or GMAIL_APP_PASSWORD are not set.
    Never raises — a failed email must not crash the pipeline.
    """
    if not config.NOTIFY_EMAIL or not config.GMAIL_APP_PASSWORD:
        logger.info("Email notification skipped (NOTIFY_EMAIL / GMAIL_APP_PASSWORD not configured)")
        return

    try:
        status = "⚠️" if errors else "✅"
        subject = (
            f"{status} Review Scraper — "
            f"{stats.get('asins_scraped', 0)} ASINs | "
            f"{stats.get('new_reviews', 0)} new reviews | "
            f"{stats.get('classified', 0)}/{stats.get('classify_found', 0)} classified"
        )

        lines = [
            "Review Dashboard Scraper — Run Summary",
            f"Completed: {stats.get('run_time', 'unknown')}",
            "",
            f"  Brand sync      {stats.get('products_upserted', 0)} products synced from Monday.com",
            f"  Scraping        {stats.get('asins_scraped', 0)} ASINs · "
            f"{stats.get('reviews_found', 0)} found · {stats.get('new_reviews', 0)} new",
            f"  Classification  {stats.get('classified', 0)} / {stats.get('classify_found', 0)} reviews classified",
        ]

        if errors:
            lines += ["", "⚠️  Issues detected:"]
            for e in errors:
                lines.append(f"   · {e}")
        else:
            lines += ["", "No errors."]

        body = "\n".join(lines)

        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = config.NOTIFY_EMAIL
        msg["To"] = config.NOTIFY_EMAIL

        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(config.NOTIFY_EMAIL, config.GMAIL_APP_PASSWORD)
            smtp.sendmail(config.NOTIFY_EMAIL, config.NOTIFY_EMAIL, msg.as_string())

        logger.info("Summary email sent to %s", config.NOTIFY_EMAIL)

    except Exception as exc:
        logger.warning("Failed to send summary email (non-fatal): %s", exc)


# ── Main pipeline ─────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 60)
    logger.info("Review Dashboard Scraper — pipeline start")
    logger.info("=" * 60)

    stats: dict = {
        "run_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "products_upserted": 0,
        "asins_scraped": 0,
        "reviews_found": 0,
        "new_reviews": 0,
        "classify_found": 0,
        "classified": 0,
    }
    errors: list[str] = []

    # ── Step 1: Brand Sync ────────────────────────────────────────────────────
    logger.info("[1/4] Brand sync from Monday.com…")
    try:
        sync_stats = brand_sync.run()
        stats["products_upserted"] = sync_stats.get("upserted", 0)
        logger.info("Brand sync: %s", sync_stats)
    except Exception as exc:
        msg = f"Brand sync failed: {exc}"
        logger.error("%s (non-fatal)", msg)
        errors.append(msg)

    # ── Step 2: Select ASINs ──────────────────────────────────────────────────
    logger.info("[2/4] Selecting ASINs due for scraping…")
    products = supabase_client.get_asins_due_for_scraping(limit=config.ASINS_PER_RUN)
    if not products:
        logger.info("No ASINs due for scraping. Exiting.")
        _send_summary_email(stats, errors)
        return

    logger.info("Selected %d ASINs", len(products))
    stats["asins_scraped"] = len(products)

    # ── Step 3: Scrape ────────────────────────────────────────────────────────
    logger.info("[3/4] Scraping reviews…")
    asin_errors = 0

    for i, product in enumerate(products, 1):
        asin = product["asin"]
        rating = product.get("rating")
        logger.info("  [%d/%d] Scraping ASIN %s…", i, len(products), asin)

        log_id = supabase_client.log_scrape_start(asin)

        try:
            reviews = scraper.scrape_asin(asin)
            attempted, inserted = supabase_client.insert_reviews(reviews)
            stats["reviews_found"] += attempted
            stats["new_reviews"] += inserted

            tier = config.assign_tier(rating)
            supabase_client.update_next_scrape(asin, tier)
            supabase_client.log_scrape_complete(log_id, attempted, inserted)

            logger.info("  ASIN %s: %d found, %d new (tier %d)", asin, attempted, inserted, tier)

        except Exception as exc:
            logger.error("  ASIN %s failed: %s", asin, exc)
            supabase_client.log_scrape_complete(log_id, 0, 0, status="error", error_message=str(exc))
            asin_errors += 1

        if i < len(products):
            time.sleep(config.WOOT_DELAY_BETWEEN_ASINS)

    if asin_errors:
        errors.append(f"{asin_errors} ASIN(s) failed during scraping")

    logger.info(
        "Scraping complete: %d reviews found, %d new",
        stats["reviews_found"],
        stats["new_reviews"],
    )

    # ── Step 4: Classify ──────────────────────────────────────────────────────
    logger.info("[4/4] Classifying new reviews…")
    try:
        classify_stats = classify_pending(max_reviews=500)
        stats["classify_found"] = classify_stats.get("found", 0)
        stats["classified"] = classify_stats.get("classified", 0)

        unclassified = stats["classify_found"] - stats["classified"]
        if unclassified > 0:
            errors.append(
                f"Classification: {unclassified} review(s) failed across all LLM providers"
            )

        logger.info("Classification: %s", classify_stats)
    except Exception as exc:
        msg = f"Classification step failed entirely: {exc}"
        logger.error("%s (non-fatal)", msg)
        errors.append(msg)

    # ── Done ──────────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info(
        "Pipeline complete. %d ASINs scraped, %d new reviews.",
        stats["asins_scraped"],
        stats["new_reviews"],
    )
    logger.info("=" * 60)

    _send_summary_email(stats, errors)


if __name__ == "__main__":
    main()
