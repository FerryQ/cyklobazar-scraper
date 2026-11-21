# Backlog

Goal: find undervalued bikes by learning market prices from real listings, instead of asking an LLM to guess.

## 1. Unblock scraping (blocking everything else)

- [ ] **Get past Cloudflare.** Plain `requests` gets a "Just a moment..." challenge page (confirmed from the home server).
      Try `curl_cffi` (impersonates a real browser's TLS fingerprint) before anything heavier like a headless browser.

## 2. Start collecting data

The sooner this runs, the sooner there is data to learn from.

- [ ] **Make Gemini optional.** `geminiApiKey` is currently required. Allow running with valuation off: just store listings.
- [ ] **Separate collection from alerts.** Scrape whole categories (gravel, road, MTB) without price/location filters
      for the dataset; apply price/location filters only when deciding what to alert on.
- [ ] **Backfill.** One run with a high `maxPages` and no `maxAgeDays` to start with thousands of current listings.
- [ ] **Nightly DB backup** from `/srv/cyklobazar/data/offers.db` to the RAID (`sqlite3 ... ".backup ..."` in host cron).

## 3. Extract bike attributes (while data accumulates)

- [ ] **Rule-based extraction** into new `listings` columns: brand, model, year (`20\d\d`), frame size (`M`, `54 cm`),
      material (karbon / alu), groupset (105, Ultegra, GRX, Rival, Apex, ...), wheel size. Regex + lookup lists.
- [ ] **Hand-label ~200 listings** as a test set to measure extraction quality.
- [ ] **If rules aren't good enough:** cheap LLM (Flash-Lite class) for extraction only, once per listing, cached in the DB.
      Its output can later be training data for an own NER model, if wanted as a learning project.
- [ ] **Filter out non-bikes:** frame-only, parts, kids' bikes, e-bikes in the wrong category.

## 4. Price model

- [ ] **Baseline: comparables.** Median price of the k most similar listings (same brand + model family, year ±1).
- [ ] **Gradient boosting** (CatBoost handles categorical features well) on the extracted attributes. Compare against the
      baseline on held-out data (mean absolute error in CZK).
- [ ] **Asking price ≠ sale price.** Use `first_seen` / `last_seen`: listings gone within days probably sold at/below market;
      ones sitting for 60+ days were overpriced. Weight or filter on that.
- [ ] **Seasonality.** Prices peak in spring; collect for a few months before trusting the model.
- [ ] Undervalued = asking price well below predicted price (e.g. below 20th percentile of comparables, with ≥5 comparables).

## 5. LLM judge on flagged candidates

- [ ] Only for the few listings the model flags per day. Give it the listing + nearest comparables with prices.
      Its job: catch what the model can't see ("frame only", cracked carbon, suspicious listing, wrong category).

## Cleanup (not urgent)

- [ ] **CI is broken:** `.github/workflows/lint.yaml` still runs npm. Replace with `uv run ruff check` + `uv run pytest`.
- [ ] **Tests:** save 2–3 real HTML pages as fixtures and test the parsing functions (would catch site HTML changes).
- [ ] **Simplify Gemini rate limiting:** one limiter + simple backoff instead of 7 settings (or `tenacity`).
- [ ] **One config key style:** drop the camelCase/snake_case double lookup in `build_settings` (or use pydantic).
- [ ] **Split `scraper.py`** into parsing / valuation / storage modules.
- [ ] **Newer Gemini model** + `responseSchema` structured output (lets us delete `parse_json_object_from_text`).
- [ ] **Price parsing:** `parse_czk_price` concatenates all digits; would break if a card shows an old + new price.
- [ ] Optionally rename `docker-compose.yml` → `compose.yaml` to match the other home-server stacks.
