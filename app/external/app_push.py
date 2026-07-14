"""Fire-and-forget вебхук в teleVpn-бэкенд, который рассылает FCM-пуши.

Бэкенд хранит FCM-токены приложений и по этому вебхуку доставляет
уведомление на все девайсы указанных пользователей. Контракт:
POST {APP_PUSH_WEBHOOK_URL} с заголовком X-Api-Key и телом
{"user_ids": [...], "notification": {id, type, title, body, payload, created_at}}.

Модуль никогда не поднимает исключения — ошибки логируются и глотаются
(пуш не должен ломать платежи/мониторинг/уведомления кабинета).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(
        settings.APP_PUSH_ENABLED
        and settings.APP_PUSH_WEBHOOK_URL
        and settings.APP_PUSH_API_KEY
    )


async def send_app_push(
    user_ids: Iterable[int],
    *,
    notification_id: Optional[int],
    type: str,
    title: Optional[str],
    body: Optional[str],
    payload: Optional[Dict[str, Any]],
    created_at: Optional[str],
) -> None:
    """Отправить уведомление в push-вебхук бэкенда. Никогда не raises."""
    if not is_configured():
        return

    ids = list(user_ids)
    if not ids:
        return

    request_body = {
        "user_ids": ids,
        "notification": {
            "id": notification_id or 0,
            "type": type,
            "title": title or "",
            "body": body or "",
            "payload": payload or {},
            "created_at": created_at or "",
        },
    }
    try:
        timeout = aiohttp.ClientTimeout(total=settings.APP_PUSH_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                settings.APP_PUSH_WEBHOOK_URL,
                json=request_body,
                headers={"X-Api-Key": settings.APP_PUSH_API_KEY},
            ) as response:
                if response.status >= 400:
                    preview = (await response.text())[:500]
                    logger.warning(
                        "App push webhook вернул %s для %s получателей (%s): %s",
                        response.status,
                        len(ids),
                        type,
                        preview,
                    )
    except Exception:
        logger.warning(
            "Ошибка вызова app push webhook (%s, %s получателей)",
            type,
            len(ids),
            exc_info=True,
        )
