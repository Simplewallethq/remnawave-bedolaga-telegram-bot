"""Метки рекламного источника, с которыми посетитель зарегистрировался в кабинете.

Фронт кабинета снимает их из адреса первого захода (lk.letovpn.com/?from=letovps&utm_…)
и присылает вместе с регистрацией. Данные приходят от клиента как есть, поэтому
берём только известные ключи и режем длину. Мусор в метках не должен мешать
регистрации: при любой ошибке возвращаем None.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_MAX_LENGTHS = {
    "from": 64,
    "utm_source": 100,
    "utm_medium": 100,
    "utm_campaign": 255,
    "utm_content": 255,
    "utm_term": 255,
    "yclid": 64,
    "gclid": 255,
    "referrer": 500,
    "landing_path": 255,
    "captured_at": 64,
}

# Тот же предел, что у колонки users.acquisition_source.
ACQUISITION_SOURCE_MAX_LENGTH = 100


def sanitize_signup_attribution(raw: Any) -> Optional[Dict[str, str]]:
    """Оставляет известные ключи со строковыми непустыми значениями."""
    if not isinstance(raw, dict):
        return None
    cleaned: Dict[str, str] = {}
    for key, limit in _MAX_LENGTHS.items():
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value:
            cleaned[key] = value[:limit]
    return cleaned or None


def acquisition_source_from(attribution: Optional[Dict[str, str]]) -> Optional[str]:
    """utm_source, а без него метка лендинга (from)."""
    if not attribution:
        return None
    source = attribution.get("utm_source") or attribution.get("from")
    return source[:ACQUISITION_SOURCE_MAX_LENGTH] if source else None
