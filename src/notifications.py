from __future__ import annotations

import requests


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    # The bot token is part of the URL, so scrub it from errors before they reach the logs.
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise requests.RequestException(str(exc).replace(token, "***")) from None

    if not response.ok:
        # Telegram explains failures in the body, e.g. "chat not found" or "bot was blocked by the user".
        raise requests.HTTPError(f"Telegram API error {response.status_code}: {response.text}")
