"""Нормализация контакта поддержки (username / t.me / URL) в ссылку и подпись."""

from __future__ import annotations

import html
import re
from typing import Optional

_TG_HOSTS = ("t.me/", "telegram.me/", "telegram.dog/")


def _clean(contact: Optional[str]) -> str:
    return (contact or "").strip()


def support_url(contact: Optional[str]) -> Optional[str]:
    contact = _clean(contact)
    if not contact:
        return None

    if contact.startswith(("http://", "https://", "tg://")):
        return contact

    without_prefix = contact.lstrip("@")

    if without_prefix.startswith(_TG_HOSTS):
        return f"https://{without_prefix}"

    if contact.startswith(_TG_HOSTS):
        return f"https://{contact}"

    if "." in without_prefix:
        return f"https://{without_prefix}"

    if without_prefix:
        return f"https://t.me/{without_prefix}"

    return None


def support_display(contact: Optional[str]) -> str:
    contact = _clean(contact)
    if not contact:
        return ""

    if contact.startswith("@"):
        return contact

    if contact.startswith(("http://", "https://", "tg://")):
        return contact

    if contact.startswith(_TG_HOSTS):
        url = support_url(contact)
        return url if url else contact

    without_prefix = contact.lstrip("@")

    if "." in without_prefix:
        url = support_url(contact)
        return url if url else contact

    if re.fullmatch(r"[A-Za-z0-9_]{3,}", without_prefix):
        return f"@{without_prefix}"

    return contact


def support_display_html(contact: Optional[str]) -> str:
    return html.escape(support_display(contact))
