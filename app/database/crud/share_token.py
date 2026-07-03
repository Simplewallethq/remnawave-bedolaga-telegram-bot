from __future__ import annotations

import logging
import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.device_binding_code import CODE_ALPHABET, CODE_LENGTH
from app.database.crud.device_link import count_device_links, get_device_link
from app.database.models import DeviceBindingCode, DeviceLink, ShareToken, Subscription

logger = logging.getLogger(__name__)

DEFAULT_MAX_ACTIVATIONS = 10

ACTIVATE_OK = "ok"
ACTIVATE_NOT_FOUND = "not_found"
ACTIVATE_DEAD = "dead"
ACTIVATE_SUB_INACTIVE = "sub_inactive"
ACTIVATE_LIMIT = "limit"


def _generate_token() -> str:
    # 24 байта энтропии -> 32 url-safe символа; user_id/subscription_id внутри нет.
    return secrets.token_urlsafe(24)


def _generate_share_code() -> str:
    # Тот же формат, что личные коды привязки (16 символов, тот же алфавит):
    # приложение валидирует ровно 16 символов и не отличает типы кодов.
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


async def _share_code_collides(db: AsyncSession, code: str) -> bool:
    """bind-by-code сначала проверяет личные коды — коллизия затенила бы share-код."""
    result = await db.execute(
        select(DeviceBindingCode.id).where(DeviceBindingCode.code == code).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def get_active_share_token(
    db: AsyncSession, subscription_id: int
) -> ShareToken | None:
    result = await db.execute(
        select(ShareToken)
        .where(
            ShareToken.subscription_id == subscription_id,
            ShareToken.revoked_at.is_(None),
            ShareToken.activations_count < ShareToken.max_activations,
        )
        .order_by(ShareToken.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_by_token(db: AsyncSession, token: str) -> ShareToken | None:
    result = await db.execute(select(ShareToken).where(ShareToken.token == token))
    return result.scalar_one_or_none()


async def get_or_create_share_token(
    db: AsyncSession,
    subscription_id: int,
    max_activations: int = DEFAULT_MAX_ACTIVATIONS,
) -> ShareToken:
    """Вернуть действующий токен подписки или молча выпустить новый.

    Это и есть «ленивый перевыпуск»: исчерпанные/отозванные токены остаются
    мёртвыми (страница по старой ссылке гаснет), а владелец по клику всегда
    получает рабочую ссылку. Без уведомлений и счётчиков.
    """
    existing = await get_active_share_token(db, subscription_id)
    if existing is not None:
        return existing

    now = datetime.utcnow()
    # Гасим все прочие токены подписки, чтобы действующий был ровно один.
    stale_result = await db.execute(
        select(ShareToken).where(
            ShareToken.subscription_id == subscription_id,
            ShareToken.revoked_at.is_(None),
        )
    )
    for stale in stale_result.scalars().all():
        stale.revoked_at = now

    for _ in range(5):
        code = _generate_share_code()
        if await _share_code_collides(db, code):
            continue
        record = ShareToken(
            subscription_id=subscription_id,
            token=_generate_token(),
            share_code=code,
            max_activations=max_activations,
        )
        db.add(record)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            continue
        await db.refresh(record)
        return record

    raise RuntimeError("Failed to generate a unique share token after 5 attempts")


async def activate_share_code(
    db: AsyncSession, code: str, device_id: str
) -> tuple[Subscription | None, str, dict]:
    """Привязать устройство по share-коду, атомарно учитывая активацию.

    Возвращает (subscription, status, extra):
        - (subscription, ACTIVATE_OK, {}) — устройство привязано (или уже было);
        - (None, ACTIVATE_NOT_FOUND, {}) — нет такого кода;
        - (None, ACTIVATE_DEAD, {}) — код отозван или исчерпан;
        - (None, ACTIVATE_SUB_INACTIVE, {}) — подписка неактивна;
        - (None, ACTIVATE_LIMIT, {"count": x, "limit": y}) — все места заняты
          (счётчик активаций НЕ увеличивается).
    """
    result = await db.execute(
        select(ShareToken).where(ShareToken.share_code == code).with_for_update()
    )
    record = result.scalar_one_or_none()
    if record is None:
        return None, ACTIVATE_NOT_FOUND, {}

    if record.revoked_at is not None or record.activations_count >= record.max_activations:
        return None, ACTIVATE_DEAD, {}

    sub_result = await db.execute(
        select(Subscription).where(Subscription.id == record.subscription_id)
    )
    subscription = sub_result.scalar_one_or_none()
    if subscription is None:
        return None, ACTIVATE_NOT_FOUND, {}

    if subscription.actual_status not in ("active", "trial"):
        return None, ACTIVATE_SUB_INACTIVE, {}

    existing_link = await get_device_link(db, device_id)
    if existing_link is not None and existing_link.subscription_id == subscription.id:
        # Повторная привязка того же устройства не должна жечь активации.
        return subscription, ACTIVATE_OK, {}

    count = await count_device_links(db, subscription.id)
    limit = subscription.device_limit or 1
    if count >= limit:
        # В отличие от личных кодов, share-код на лимите не расходуется.
        return None, ACTIVATE_LIMIT, {"count": count, "limit": limit}

    # Привязка и учёт активации — одной транзакцией под row-lock'ом токена,
    # чтобы конкурентные активации не проскочили мимо 10-го колпака.
    now = datetime.utcnow()
    if existing_link is not None:
        # Перепривязка in-place (как rebind_device_link): unique-индекс
        # device_id не пустеет и не дублируется.
        logger.info(
            "Rebinding device %s from subscription %s to %s via share code",
            device_id, existing_link.subscription_id, subscription.id,
        )
        existing_link.subscription_id = subscription.id
        existing_link.linked_at = now
    else:
        db.add(DeviceLink(subscription_id=subscription.id, device_id=device_id))

    record.activations_count += 1
    if record.activations_count >= record.max_activations:
        # Защита от утечки страницы: после 10-й активации код гаснет.
        record.revoked_at = now
    await db.commit()

    await db.refresh(subscription)
    return subscription, ACTIVATE_OK, {}
