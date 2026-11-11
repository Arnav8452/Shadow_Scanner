"""Telegram alerts via the Bot API. Token comes from ``TELEGRAM_BOT_TOKEN`` and is never logged.

Without a token or chat id the notifier runs in dry-run mode: messages are logged, not sent.
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from engine.core.env import Secret, get_secret
from engine.core.logging_setup import redact

log = logging.getLogger(__name__)
MAX_LEN = 4000  # Telegram limit is 4096 characters

Poster = Callable[[str, bytes, float], int]  # (url, form body, timeout) -> HTTP status


def _urllib_post(url: str, body: bytes, timeout: float) -> int:
    req = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status: int = resp.status
        return status


@dataclass
class TelegramNotifier:
    token: Secret
    chat_id: str
    poster: Poster = _urllib_post
    timeout: float = 15.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TelegramNotifier:
        tok = get_secret("TELEGRAM_BOT_TOKEN", environ)
        chat = get_secret("TELEGRAM_CHAT_ID", environ).reveal()
        return cls(token=tok, chat_id=chat)

    @property
    def dry_run(self) -> bool:
        return not (self.token and self.chat_id)

    def send(self, text: str) -> bool:
        chunks = [text[i : i + MAX_LEN] for i in range(0, len(text), MAX_LEN)] or [""]
        if self.dry_run:
            for c in chunks:
                log.info("[telegram dry-run] %s", c)
            return False
        url = f"https://api.telegram.org/bot{self.token.reveal()}/sendMessage"
        ok = True
        for c in chunks:
            body = urllib.parse.urlencode(
                {"chat_id": self.chat_id, "text": c, "disable_web_page_preview": "true"}
            ).encode()
            try:
                status = self.poster(url, body, self.timeout)
            except Exception as exc:  # network errors must not leak the token in the URL
                log.error("telegram send failed: %s", redact(str(exc), [self.token.reveal()]))
                ok = False
                continue
            if status != 200:
                log.error("telegram send failed: HTTP %s", status)
                ok = False
        return ok


def format_digest(title: str, lines: list[str]) -> str:
    return title + "\n" + "\n".join(f"• {line}" for line in lines) if lines else title
