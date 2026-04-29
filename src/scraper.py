from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from notifications import send_telegram_message
from state import load_seen_offers, save_seen_offers

LOGGER = logging.getLogger(__name__)
HTTP_TIMEOUT_SECONDS = 20
PRUNE_AFTER_DAYS = 30


@dataclass
class Offer:
    title: str
    price: str
    description: str
    location: str
    created: str | None
    url: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "title": self.title,
            "price": self.price,
            "location": self.location,
            "created": self.created,
            "description": self.description,
            "url": self.url,
        }


@dataclass
class ScraperSettings:
    urls: list[str]
    detailed_output: bool
    start_date: str | None
    telegram_token: str | None
    telegram_chat_id: str | None
    output_file: Path
    state_file: Path
    request_delay_seconds: float = 0.0


class CyklobazarScraper:
    def __init__(self, settings: ScraperSettings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "cyklobazar-fanda/1.0 (+https://www.cyklobazar.cz/)",
            },
        )
        self.start_date = parse_iso_date(settings.start_date) if settings.start_date else None
        self.seen_offers = load_seen_offers(settings.state_file, PRUNE_AFTER_DAYS)
        LOGGER.info("Loaded %s seen offers.", len(self.seen_offers))
        self._output_handle: TextIO | None = None
        self._scraped_count = 0

    def run(self) -> int:
        self.settings.output_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings.state_file.parent.mkdir(parents=True, exist_ok=True)

        with self.settings.output_file.open("a", encoding="utf-8") as output_handle:
            self._output_handle = output_handle
            try:
                for url in self.settings.urls:
                    self._crawl_listing_pages(url)
            finally:
                save_seen_offers(self.settings.state_file, self.seen_offers)
                self._output_handle = None

        return self._scraped_count

    def _crawl_listing_pages(self, start_url: str) -> None:
        queue: list[str] = [start_url]
        visited: set[str] = set()

        while queue:
            current_url = queue.pop(0)
            if current_url in visited:
                continue
            visited.add(current_url)

            soup = self._fetch_soup(current_url)
            offers = scrape_listing_offer_infos(soup, self.start_date, current_url)

            if offers:
                LOGGER.info("Found %s offers on %s", len(offers), current_url)
            else:
                LOGGER.info("No matching offers on %s", current_url)

            if self.settings.detailed_output:
                for offer in offers:
                    detailed_offer = self._fetch_offer_details(offer.url)
                    self._emit_offer(detailed_offer)
            else:
                for offer in offers:
                    self._emit_offer(offer)

            next_page = scrape_next_page(soup, current_url)
            if next_page and next_page not in visited:
                queue.append(next_page)

    def _fetch_soup(self, url: str) -> BeautifulSoup:
        LOGGER.info("Fetching %s", url)
        response = self.session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        self._rate_limit()
        return BeautifulSoup(response.text, "html.parser")

    def _fetch_offer_details(self, url: str) -> Offer:
        soup = self._fetch_soup(url)
        created_raw = attr_or_empty(soup.select_one(".cb-time-ago"), "title")
        created_date = parse_czech_date(created_raw)

        return Offer(
            title=text_or_empty(soup.select_one(".offer-detail__header h1")),
            price=text_or_empty(soup.select_one(".cb-seller-box__price")),
            description=text_or_empty(soup.select_one(".offer-detail__desc")),
            location=text_or_empty(soup.select_one(".cb-seller-box__location")),
            created=created_date.isoformat() if created_date else None,
            url=url,
        )
    # in this function add LLM parsing
    def _emit_offer(self, offer: Offer) -> None:
        if self._output_handle is None:
            raise RuntimeError("Output handle is not initialized")

        json.dump(offer.to_dict(), self._output_handle, ensure_ascii=False)
        self._output_handle.write("\n")
        self._scraped_count += 1

        offer_id = urlparse(offer.url).path
        was_seen = offer_id in self.seen_offers
        self.seen_offers[offer_id] = datetime.now(timezone.utc).isoformat()
        if was_seen:
            LOGGER.info("Already seen, skipping Telegram: %s", offer_id)
            return

        if self.settings.telegram_token and self.settings.telegram_chat_id:
            self._send_telegram_offer(offer)

    def _send_telegram_offer(self, offer: Offer) -> None:
        created_date = iso_to_datetime(offer.created)
        created_str = created_date.strftime("%d.%m.%Y") if created_date else "nezadano"
        message = (
            f"{offer.title}\n"
            f"Cena: {offer.price}\n"
            f"Misto: {offer.location}\n"
            f"Vytvoreno: {created_str}\n"
            f"{offer.url}"
        )
        try:
            send_telegram_message(
                token=self.settings.telegram_token or "",
                chat_id=self.settings.telegram_chat_id or "",
                text=message,
            )
        except requests.RequestException as exc:
            LOGGER.error("Failed to send Telegram message for %s: %s", offer.url, exc)

    def _rate_limit(self) -> None:
        if self.settings.request_delay_seconds > 0:
            time.sleep(self.settings.request_delay_seconds)


def parse_iso_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"Invalid start date '{value}'. Use YYYY-MM-DD.") from exc


def parse_czech_date(text: str) -> datetime | None:
    match = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", text)
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    return datetime(year, month, day, tzinfo=timezone.utc)


def scrape_next_page(soup: BeautifulSoup, base_url: str) -> str | None:
    next_link = soup.select_one('.paginator__item--next a[href*="vp-page="]')
    if not isinstance(next_link, Tag):
        return None
    href = next_link.get("href")
    if not isinstance(href, str):
        return None
    return urljoin(base_url, href)


def scrape_listing_offer_infos(
    soup: BeautifulSoup,
    start_date: datetime | None,
    base_url: str,
) -> list[Offer]:
    offers: list[Offer] = []
    for anchor in soup.select('a.cb-offer[href*="/inzerat/"]'):
        if not isinstance(anchor, Tag):
            continue
        if has_ancestor_with_class(anchor, "cb-offer-list--vertical"):
            continue

        published_raw = attr_or_empty(anchor.select_one(".cb-time-ago"), "title")
        published_date = parse_czech_date(published_raw)
        if start_date and published_date and published_date < start_date:
            continue

        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        offer_url = urljoin(base_url, href)

        offers.append(
            Offer(
                url=offer_url,
                title=text_or_empty(anchor.select_one(".cb-offer__header h4")),
                price=text_or_empty(anchor.select_one(".cb-offer__price")),
                description=text_or_empty(anchor.select_one(".cb-offer__desc")),
                location=text_or_empty(anchor.select_one(".cb-offer__tag-location")),
                created=published_date.isoformat() if published_date else None,
            ),
        )
    return offers


def has_ancestor_with_class(node: Tag, class_name: str) -> bool:
    for parent in node.parents:
        if isinstance(parent, Tag):
            classes = parent.get("class", [])
            if isinstance(classes, list) and class_name in classes:
                return True
    return False


def text_or_empty(node: Tag | None) -> str:
    if not isinstance(node, Tag):
        return ""
    return node.get_text(strip=True)


def attr_or_empty(node: Tag | None, attr_name: str) -> str:
    if not isinstance(node, Tag):
        return ""
    value = node.get(attr_name)
    return value if isinstance(value, str) else ""


def iso_to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)
