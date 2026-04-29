from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from scraper import CyklobazarScraper, ScraperSettings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cyklobazar scraper")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Path to JSON config (default: ./config.json)",
    )
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in config file {path}: {exc}") from exc


def pick(config: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in config:
            return config[key]
    return default


def normalize_urls(raw_urls: Any) -> list[str]:
    if not isinstance(raw_urls, list) or not raw_urls:
        raise ValueError("Config key 'urls' must be a non-empty list")

    normalized: list[str] = []
    for item in raw_urls:
        if isinstance(item, str):
            url = item.strip()
        elif isinstance(item, dict) and isinstance(item.get("url"), str):
            url = item["url"].strip()
        else:
            raise ValueError("Each urls item must be a URL string or an object with a 'url' string")

        if not url.startswith(("http://", "https://")):
            raise ValueError(f"Invalid URL: {url}")
        normalized.append(url)

    return normalized


def build_settings(config: dict[str, Any]) -> ScraperSettings:
    request_delay_seconds = float(
        pick(config, "requestDelaySeconds", "request_delay_seconds", default=0.0),
    )
    if request_delay_seconds < 0:
        raise ValueError("requestDelaySeconds must be >= 0")
    # todo: create ouput_file and state_file if it doesnt exist already
    return ScraperSettings(
        urls=normalize_urls(pick(config, "urls")),
        detailed_output=bool(pick(config, "detailedOutput", "detailed_output", default=False)),
        start_date=pick(config, "startDate", "start_date"),
        telegram_token=pick(config, "telegramToken", "telegram_token"),
        telegram_chat_id=pick(config, "telegramChatId", "telegram_chat_id"),
        output_file=Path(pick(config, "outputFile", "output_file", default="output/offers.jsonl")),
        state_file=Path(pick(config, "stateFile", "state_file", default="state/seen_offers.json")),
        request_delay_seconds=request_delay_seconds,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    args = parse_args()
    try:
        config = load_config(args.config)
        settings = build_settings(config)
        scraper = CyklobazarScraper(settings)
        scraped = scraper.run()
        logging.info("Finished. Scraped %s offers.", scraped)
        return 0
    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")
        return 130
    except Exception as exc:
        logging.error("Scraper failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
