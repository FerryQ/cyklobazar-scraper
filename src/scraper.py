from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from db import OfferDatabase
from notifications import send_telegram_message
from state import load_seen_offers, save_seen_offers

LOGGER = logging.getLogger(__name__)
HTTP_TIMEOUT_SECONDS = 20
PRUNE_AFTER_DAYS = 30
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
HTTP_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
GEMINI_SYSTEM_INSTRUCTION = (
    "Jsi konzervativni odhadce ceny ojetych kol pro cesky trh. "
    "Tvuj ukol je realisticky odhadnout ferovou trzni cenu, ne maximalistickou cenu. "
    "Pouzivej opatrne predpoklady: bez dukazu nepredpokladej top stav, servis ani premium komponenty. "
    "Pokud je informace nejista nebo chybi, sniz odhad a confidence. "
    "Vyhni se optimismu, zejmena u starsich kol a nejasneho stavu. "
    "Prioritizuj odhad, ktery by byl dosazitelny pri realnem rychlem prodeji, ne idealni nabidkove cene. "
    "Vrat pouze JSON objekt se schema: "
    "verdict, estimatedMarketPriceCzk, confidence, reasoning."
)


@dataclass
class OfferValuation:
    verdict: str
    estimated_market_price_czk: int | None
    listing_price_czk: int | None
    expected_profit_czk: int | None
    confidence: str
    reasoning: str

    def is_very_undervalued(self, min_profit_czk: int) -> bool:
        return (
            self.verdict == "undervalued"
            and self.expected_profit_czk is not None
            and self.expected_profit_czk >= min_profit_czk
        )

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "verdict": self.verdict,
            "estimatedMarketPriceCzk": self.estimated_market_price_czk,
            "listingPriceCzk": self.listing_price_czk,
            "expectedProfitCzk": self.expected_profit_czk,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
        }


@dataclass
class Offer:
    title: str
    price: str
    description: str
    location: str
    created: str | None
    url: str
    valuation: OfferValuation | None = None

    def to_dict(self) -> dict[str, str | int | None | dict[str, str | int | None]]:
        data: dict[str, str | int | None | dict[str, str | int | None]] = {
            "title": self.title,
            "price": self.price,
            "location": self.location,
            "created": self.created,
            "description": self.description,
            "url": self.url,
        }
        if self.valuation:
            data["valuation"] = self.valuation.to_dict()
        return data


@dataclass
class ScraperSettings:
    urls: list[str]
    detailed_output: bool
    start_date: str | None
    telegram_token: str | None
    telegram_chat_id: str | None
    output_file: Path
    state_file: Path
    database_file: Path = Path("data/offers.db")
    max_age_days: int | None = None
    max_pages: int = 20
    request_delay_seconds: float = 0.0
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    min_profit_czk: int = 10_000
    gemini_min_interval_seconds: float = 1.5
    gemini_max_retries: int = 1
    gemini_retry_base_delay_seconds: float = 2.0
    gemini_requests_per_minute: int = 5
    gemini_rate_limit_window_seconds: int = 60
    disable_gemini_on_rate_limit: bool = True
    only_very_undervalued: bool = False


class CyklobazarScraper:
    def __init__(self, settings: ScraperSettings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "cyklobazar-fanda/1.0 (+https://www.cyklobazar.cz/)",
            },
        )
        self.cutoff = resolve_cutoff(settings.start_date, settings.max_age_days)
        self.seen_offers = load_seen_offers(settings.state_file, PRUNE_AFTER_DAYS)
        LOGGER.info("Loaded %s seen offers.", len(self.seen_offers))
        self._output_handle: TextIO | None = None
        self._db: OfferDatabase | None = None
        self._scraped_count = 0
        self._next_gemini_request_at = 0.0
        self._gemini_request_times: deque[float] = deque()
        self._gemini_disabled_reason: str | None = None

    def run(self) -> int:
        self.settings.output_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings.state_file.parent.mkdir(parents=True, exist_ok=True)

        with (
            self.settings.output_file.open("a", encoding="utf-8") as output_handle,
            OfferDatabase(self.settings.database_file) as db,
        ):
            self._output_handle = output_handle
            self._db = db
            try:
                for url in self.settings.urls:
                    try:
                        self._crawl_listing_pages(url)
                    except requests.RequestException as exc:
                        LOGGER.error("Failed to crawl %s (blocked by Cloudflare?): %s", url, exc)
            finally:
                save_seen_offers(self.settings.state_file, self.seen_offers)
                self._output_handle = None
                self._db = None

        return self._scraped_count

    def _crawl_listing_pages(self, start_url: str) -> None:
        current_url: str | None = start_url
        visited: set[str] = set()

        while current_url and current_url not in visited:
            if len(visited) >= self.settings.max_pages:
                LOGGER.info("Reached maxPages=%s, stopping at %s", self.settings.max_pages, current_url)
                return
            visited.add(current_url)

            soup = self._fetch_soup(current_url)
            offers = scrape_listing_offer_infos(soup, current_url)
            recent_offers = [offer for offer in offers if self._is_recent(offer)]
            LOGGER.info("Found %s offers (%s recent) on %s", len(offers), len(recent_offers), current_url)

            # Record every listing on the page, old ones included: they are comparables for valuation.
            for offer in offers:
                self._record_offer(offer)

            for offer in recent_offers:
                listing_id = offer_id(offer.url)
                if listing_id in self.seen_offers:
                    # Refresh, so offers that stay listed are not pruned and notified again.
                    self.seen_offers[listing_id] = datetime.now(timezone.utc).isoformat()
                    LOGGER.info("Already seen, skipping: %s", offer.url)
                    continue
                if self.settings.detailed_output:
                    try:
                        offer = self._fetch_offer_details(offer.url)
                    except requests.RequestException as exc:
                        LOGGER.warning("Failed to fetch details for %s, will retry next run: %s", offer.url, exc)
                        continue
                    self._record_offer(offer)
                self._emit_offer(offer)

            # Listings are sorted newest first, so a page with nothing recent means the rest is older too.
            if offers and not recent_offers:
                LOGGER.info("No recent offers on %s, stopping pagination.", current_url)
                return

            current_url = scrape_next_page(soup, current_url)

    def _is_recent(self, offer: Offer) -> bool:
        if self.cutoff is None:
            return True
        created = iso_to_datetime(offer.created)
        return created is None or created >= self.cutoff

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

    def _record_offer(self, offer: Offer) -> None:
        if self._db is None:
            raise RuntimeError("Database is not initialized")
        self._db.record_offer(offer_id(offer.url), offer, parse_czk_price(offer.price))

    def _emit_offer(self, offer: Offer) -> None:
        if self._output_handle is None or self._db is None:
            raise RuntimeError("Output handle is not initialized")

        listing_id = offer_id(offer.url)
        valuation, valuation_ok = self._value_offer(offer)
        offer.valuation = valuation
        if valuation_ok:
            self._db.save_valuation(listing_id, valuation)

        if self.settings.only_very_undervalued and not valuation.is_very_undervalued(
            self.settings.min_profit_czk,
        ):
            if not valuation_ok:
                # Don't mark as seen, otherwise a failed valuation would hide the offer forever.
                LOGGER.info("Valuation failed for %s, will retry next run.", offer.url)
                return
            self.seen_offers[listing_id] = datetime.now(timezone.utc).isoformat()
            LOGGER.info(
                "Skipping not-undervalued offer %s: verdict=%s expectedProfit=%s CZK",
                offer.url,
                valuation.verdict,
                valuation.expected_profit_czk,
            )
            return

        self.seen_offers[listing_id] = datetime.now(timezone.utc).isoformat()
        json.dump(offer.to_dict(), self._output_handle, ensure_ascii=False)
        self._output_handle.write("\n")
        self._scraped_count += 1

        if self.settings.telegram_token and self.settings.telegram_chat_id:
            self._send_telegram_offer(offer)

    def _send_telegram_offer(self, offer: Offer) -> None:
        if not offer.valuation:
            raise ValueError("Offer valuation is missing")

        created_date = iso_to_datetime(offer.created)
        created_str = created_date.strftime("%d.%m.%Y") if created_date else "nezadano"
        estimated_str = format_czk(offer.valuation.estimated_market_price_czk)
        profit_str = format_czk(offer.valuation.expected_profit_czk)

        message = (
            f"{offer.title}\n"
            f"Cena: {offer.price}\n"
            f"Misto: {offer.location}\n"
            f"Vytvoreno: {created_str}\n"
            f"Ocenění: {offer.valuation.verdict}\n"
            f"Odhad trzni ceny: {estimated_str}\n"
            f"Ocekavany zisk: {profit_str}\n"
            f"Duvod: {offer.valuation.reasoning}\n"
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

    def _value_offer(self, offer: Offer) -> tuple[OfferValuation, bool]:
        """Return the valuation and whether it succeeded (failed ones get verdict 'unknown')."""
        if self._gemini_disabled_reason:
            return self._build_unknown_valuation(offer, self._gemini_disabled_reason), False
        try:
            return self._evaluate_offer_value(offer), True
        except (requests.RequestException, ValueError) as exc:
            LOGGER.warning("Failed to value offer %s, continuing with unknown valuation: %s", offer.url, exc)
            return self._build_unknown_valuation(offer, str(exc)), False

    def _evaluate_offer_value(self, offer: Offer) -> OfferValuation:
        listed_price = parse_czk_price(offer.price)
        if listed_price is None:
            raise ValueError(f"Could not parse CZK price from '{offer.price}'")

        prompt = (
            "Vyhodnot cenu kola pouze na základě titulku, popisu a uvedené ceny inzerátu. "
            "Vrat pouze JSON objekt bez dalsiho textu s poli: "
            "verdict (jedno z: undervalued, fairly_priced, overvalued, unknown; unknown pouzij, pokud nelze cenu spolehlive vyhodnotit), "
            "estimatedMarketPriceCzk (integer), confidence (low|medium|high), reasoning (krátký text).\n\n"
            f"Titulek: {offer.title}\n"
            f"Popis: {offer.description}\n"
            f"Uvedena cena CZK: {listed_price}\n"
        )
        payload = self._call_gemini_with_retries(prompt)

        model_text = extract_gemini_text(payload)
        parsed = parse_json_object_from_text(model_text)

        verdict_raw = str(parsed.get("verdict", "unknown")).strip().lower()
        if verdict_raw not in {"undervalued", "fairly_priced", "overvalued"}:
            verdict_raw = "unknown"

        estimated_price = int_or_none(parsed.get("estimatedMarketPriceCzk"))
        # Compute the profit ourselves from the known listed price instead of trusting the model's arithmetic.
        expected_profit = estimated_price - listed_price if estimated_price is not None else None
        confidence = str(parsed.get("confidence", "low")).strip().lower()
        reasoning = str(parsed.get("reasoning", "")).strip()
        if not reasoning:
            reasoning = "Bez vysvětlení"

        return OfferValuation(
            verdict=verdict_raw,
            estimated_market_price_czk=estimated_price,
            listing_price_czk=listed_price,
            expected_profit_czk=expected_profit,
            confidence=confidence,
            reasoning=reasoning,
        )

    def _call_gemini_with_retries(self, prompt: str) -> dict[str, Any]:
        url = f"{GEMINI_API_BASE_URL}/{self.settings.gemini_model}:generateContent"
        for attempt in range(self.settings.gemini_max_retries + 1):
            self._wait_for_gemini_slot()
            try:
                response = requests.post(
                    url,
                    # Header, not a URL param: request errors include the URL and end up in logs.
                    headers={"x-goog-api-key": self.settings.gemini_api_key},
                    timeout=HTTP_TIMEOUT_SECONDS,
                    json={
                        "systemInstruction": {"parts": [{"text": GEMINI_SYSTEM_INSTRUCTION}]},
                        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": 0.2,
                            "responseMimeType": "application/json",
                        },
                    },
                )
            except requests.RequestException:
                if attempt >= self.settings.gemini_max_retries:
                    raise
                backoff_seconds = self.settings.gemini_retry_base_delay_seconds * (2**attempt)
                LOGGER.warning(
                    "Gemini request failed (attempt %s/%s). Retrying in %.1fs.",
                    attempt + 1,
                    self.settings.gemini_max_retries + 1,
                    backoff_seconds,
                )
                time.sleep(backoff_seconds)
                continue

            if response.status_code in HTTP_RETRYABLE_STATUS_CODES:
                if attempt >= self.settings.gemini_max_retries:
                    if response.status_code == 429 and self.settings.disable_gemini_on_rate_limit:
                        self._gemini_disabled_reason = (
                            "Gemini disabled for this run after repeated 429 rate limits"
                        )
                        raise ValueError(self._gemini_disabled_reason)
                    response.raise_for_status()
                retry_after = parse_retry_after_seconds(response.headers.get("Retry-After"))
                backoff_seconds = retry_after or (self.settings.gemini_retry_base_delay_seconds * (2**attempt))
                LOGGER.warning(
                    "Gemini rate-limited/status=%s (attempt %s/%s). Retrying in %.1fs.",
                    response.status_code,
                    attempt + 1,
                    self.settings.gemini_max_retries + 1,
                    backoff_seconds,
                )
                time.sleep(backoff_seconds)
                continue

            response.raise_for_status()
            return response.json()

        raise RuntimeError("Gemini retry loop exited unexpectedly")

    def _wait_for_gemini_slot(self) -> None:
        rate_window = float(self.settings.gemini_rate_limit_window_seconds)
        # `gemini_requests_per_minute` is a legacy setting name; the limit is
        # applied over the configurable `gemini_rate_limit_window_seconds` window.
        requests_per_window = self.settings.gemini_requests_per_minute
        while True:
            now = time.monotonic()
            while self._gemini_request_times and (now - self._gemini_request_times[0]) >= rate_window:
                self._gemini_request_times.popleft()

            if len(self._gemini_request_times) < requests_per_window:
                break

            wait_seconds = rate_window - (now - self._gemini_request_times[0])
            if wait_seconds > 0:
                LOGGER.info(
                    "Gemini limit reached (%s requests per %ss). Waiting %.1fs.",
                    requests_per_window,
                    self.settings.gemini_rate_limit_window_seconds,
                    wait_seconds,
                )
                time.sleep(wait_seconds)

        now = time.monotonic()
        if now < self._next_gemini_request_at:
            time.sleep(self._next_gemini_request_at - now)

        request_time = time.monotonic()
        self._next_gemini_request_at = request_time + self.settings.gemini_min_interval_seconds
        self._gemini_request_times.append(request_time)

    def _build_unknown_valuation(self, offer: Offer, reason: str) -> OfferValuation:
        return OfferValuation(
            verdict="unknown",
            estimated_market_price_czk=None,
            listing_price_czk=parse_czk_price(offer.price),
            expected_profit_czk=None,
            confidence="low",
            reasoning=f"Gemini valuation unavailable: {reason}",
        )


def parse_iso_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"Invalid start date '{value}'. Use YYYY-MM-DD.") from exc


def resolve_cutoff(start_date: str | None, max_age_days: int | None) -> datetime | None:
    """Offers created before the returned moment are ignored; the stricter of the two settings wins."""
    candidates: list[datetime] = []
    if start_date:
        candidates.append(parse_iso_date(start_date))
    if max_age_days is not None:
        candidates.append(datetime.now(timezone.utc) - timedelta(days=max_age_days))
    return max(candidates, default=None)


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


def scrape_listing_offer_infos(soup: BeautifulSoup, base_url: str) -> list[Offer]:
    offers: list[Offer] = []
    for anchor in soup.select('a.cb-offer[href*="/inzerat/"]'):
        if not isinstance(anchor, Tag):
            continue
        # Promoted/recommended offers are rendered as vertical cards; they ignore the category and date filters.
        if "cb-offer--vertical" in (anchor.get("class") or []):
            continue

        published_raw = attr_or_empty(anchor.select_one(".cb-time-ago"), "title")
        published_date = parse_czech_date(published_raw)

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


def offer_id(url: str) -> str:
    return urlparse(url).path


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


def parse_czk_price(value: str) -> int | None:
    digits = re.sub(r"[^\d]", "", value)
    if not digits:
        return None
    return int(digits)


def format_czk(value: int | None) -> str:
    if value is None:
        return "nezname"
    grouped = f"{value:,}".replace(",", " ")
    return f"{grouped} CZK"


def int_or_none(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        parsed = parse_czk_price(value)
        if parsed is not None:
            return parsed
    return None


def extract_gemini_text(response_payload: dict[str, Any]) -> str:
    candidates = response_payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Gemini response did not contain candidates")
    first_candidate = candidates[0]
    if not isinstance(first_candidate, dict):
        raise ValueError("Gemini response candidate has invalid format")

    content = first_candidate.get("content")
    if not isinstance(content, dict):
        raise ValueError("Gemini response candidate missing content")

    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("Gemini response content missing parts")

    text_chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                text_chunks.append(text)

    if not text_chunks:
        raise ValueError("Gemini response parts do not contain text")
    return "\n".join(text_chunks)


def parse_json_object_from_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < 0 or end <= start:
        raise ValueError(f"Gemini response does not contain a JSON object: {text}")

    return json.loads(cleaned[start : end + 1])


def parse_retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds
