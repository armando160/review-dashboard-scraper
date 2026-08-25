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

def _send_summary_email(stats: dict, errors: list[str], critical: bool = False) -> None:
    """Send a one-line summary email via Gmail SMTP.

    `critical` marks a hard failure (e.g. classification fully down) with a 🔴 so it
    stands out from ordinary ⚠️ warnings in the inbox.

    Silently skips if NOTIFY_EMAIL or GMAIL_APP_PASSWORD are not set.
    Never raises — a failed email must not crash the pipeline.
    """
    if not config.NOTIFY_EMAIL or not config.GMAIL_APP_PASSWORD:
        logger.info("Email notification skipped (NOTIFY_EMAIL / GMAIL_APP_PASSWORD not configured)")
        return

    try:
        status = "🔴" if critical else ("⚠️" if errors else "✅")
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
    classification_outage = False  # True → LLM classification is fully down; fail the run

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
        provider_errors = classify_stats.get("provider_errors", {})

        found = stats["classify_found"]
        classified = stats["classified"]
        unclassified = found - classified

        # Turn the aggregated provider errors into a human-readable cause, e.g.
        # "OpenRouter: 401 Unauthorized (×20); Groq: 429 Too Many Requests (×20)"
        detail = "; ".join(f"{k} (×{v})" for k, v in provider_errors.items())

        if found > 0 and classified == 0:
            # Total outage: every review failed. This is a real failure — not a warning —
            # so the run must exit non-zero and turn the Action red.
            classification_outage = True
            msg = f"Classification DOWN — 0/{found} classified; all LLM providers failing"
            if detail:
                msg += f" [{detail}]"
            logger.error(msg)
            errors.append(msg)
        elif unclassified > 0:
            msg = f"Classification degraded — {unclassified}/{found} reviews failed"
            if detail:
                msg += f" [{detail}]"
            logger.warning(msg)
            errors.append(msg)
        elif provider_errors:
            # Everything classified, but a provider failed and a fallback covered it.
            # Surface this early — it's the warning that precedes a full outage.
            msg = f"Classification OK, but a provider is failing (fallback covered {found} review(s))"
            if detail:
                msg += f" [{detail}]"
            logger.warning(msg)
            errors.append(msg)

        logger.info("Classification: %s", classify_stats)
    except Exception as exc:
        classification_outage = True
        msg = f"Classification step crashed: {exc}"
        logger.error(msg)
        errors.append(msg)

    # ── Done ──────────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info(
        "Pipeline complete. %d ASINs scraped, %d new reviews.",
        stats["asins_scraped"],
        stats["new_reviews"],
    )
    logger.info("=" * 60)

    _send_summary_email(stats, errors, critical=classification_outage)

    # Fail the run (non-zero exit) when classification is fully down, so the GitHub
    # Action turns red and GitHub's failure notifications fire. Scraping still ran and
    # its data is saved — we only signal failure so a broken LLM path can't hide.
    if classification_outage:
        logger.error("Exiting with status 1: classification is down (see errors above).")
        sys.exit(1)


if __name__ == "__main__":
    main()
