from __future__ import annotations

import argparse
import json
import logging
import os
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

    min_profit_czk = int(pick(config, "minProfitCzk", "min_profit_czk", default=10000))
    if min_profit_czk < 0:
        raise ValueError("minProfitCzk must be >= 0")

    gemini_min_interval_seconds = float(
        pick(config, "geminiMinIntervalSeconds", "gemini_min_interval_seconds", default=1.5),
    )
    if gemini_min_interval_seconds < 0:
        raise ValueError("geminiMinIntervalSeconds must be >= 0")

    gemini_max_retries = int(pick(config, "geminiMaxRetries", "gemini_max_retries", default=1))
    if gemini_max_retries < 0:
        raise ValueError("geminiMaxRetries must be >= 0")

    gemini_retry_base_delay_seconds = float(
        pick(config, "geminiRetryBaseDelaySeconds", "gemini_retry_base_delay_seconds", default=2.0),
    )
    if gemini_retry_base_delay_seconds <= 0:
        raise ValueError("geminiRetryBaseDelaySeconds must be > 0")

    gemini_requests_per_minute = int(
        pick(config, "geminiRequestsPerMinute", "gemini_requests_per_minute", default=5),
    )
    if gemini_requests_per_minute <= 0:
        raise ValueError("geminiRequestsPerMinute must be > 0")

    gemini_rate_limit_window_seconds = int(
        pick(
            config,
            "geminiRateLimitWindowSeconds",
            "gemini_rate_limit_window_seconds",
            default=60,
        ),
    )
    if gemini_rate_limit_window_seconds <= 0:
        raise ValueError("geminiRateLimitWindowSeconds must be > 0")

    disable_gemini_on_rate_limit = bool(
        pick(config, "disableGeminiOnRateLimit", "disable_gemini_on_rate_limit", default=True),
    )

    gemini_api_key = pick(config, "geminiApiKey", "gemini_api_key", default=os.getenv("GEMINI_API_KEY"))
    if not isinstance(gemini_api_key, str) or not gemini_api_key.strip():
        raise ValueError("geminiApiKey is required (config key or GEMINI_API_KEY env var)")

    return ScraperSettings(
        urls=normalize_urls(pick(config, "urls")),
        detailed_output=bool(pick(config, "detailedOutput", "detailed_output", default=False)),
        start_date=pick(config, "startDate", "start_date"),
        telegram_token=pick(config, "telegramToken", "telegram_token"),
        telegram_chat_id=pick(config, "telegramChatId", "telegram_chat_id"),
        output_file=Path(pick(config, "outputFile", "output_file", default="output/offers.jsonl")),
        state_file=Path(pick(config, "stateFile", "state_file", default="state/seen_offers.json")),
        request_delay_seconds=request_delay_seconds,
        gemini_api_key=gemini_api_key.strip(),
        gemini_model=str(pick(config, "geminiModel", "gemini_model", default="gemini-2.0-flash")),
        min_profit_czk=min_profit_czk,
        gemini_min_interval_seconds=gemini_min_interval_seconds,
        gemini_max_retries=gemini_max_retries,
        gemini_retry_base_delay_seconds=gemini_retry_base_delay_seconds,
        gemini_requests_per_minute=gemini_requests_per_minute,
        gemini_rate_limit_window_seconds=gemini_rate_limit_window_seconds,
        disable_gemini_on_rate_limit=disable_gemini_on_rate_limit,
        only_very_undervalued=bool(
            pick(config, "onlyVeryUndervalued", "only_very_undervalued", default=False),
        ),
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
