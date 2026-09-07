"""Подстановка бренда витрины в тексты.

Один код обслуживает несколько ботов-копикэтов. Тексты в локалях написаны
с "Leto", а здесь имя заменяется на бренд конкретной витрины — так копикэту
не нужен ни свой набор локалей, ни своя ветка.

Замена регистрозависимая по форме, а не по совпадению: "LETO" остаётся
капсом, "leto" — строчным, "Leto" — с заглавной. Домены и служебные
идентификаторы (letovpn.com, com.letovpn.ios, @letovpn_bot) не трогаем:
переименование сломало бы ссылку или bundle id.
"""

from __future__ import annotations

import re

# "Leto" как отдельное слово, но не когда за ним идёт домен/идентификатор
# (letovpn.com, com.letovpn.ios) и не внутри @username.
_LETO_RE = re.compile(
    r"(?<![\w@./-])leto(?![\w.-])",
    re.IGNORECASE,
)


def _match_case(source: str, replacement: str) -> str:
    if source.isupper():
        return replacement.upper()
    if source.islower():
        return replacement.lower()
    if source[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def apply_brand(value: str, brand: str) -> str:
    """Заменить имя приложения в готовой строке."""
    if not value or not brand:
        return value
    return _LETO_RE.sub(lambda m: _match_case(m.group(0), brand), value)


def brand_text(value):
    """Применить бренд текущей витрины, если она переименована."""
    from app.config import settings

    if not isinstance(value, str):
        return value
    if not settings.is_rebranded():
        return value
    return apply_brand(value, settings.get_vpn_brand_name())
