from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def load_seen_offers(path: Path, prune_after_days: int) -> dict[str, str]:
    if not path.exists():
        return {}

    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"State file must contain an object: {path}")

    cutoff = datetime.now(timezone.utc) - timedelta(days=prune_after_days)
    pruned: dict[str, str] = {}

    for offer_id, seen_at in loaded.items():
        if not isinstance(offer_id, str) or not isinstance(seen_at, str):
            continue

        parsed = parse_iso_datetime(seen_at)
        if parsed and parsed >= cutoff:
            pruned[offer_id] = seen_at

    return pruned


def save_seen_offers(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def parse_iso_datetime(value: str) -> datetime | None:
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
