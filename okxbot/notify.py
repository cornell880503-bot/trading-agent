"""Outbound notifications.

Console always. Telegram if ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` are
set -- useful once ``sync`` runs from cron and nobody is watching the terminal.

A notification failure never propagates: losing an alert is bad, but crashing
the process that was about to place a protective stop is worse.
"""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


def _telegram_config() -> tuple[str, str] | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return (token, chat_id) if token and chat_id else None


def notify(title: str, body: str = "", level: str = "info") -> None:
    marker = {"info": "·", "warn": "!", "error": "x"}.get(level, "·")
    print(f"\n[{marker}] {title}")
    if body:
        print(body)

    config = _telegram_config()
    if not config:
        return
    token, chat_id = config
    text = f"*{title}*\n```\n{body}\n```" if body else f"*{title}*"
    try:
        response = requests.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000], "parse_mode": "Markdown"},
            timeout=10,
        )
        if response.status_code >= 400:
            log.warning("telegram rejected the message: HTTP %s", response.status_code)
    except requests.RequestException as exc:
        log.warning("telegram notification failed: %s", exc)
