"""Уведомления личного кабинета: персистентная лента + realtime-доставка через SSE.

Единая точка входа — notify() (или notify_detached/notify_bulk, если нет сессии).
Сбой записи/доставки уведомления никогда не пробрасывается наружу — пуш не должен
ломать платежи, мониторинг или отправку в Telegram.

CabinetNotificationHub — in-process fan-out: бот и uvicorn работают в одном
процессе с одним воркером (см. app/webapi/server.py, embed-режим), поэтому
publish из любого места кода доходит до подключённых SSE-клиентов.
Если web API когда-нибудь вынесут в отдельный процесс/мульти-воркер —
заменить внутренности subscribe/publish на Redis pub/sub (app/utils/cache.py),
публичный API хаба менять не нужно.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.cabinet_notification import (
    bulk_create_notifications,
    create_cabinet_notification,
)
from app.database.database import get_db
from app.database.models import CabinetNotification

logger = logging.getLogger(__name__)


class CabinetNotificationType:
    SUBSCRIPTION_EXPIRING = "subscription_expiring"
    SUBSCRIPTION_EXPIRED = "subscription_expired"
    TRIAL_ENDING = "trial_ending"
    AUTOPAY_SUCCESS = "autopay_success"
    AUTOPAY_FAILED = "autopay_failed"
    TOPUP_SUCCESS = "topup_success"
    SUBSCRIPTION_PURCHASED = "subscription_purchased"
    SUBSCRIPTION_RENEWED = "subscription_renewed"
    REFERRAL_JOINED = "referral_joined"
    REFERRAL_COMMISSION = "referral_commission"
    TICKET_REPLY = "ticket_reply"
    BROADCAST = "broadcast"
    PROMO_OFFER = "promo_offer"


# Русские title/body — fallback для фронта (основной рендер — по type+payload
# локалями самого фронта, см. контракт в plan/доках).
_DEFAULT_TEXTS: Dict[str, tuple] = {
    CabinetNotificationType.SUBSCRIPTION_EXPIRING: (
        "Подписка скоро закончится",
        "Продлите подписку, чтобы не потерять доступ.",
    ),
    CabinetNotificationType.SUBSCRIPTION_EXPIRED: (
        "Подписка истекла",
        "Доступ приостановлен. Продлите подписку, чтобы восстановить его.",
    ),
    CabinetNotificationType.TRIAL_ENDING: (
        "Пробный период заканчивается",
        "Оформите подписку, чтобы сохранить доступ.",
    ),
    CabinetNotificationType.AUTOPAY_SUCCESS: (
        "Подписка продлена автоматически",
        "Автопродление прошло успешно.",
    ),
    CabinetNotificationType.AUTOPAY_FAILED: (
        "Не удалось продлить подписку",
        "Недостаточно средств для автопродления. Пополните баланс.",
    ),
    CabinetNotificationType.TOPUP_SUCCESS: (
        "Баланс пополнен",
        "Средства зачислены на баланс.",
    ),
    CabinetNotificationType.SUBSCRIPTION_PURCHASED: (
        "Подписка оформлена",
        "Спасибо за покупку!",
    ),
    CabinetNotificationType.SUBSCRIPTION_RENEWED: (
        "Подписка продлена",
        "Срок действия подписки увеличен.",
    ),
    CabinetNotificationType.REFERRAL_JOINED: (
        "Новый реферал",
        "По вашей ссылке зарегистрировался новый пользователь.",
    ),
    CabinetNotificationType.REFERRAL_COMMISSION: (
        "Реферальное вознаграждение",
        "Вам начислена комиссия с пополнения реферала.",
    ),
    CabinetNotificationType.TICKET_REPLY: (
        "Ответ поддержки",
        "В вашем тикете новый ответ.",
    ),
    CabinetNotificationType.BROADCAST: ("Объявление", None),
    CabinetNotificationType.PROMO_OFFER: ("Специальное предложение", None),
}

_CLOSE_SENTINEL: Dict[str, Any] = {"event": "close"}


def serialize_notification(notification: CabinetNotification) -> Dict[str, Any]:
    return {
        "id": f"ntf_{notification.id}",
        "type": notification.type,
        "title": notification.title,
        "body": notification.body,
        "payload": notification.payload or {},
        "isRead": bool(notification.is_read),
        "createdAt": notification.created_at.isoformat() if notification.created_at else None,
    }


class CabinetNotificationHub:

    def __init__(self) -> None:
        self._subscribers: Dict[int, List[asyncio.Queue]] = {}

    def subscribe(self, user_id: int) -> asyncio.Queue:
        queues = self._subscribers.setdefault(user_id, [])
        max_connections = max(1, settings.CABINET_NOTIFICATIONS_SSE_MAX_CONNECTIONS)
        while len(queues) >= max_connections:
            oldest = queues.pop(0)
            self._put_nowait(oldest, _CLOSE_SENTINEL)
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        queues.append(queue)
        return queue

    def unsubscribe(self, user_id: int, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(user_id)
        if not queues:
            return
        try:
            queues.remove(queue)
        except ValueError:
            pass
        if not queues:
            self._subscribers.pop(user_id, None)

    def publish(self, user_id: int, event: Dict[str, Any]) -> None:
        for queue in self._subscribers.get(user_id, []):
            self._put_nowait(queue, event)

    @property
    def connected_user_ids(self) -> Set[int]:
        return set(self._subscribers.keys())

    @staticmethod
    def _put_nowait(queue: asyncio.Queue, event: Dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass


cabinet_notification_hub = CabinetNotificationHub()


def _is_enabled() -> bool:
    return bool(settings.CABINET_ENABLED and settings.CABINET_NOTIFICATIONS_ENABLED)


# Лимит получателей одного вызова push-вебхука (совпадает с капом бэкенда).
_APP_PUSH_CHUNK_SIZE = 10_000


def _schedule_app_push(
    user_ids: List[int],
    *,
    notification_id: Optional[int],
    type: str,
    title: Optional[str],
    body: Optional[str],
    payload: Optional[Dict[str, Any]],
    created_at: Optional[str],
) -> None:
    """Fire-and-forget FCM-пуш через teleVpn-вебхук. Никогда не raises."""
    try:
        from app.external.app_push import is_configured, send_app_push

        if not is_configured():
            return
        for start in range(0, len(user_ids), _APP_PUSH_CHUNK_SIZE):
            asyncio.create_task(
                send_app_push(
                    user_ids[start : start + _APP_PUSH_CHUNK_SIZE],
                    notification_id=notification_id,
                    type=type,
                    title=title,
                    body=body,
                    payload=payload,
                    created_at=created_at,
                )
            )
    except Exception:
        logger.warning("Не удалось запланировать app push (%s)", type, exc_info=True)


def _resolve_texts(
    type: str, title: Optional[str], body: Optional[str]
) -> tuple:
    default_title, default_body = _DEFAULT_TEXTS.get(type, (None, None))
    return title or default_title, body or default_body


async def notify(
    db: AsyncSession,
    *,
    user_id: int,
    type: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[CabinetNotification]:
    """Сохранить уведомление и доставить его в открытые SSE-стримы пользователя.

    Вызывать после коммита вызывающего кода: функция сама делает commit.
    """
    if not _is_enabled():
        return None

    try:
        resolved_title, resolved_body = _resolve_texts(type, title, body)
        notification = await create_cabinet_notification(
            db,
            user_id=user_id,
            type=type,
            title=resolved_title,
            body=resolved_body,
            payload=payload,
        )
        cabinet_notification_hub.publish(user_id, serialize_notification(notification))
        _schedule_app_push(
            [user_id],
            notification_id=notification.id,
            type=type,
            title=resolved_title,
            body=resolved_body,
            payload=payload,
            created_at=(
                notification.created_at.isoformat() if notification.created_at else None
            ),
        )
        return notification
    except Exception:
        logger.error(
            "Не удалось создать уведомление кабинета (%s) для пользователя %s",
            type,
            user_id,
            exc_info=True,
        )
        try:
            await db.rollback()
        except Exception:
            logger.error(
                "Не удалось выполнить rollback после ошибки уведомления кабинета",
                exc_info=True,
            )
        return None


async def notify_detached(
    user_id: int,
    *,
    type: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[CabinetNotification]:
    """То же, что notify(), но в собственной сессии (для кода без AsyncSession)."""
    if not _is_enabled():
        return None

    try:
        async for session in get_db():
            return await notify(
                session,
                user_id=user_id,
                type=type,
                title=title,
                body=body,
                payload=payload,
            )
    except Exception:
        logger.error(
            "Не удалось создать detached-уведомление кабинета (%s) для пользователя %s",
            type,
            user_id,
            exc_info=True,
        )
    return None


async def notify_bulk(
    user_ids: Iterable[int],
    *,
    type: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> int:
    """Массовое уведомление (рассылки): один bulk insert, publish только подключённым."""
    if not _is_enabled():
        return 0

    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return 0

    resolved_title, resolved_body = _resolve_texts(type, title, body)

    try:
        async for session in get_db():
            created = await bulk_create_notifications(
                session,
                ids,
                type=type,
                title=resolved_title,
                body=resolved_body,
                payload=payload,
            )

            connected = cabinet_notification_hub.connected_user_ids & set(ids)
            if connected:
                from app.database.crud.cabinet_notification import (
                    list_cabinet_notifications,
                )

                for user_id in connected:
                    items, _ = await list_cabinet_notifications(
                        session, user_id, limit=1
                    )
                    if items and items[0].type == type:
                        cabinet_notification_hub.publish(
                            user_id, serialize_notification(items[0])
                        )

            # Пуш — всем получателям, не только подключённым к SSE; per-user id
            # после bulk insert недоступен, поэтому id=0.
            _schedule_app_push(
                ids,
                notification_id=None,
                type=type,
                title=resolved_title,
                body=resolved_body,
                payload=payload,
                created_at=None,
            )
            return created
    except Exception:
        logger.error(
            "Не удалось создать массовые уведомления кабинета (%s)",
            type,
            exc_info=True,
        )
    return 0
