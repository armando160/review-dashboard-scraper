# review-dashboard-scraper

Automated review scraper for the Chai Vision Amazon analytics dashboard.
Runs 2× daily via GitHub Actions (6 AM and 6 PM EST).

## What it does

1. **Brand Sync** — pulls all ASINs, brands, and metadata from the Monday.com board into Supabase `products`
2. **Scrape** — hits the Woot review API in max mode (5 ratings × 4 sort orders = 20 calls/ASIN), deduplicates via SHA-256 hash, inserts new reviews into Supabase `reviews`
3. **Classify** — sends unclassified reviews through an LLM (OpenRouter → Gemini → Groq fallback) to assign sentiment and category

## Setup

### 1. Run the database migration

Open the [Supabase SQL editor](https://supabase.com/dashboard/project/bjrtlozqpfbrsllthxnm/sql) and paste + run the contents of `migrations/001_dashboard_columns.sql`.

### 2. Create a new GitHub repo

Push this folder to a new GitHub repository (ideally on a fresh GitHub account for a clean Actions quota).

### 3. Set GitHub Actions secrets

In the repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `SUPABASE_URL` | `https://bjrtlozqpfbrsllthxnm.supabase.co` |
| `SUPABASE_KEY` | Supabase **service role** key (from Supabase dashboard → Project Settings → API) |
| `MONDAY_TOKEN` | Monday.com API token (Profile → Developers → API v2 Token) |
| `OPENROUTER_API_KEY` | OpenRouter key (primary LLM) |
| `GEMINI_API_KEY` | Google AI Studio key (fallback) |
| `GROQ_API_KEY` | Groq key (last-resort fallback) |

### 4. Trigger the first run manually

Go to Actions → "Review Dashboard Scraper" → Run workflow.

## Local testing

```bash
cp .env.example .env
# Fill in .env values

pip install -r requirements.txt

# Test brand sync only
python -c "import brand_sync; print(brand_sync.run())"

# Test scraper on one ASIN
python -c "import scraper; reviews = scraper.scrape_asin('B08N5WRWNW'); print(len(reviews), 'reviews')"

# Run full pipeline
python pipeline.py

# Backfill all unclassified reviews (run once after migration)
python classifier.py --backfill
```

## Backfill

After running the migration, existing reviews will have `sentiment = NULL`.
Run the backfill once manually to classify them:

```bash
python classifier.py --backfill
```

It will show the count and ask for confirmation if > 50,000 reviews.
