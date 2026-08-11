"""Вызов teleVpn-бэкенда для подключения телевизора из Telegram.

Зритель на letovpn.com/tv выбирает вход через Telegram, попадает сюда по
deep-link `/start tv_<pairing_token>` и подтверждает подключение. Бот
пересылает токен бэкенду, а тот привязывает телевизор к аккаунту.

Почему привязку делает бэкенд, а не бот напрямую: pairing-сессия хранит
device_id телевизора, а device_id на бэкенде — это учётка. По нему
`POST /api/v1/device/status` выдаёт JWT любому, кто его знает. Поэтому наружу
он не отдаётся никогда, и бот тоже его не получает.

В deep-link едет шестизначный код с экрана телевизора, а не QR-токен: Telegram
ограничивает payload у `/start` 64 символами, а токен сам по себе — 64 hex-символа.
Побочный плюс — Telegram доступен и тем, кто ввёл цифры руками, а не сканировал.

Контракт: POST {APP_TV_PAIRING_WEBHOOK_URL} с заголовком X-Api-Key и телом
{"short_code": "502476", "bot_user_id": 123}. Ответ — {"status": "..."}.

В отличие от app_push этот модуль ошибки НЕ глотает: зритель ждёт ответа на
экране, и молчание вместо результата хуже честного «не получилось».
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)

# Машиночитаемые исходы, которые возвращает бэкенд. Бот по ним выбирает текст.
STATUS_COMPLETED = "completed"
STATUS_DEVICE_LIMIT = "device_limit_reached"
STATUS_NO_SUBSCRIPTION = "no_subscription"
STATUS_PAIRING_EXPIRED = "pairing_expired"
STATUS_PAIRING_USED = "pairing_already_used"
STATUS_PAIRING_NOT_FOUND = "pairing_not_found"
STATUS_TOO_MANY_ATTEMPTS = "too_many_attempts"
# Локальный исход: до бэкенда не дошли (сеть, таймаут, 5xx, нет настройки).
STATUS_ERROR = "error"


def is_configured() -> bool:
    return bool(settings.APP_TV_PAIRING_WEBHOOK_URL and settings.APP_PUSH_API_KEY)


async def authorize_tv(short_code: str, bot_user_id: int) -> str:
    """Подключить телевизор к аккаунту bot_user_id. Возвращает статус-маркер.

    Никогда не raises: любая неудача — это STATUS_ERROR, потому что вызывающий
    код показывает пользователю сообщение и не должен падать.
    """
    if not is_configured():
        logger.warning("TV pairing: APP_TV_PAIRING_WEBHOOK_URL не задан")
        return STATUS_ERROR

    try:
        timeout = aiohttp.ClientTimeout(total=settings.APP_PUSH_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                settings.APP_TV_PAIRING_WEBHOOK_URL,
                json={"short_code": short_code, "bot_user_id": bot_user_id},
                headers={"X-Api-Key": settings.APP_PUSH_API_KEY},
            ) as response:
                # Тело читаем всегда: маркер приезжает и в ответах 4xx.
                payload = await _read_status(response)
                if payload:
                    return payload
                logger.warning(
                    "TV pairing: бэкенд вернул %s без распознанного статуса",
                    response.status,
                )
                return STATUS_ERROR
    except Exception:
        # Код в лог не пишем: пока сессия жива, он даёт право привязать телевизор.
        logger.warning("TV pairing: ошибка вызова бэкенда", exc_info=True)
        return STATUS_ERROR


async def _read_status(response: aiohttp.ClientResponse) -> Optional[str]:
    try:
        body = await response.json(content_type=None)
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    status = body.get("status")
    return status if isinstance(status, str) and status else None
