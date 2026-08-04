from __future__ import annotations

from typing import Optional


INTERFACE_LANGUAGES = ("ru", "en")


def resolve_telegram_language(language_code: Optional[str]) -> str:
    """Map a Telegram interface locale to a supported bot locale."""
    normalized = (language_code or "").strip().lower().replace("_", "-")
    return "ru" if normalized.split("-", 1)[0] == "ru" else "en"
