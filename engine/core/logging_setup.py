"""Logging from ``ops/logging.yaml`` with a filter that masks secrets in every record."""

from __future__ import annotations

import logging
import logging.config
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

import yaml

MASK = "****"

# Env var names whose values are treated as secrets.
_SECRET_NAME = re.compile(r"(TOKEN|SECRET|KEY|PASSWORD|PIN|TOTP|AUTH)", re.IGNORECASE)

# Patterns that look like credentials regardless of where they came from.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(access_token|refresh_token|api_key|api_secret|secret_key|password|totp|"
        r"auth_code|request_token|client_secret|token)\b(\s*[=:]\s*)(['\"]?)[^\s'\",&]{4,}"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
)


def _secret_values(environ: Mapping[str, str]) -> list[str]:
    vals = {v for k, v in environ.items() if _SECRET_NAME.search(k) and len(v) >= 6}
    return sorted(vals, key=len, reverse=True)


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    for value in [*extra_secrets, *_secret_values(os.environ)]:
        if value:
            text = text.replace(value, MASK)
    text = _PATTERNS[0].sub(lambda m: f"{m.group(1)} {MASK}", text)
    text = _PATTERNS[1].sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}", text)
    text = _PATTERNS[2].sub(MASK, text)
    return text


class RedactingFilter(logging.Filter):
    """Renders the message, masks secrets, and freezes the result into the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed %-args: still log something safe
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def setup_logging(config_path: Path, *, log_dir: Path | None = None) -> None:
    doc = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    for handler in doc.get("handlers", {}).values():
        filename = handler.get("filename")
        if filename:
            path = Path(filename)
            if log_dir is not None:
                path = log_dir / path.name
            elif not path.is_absolute():
                path = config_path.resolve().parents[1] / path
            path.parent.mkdir(parents=True, exist_ok=True)
            handler["filename"] = str(path)
    logging.config.dictConfig(doc)
