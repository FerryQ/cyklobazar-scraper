## cyklobaza-fanda

`cyklobaza-fanda` is a standalone Python rewrite of the original TypeScript/Apify scraper in this repository.
It scrapes offers from [cyklobazar.cz](https://www.cyklobazar.cz), writes them to a local JSON file, and can send Telegram alerts for newly seen offers.

### Features

- No Apify dependency
- Listing-page scraping with pagination
- Optional detailed mode (visit every offer page for full description)
- Optional start-date filtering (`YYYY-MM-DD`)
- Local dedup state for Telegram notifications (`seen_offers.json`)
- Output fields compatible with the original actor:
  - `title`, `price`, `location`, `created`, `description`, `url`

### Setup

1. Use Python 3.11+.
2. Create a virtual environment.
3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Copy and edit config:

```bash
cp config.example.json config.json
```

5. Run:

```bash
python main.py --config config.json
```

### Config format

`config.json` example:

```json
{
    "urls": [{ "url": "https://www.cyklobazar.cz/gravel-bike" }],
    "detailedOutput": false,
    "startDate": "2026-04-01",
    "telegramToken": "",
    "telegramChatId": "",
    "outputFile": "output/offers.jsonl",
    "stateFile": "state/seen_offers.json",
    "requestDelaySeconds": 0.5
}
```

You can also provide `urls` as a simple string array.

### Output

Each scraped offer is appended as one JSON line to `output/offers.jsonl`.
The script keeps a local state file to avoid repeat Telegram notifications for already seen offers.
