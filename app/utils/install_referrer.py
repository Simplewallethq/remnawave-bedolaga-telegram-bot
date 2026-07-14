from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    from app.database.models import User

logger = logging.getLogger(__name__)

_ACQUISITION_SOURCE_MAX_LENGTH = 100


def parse_install_referrer(raw: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    """Parse a Google Play Install Referrer string.

    Expected form: 'utm_source=telegram&tg_user_id=123456789'.
    Returns (utm_source, tg_user_id); each is None when missing or invalid.
    Never raises on garbage input.
    """
    if not raw:
        return None, None

    try:
        params = dict(parse_qsl(raw, keep_blank_values=False))
    except ValueError:
        return None, None

    utm_source = (params.get("utm_source") or "").strip() or None
    if utm_source:
        utm_source = utm_source[:_ACQUISITION_SOURCE_MAX_LENGTH]

    tg_user_id: Optional[int] = None
    tg_user_id_raw = (params.get("tg_user_id") or "").strip()
    if tg_user_id_raw.isdigit():
        tg_user_id = int(tg_user_id_raw)

    return utm_source, tg_user_id


def build_personal_play_link(base_url: Optional[str], telegram_id: Optional[int]) -> Optional[str]:
    """Персонализирует ссылку на Google Play, добавляя параметр
    referrer=utm_source%3Dtelegram%26tg_user_id%3D<telegram_id>, который
    приложение получит через Install Referrer API после установки.

    Не-Play ссылки (например, прямой APK) возвращаются без изменений —
    referrer имеет смысл только для установки из Google Play.
    """
    url = (base_url or "").strip()
    if not url:
        return None
    if not telegram_id:
        return url

    parts = urlsplit(url)
    if parts.netloc.lower() != "play.google.com":
        return url

    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "referrer"
    ]
    query.append(
        ("referrer", urlencode({"utm_source": "telegram", "tg_user_id": telegram_id}))
    )
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def apply_install_referrer(user: "User", raw: Optional[str]) -> bool:
    """Apply install-referrer attribution to a user (first-touch: only fills
    empty fields, never overwrites existing attribution).

    Returns True if the user object was modified (caller must commit).
    """
    utm_source, tg_user_id = parse_install_referrer(raw)
    if utm_source is None and tg_user_id is None:
        return False

    changed = False

    if utm_source and not user.acquisition_source:
        user.acquisition_source = utm_source
        changed = True

    if tg_user_id and not user.tg_user_id:
        user.tg_user_id = tg_user_id
        changed = True

    # Реферрер приходит только из мобильного приложения — фиксируем факт
    # использования даже если атрибуция уже была записана ранее.
    if not user.has_used_mobile_app:
        user.has_used_mobile_app = True
        changed = True

    if changed:
        logger.info(
            "Install referrer применён к пользователю %s: acquisition_source=%s, tg_user_id=%s",
            user.id,
            user.acquisition_source,
            user.tg_user_id,
        )

    return changed
