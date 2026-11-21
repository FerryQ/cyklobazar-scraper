from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scraper import Offer, OfferValuation

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    price_raw TEXT NOT NULL,
    price_czk INTEGER,
    location TEXT NOT NULL,
    description TEXT NOT NULL,
    created TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    valuation TEXT,
    valued_at TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
    listing_id TEXT NOT NULL REFERENCES listings (id),
    price_czk INTEGER,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (listing_id, seen_at)
);
"""


class OfferDatabase:
    """Stores every scraped listing, so prices can later be compared across the market."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.executescript(SCHEMA)

    def __enter__(self) -> OfferDatabase:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._conn.close()

    def record_offer(self, listing_id: str, offer: Offer, price_czk: int | None) -> None:
        now = utc_now()
        with self._conn:
            row = self._conn.execute(
                "SELECT price_czk FROM listings WHERE id = ?",
                (listing_id,),
            ).fetchone()

            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO listings (
                        id, url, title, price_raw, price_czk, location,
                        description, created, first_seen, last_seen
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        listing_id,
                        offer.url,
                        offer.title,
                        offer.price,
                        price_czk,
                        offer.location,
                        offer.description,
                        offer.created,
                        now,
                        now,
                    ),
                )
                self._record_price(listing_id, price_czk, now)
                return

            # Listing cards carry a truncated description, so keep the longest one seen
            # (typically the full text from the detail page).
            self._conn.execute(
                """
                UPDATE listings SET
                    url = ?,
                    title = ?,
                    price_raw = ?,
                    price_czk = ?,
                    location = ?,
                    description = CASE
                        WHEN length(?) > length(description) THEN ? ELSE description
                    END,
                    created = COALESCE(?, created),
                    last_seen = ?
                WHERE id = ?
                """,
                (
                    offer.url,
                    offer.title,
                    offer.price,
                    price_czk,
                    offer.location,
                    offer.description,
                    offer.description,
                    offer.created,
                    now,
                    listing_id,
                ),
            )
            if row[0] != price_czk:
                self._record_price(listing_id, price_czk, now)

    def save_valuation(self, listing_id: str, valuation: OfferValuation) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE listings SET valuation = ?, valued_at = ? WHERE id = ?",
                (json.dumps(valuation.to_dict(), ensure_ascii=False), utc_now(), listing_id),
            )

    def _record_price(self, listing_id: str, price_czk: int | None, seen_at: str) -> None:
        self._conn.execute(
            "INSERT INTO price_history (listing_id, price_czk, seen_at) VALUES (?, ?, ?)",
            (listing_id, price_czk, seen_at),
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
