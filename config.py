import os
from dataclasses import dataclass, field
from typing import Optional

# ── Supabase ──────────────────────────────────────────────────────────────────
SUPABASE_URL: str = os.environ["SUPABASE_URL"]
SUPABASE_KEY: str = os.environ["SUPABASE_KEY"]  # service role key

# ── Monday.com ────────────────────────────────────────────────────────────────
MONDAY_TOKEN: str = os.environ["MONDAY_TOKEN"]
MONDAY_BOARD_ID: str = "8574487078"
MONDAY_API_URL: str = "https://api.monday.com/v2"

# Column IDs on the Monday board
MONDAY_COL_ASIN = "text_mknhd0s7"
MONDAY_COL_BRAND = "color_mktjf611"
MONDAY_COL_PRODUCT = "text_mknhzj47"
MONDAY_COL_STATUS = "status"
MONDAY_COL_RATING = "numeric_mknj71zj"
MONDAY_COL_REVIEWS = "numeric_mknjr9cg"
MONDAY_COL_CATEGORY = "text_mkxp62c"
MONDAY_COL_DEAL_BUCKET = "color_mky9e9at"

# ── LLM keys (optional — pipeline continues if missing) ──────────────────────
OPENROUTER_API_KEY: Optional[str] = os.environ.get("OPENROUTER_API_KEY")
GEMINI_API_KEY: Optional[str] = os.environ.get("GEMINI_API_KEY")
GROQ_API_KEY: Optional[str] = os.environ.get("GROQ_API_KEY")

# ── Woot API ──────────────────────────────────────────────────────────────────
WOOT_BASE_URL = "https://www.woot.com/review/Reviews"
WOOT_RATINGS = [1, 2, 3, 4, 5]
WOOT_SORT_ORDERS = ["relevancy", "recent", "helpful_up", "helpful_down"]
WOOT_DELAY_BETWEEN_ASINS = 2.5  # seconds
WOOT_REQUEST_TIMEOUT = 30       # seconds

# ── Scraper behaviour ─────────────────────────────────────────────────────────
ASINS_PER_RUN = 20

# Tier → hours until next scrape
TIER_INTERVALS = {1: 24, 2: 48, 3: 72, 4: 96}

# Tier assignment thresholds (inclusive upper bounds by rating)
def assign_tier(rating: Optional[float]) -> int:
    if rating is None:
        return 2
    if rating < 4.0:
        return 1
    if rating < 4.5:
        return 2
    if rating <= 4.7:
        return 3
    return 4

# ── LLM classification ────────────────────────────────────────────────────────
CLASSIFY_BATCH_SIZE = 10
CLASSIFY_MIN_CONFIDENCE = 0.60

REVIEW_CATEGORIES = [
    "Product Quality",
    "Usability & Design",
    "Value",
    "Customer Service",
    "Fulfillment",
    "Other",
]

SENTIMENTS = ["positive", "negative", "neutral"]
