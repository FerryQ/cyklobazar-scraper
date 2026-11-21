## cyklobaza-fanda

`cyklobaza-fanda` is a standalone Python rewrite of the original TypeScript/Apify scraper in this repository.
It scrapes offers from [cyklobazar.cz](https://www.cyklobazar.cz), evaluates valuation via Gemini, writes offers to local JSONL output, and can send Telegram alerts for newly seen offers.

### Features

- No Apify dependency
- Listing-page scraping with pagination
- Optional detailed mode (visit every offer page for full description)
- Only recent offers: `maxAgeDays` (rolling window) and/or `startDate` (`YYYY-MM-DD`); pagination stops at the first page with nothing recent, capped by `maxPages`
- Optional built-in scheduler (`intervalMinutes`) for running in Docker
- Local dedup state for Telegram notifications (`seen_offers.json`)
- Gemini valuation of each offer (`undervalued` / `fairly_priced` / `overvalued`)
- Optional filter for only very undervalued offers (min profit threshold in CZK)
- Gemini pacing + strict RPM cap + retry/backoff controls for rate-limit handling
- Output fields compatible with the original actor:
  - `title`, `price`, `location`, `created`, `description`, `url`, `valuation`

### Setup

1. Use Python 3.11+ and [uv](https://docs.astral.sh/uv/).
2. Install dependencies:

```bash
uv sync
```

3. Copy and edit config:

```bash
cp config.example.json config.json
```

4. Run:

```bash
uv run src/main.py --config config.json
```

### Docker

```bash
cp config.example.json config.json   # leave the secrets empty here
cp .env.example .env                 # put secrets and DATA_DIR here
sudo mkdir -p /srv/cyklobazar && sudo chown 1000:1000 /srv/cyklobazar
docker compose up -d --build
docker compose logs -f
```

The container runs as a non-root user with a read-only filesystem and only writes to `DATA_DIR`
(database, state and JSONL output). With `intervalMinutes` set it runs forever, scraping every N minutes;
without it, it scrapes once and exits.

### Config format

`config.json` example:

```json
{
    "urls": [{ "url": "https://www.cyklobazar.cz/gravel-bike" }],
    "detailedOutput": false,
    "maxAgeDays": 3,
    "maxPages": 20,
    "intervalMinutes": 60,
    "telegramToken": "",
    "telegramChatId": "",
    "outputFile": "output/offers.jsonl",
    "stateFile": "state/seen_offers.json",
    "databaseFile": "data/offers.db",
    "requestDelaySeconds": 0.5,
    "geminiApiKey": "",
    "geminiModel": "gemini-2.0-flash",
    "minProfitCzk": 10000,
    "onlyVeryUndervalued": false,
    "geminiMinIntervalSeconds": 1.5,
    "geminiRequestsPerMinute": 5,
    "geminiRateLimitWindowSeconds": 60,
    "geminiMaxRetries": 1,
    "geminiRetryBaseDelaySeconds": 2.0,
    "disableGeminiOnRateLimit": true
}
```

You can also provide `urls` as a simple string array.
Secrets can be left empty in the config and provided through the `GEMINI_API_KEY`, `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` environment variables.
You can still set `startDate` (`YYYY-MM-DD`); if both it and `maxAgeDays` are set, the later cutoff wins.

### Output

Each offer is appended as one JSON line to `output/offers.jsonl`.
The script keeps a local state file to avoid repeat Telegram notifications for already seen offers.
Offers whose valuation failed (e.g. Gemini errors) are not marked as seen when `onlyVeryUndervalued` is on, so they are retried on the next run.

Every scraped listing (including already seen ones) is also stored in a SQLite database (`data/offers.db`):

- `listings` - latest data per listing, with `first_seen` / `last_seen` timestamps and the last successful valuation
- `price_history` - one row each time a listing's price changes
By default, all new offers are written and notified, with valuation reasoning included.
If Gemini keeps returning `429`, the scraper can auto-disable Gemini for the rest of the run and still send offers with `valuation.verdict = "unknown"` and an explanatory reason.
