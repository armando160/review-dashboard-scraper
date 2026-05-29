-- Migration 001: Add columns required by the review analytics dashboard
-- Run this against Supabase project bjrtlozqpfbrsllthxnm
-- These are all additive (IF NOT EXISTS) — safe to run multiple times

-- ── products table ──────────────────────────────────────────────────────────
ALTER TABLE products ADD COLUMN IF NOT EXISTS brand TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS product_category TEXT;

-- ── reviews table ────────────────────────────────────────────────────────────
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS sentiment TEXT;               -- 'positive', 'negative', 'neutral'
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS review_category TEXT;         -- one of 6 categories
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS sentiment_confidence NUMERIC; -- 0.0-1.0
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS category_confidence NUMERIC;  -- 0.0-1.0

-- ── Indexes for dashboard query performance ───────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_reviews_sentiment       ON reviews(sentiment);
CREATE INDEX IF NOT EXISTS idx_reviews_category        ON reviews(review_category);
CREATE INDEX IF NOT EXISTS idx_reviews_asin_date       ON reviews(asin, review_date);
CREATE INDEX IF NOT EXISTS idx_products_brand          ON products(brand);

-- ── Row Level Security (enable + read-only anon policies) ────────────────────
-- Dashboard uses the anon key (client-side), so RLS must allow SELECT.
-- Scraper uses the service role key, which bypasses RLS entirely.

ALTER TABLE products        ENABLE ROW LEVEL SECURITY;
ALTER TABLE reviews         ENABLE ROW LEVEL SECURITY;
ALTER TABLE compliance_flags ENABLE ROW LEVEL SECURITY;
ALTER TABLE scrape_log      ENABLE ROW LEVEL SECURITY;

-- Drop first to avoid "already exists" errors on re-run
DROP POLICY IF EXISTS "anon_read" ON products;
DROP POLICY IF EXISTS "anon_read" ON reviews;
DROP POLICY IF EXISTS "anon_read" ON compliance_flags;
DROP POLICY IF EXISTS "anon_read" ON scrape_log;

CREATE POLICY "anon_read" ON products        FOR SELECT USING (true);
CREATE POLICY "anon_read" ON reviews         FOR SELECT USING (true);
CREATE POLICY "anon_read" ON compliance_flags FOR SELECT USING (true);
CREATE POLICY "anon_read" ON scrape_log      FOR SELECT USING (true);
