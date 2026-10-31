"""Discord and Telegram notifications. A failed notification never stops a trade."""

from __future__ import annotations

import logging

import httpx

from .config import Settings

log = logging.getLogger(__name__)

DISCORD_LIMIT = 2000
TELEGRAM_LIMIT = 4096


class Notifier:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self._settings = settings
        self._client = client or httpx.Client(timeout=10)

    @property
    def channels(self) -> list[str]:
        s = self._settings
        out = []
        if s.discord_webhook_url:
            out.append("discord")
        if s.telegram_bot_token and s.telegram_chat_id:
            out.append("telegram")
        return out

    def format(self, title: str, lines: list[str]) -> str:
        tag = "LIVE" if self._settings.is_live else "PAPER"
        return "\n".join([f"[{tag}] {title}", *lines])

    def send(self, title: str, lines: list[str] | None = None) -> list[str]:
        """Send to every configured channel. Returns the channels that failed."""
        text = self.format(title, lines or [])
        failed = []
        for channel in self.channels:
            try:
                if channel == "discord":
                    self._post(self._settings.discord_webhook_url, {"content": text[:DISCORD_LIMIT]})
                else:
                    url = f"https://api.telegram.org/bot{self._settings.telegram_bot_token}/sendMessage"
                    self._post(url, {"chat_id": self._settings.telegram_chat_id, "text": text[:TELEGRAM_LIMIT]})
            except httpx.HTTPError as exc:
                # Don't log the exception text: the Telegram URL contains the bot token.
                reason = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
                log.warning("%s notification failed: %s", channel, reason)
                failed.append(channel)
        return failed

    def _post(self, url: str, payload: dict) -> None:
        self._client.post(url, json=payload).raise_for_status()
