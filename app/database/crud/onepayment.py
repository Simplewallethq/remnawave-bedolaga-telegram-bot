"""CRUD для платежей и привязок 1Payment (СБП с рекуррентами)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    OnePaymentBinding,
    OnePaymentPayment,
    Subscription,
    SubscriptionStatus,
)

logger = logging.getLogger(__name__)


def truncate_seconds(value: Optional[datetime]) -> Optional[datetime]:
    """MySQL DATETIME отбрасывает микросекунды — сравниваем периоды по секундам."""
    if value is None:
        return None
    return value.replace(microsecond=0)


# --------------------------------------------------------------------- платежи


async def create_onepayment_payment(
    db: AsyncSession,
    *,
    user_id: int,
    user_data: str,
    amount_kopeks: int,
    currency: str,
    description: Optional[str],
    status: str,
    url: Optional[str] = None,
    provider_order_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    expires_at: Optional[datetime] = None,
    is_recurring: bool = False,
    binding_id: Optional[int] = None,
) -> OnePaymentPayment:
    payment = OnePaymentPayment(
        user_id=user_id,
        user_data=user_data,
        provider_order_id=provider_order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        status=status,
        url=url,
        metadata_json=metadata or {},
        expires_at=expires_at,
        is_recurring=is_recurring,
        binding_id=binding_id,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)

    logger.info(
        "Создан 1Payment платеж #%s (%s) для пользователя %s: %s копеек, recurring=%s",
        payment.id,
        user_data,
        user_id,
        amount_kopeks,
        is_recurring,
    )
    return payment


async def get_onepayment_payment_by_id(
    db: AsyncSession, payment_id: int
) -> Optional[OnePaymentPayment]:
    result = await db.execute(
        select(OnePaymentPayment).where(OnePaymentPayment.id == payment_id)
    )
    return result.scalar_one_or_none()


async def get_onepayment_payment_by_id_for_update(
    db: AsyncSession, payment_id: int
) -> Optional[OnePaymentPayment]:
    result = await db.execute(
        select(OnePaymentPayment)
        .where(OnePaymentPayment.id == payment_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def get_onepayment_payment_by_user_data(
    db: AsyncSession, user_data: str
) -> Optional[OnePaymentPayment]:
    result = await db.execute(
        select(OnePaymentPayment).where(OnePaymentPayment.user_data == user_data)
    )
    return result.scalar_one_or_none()


async def get_onepayment_payment_by_provider_order_id(
    db: AsyncSession, provider_order_id: str
) -> Optional[OnePaymentPayment]:
    result = await db.execute(
        select(OnePaymentPayment).where(
            OnePaymentPayment.provider_order_id == provider_order_id
        )
    )
    return result.scalar_one_or_none()


async def mark_onepayment_payment_paid_cas(
    db: AsyncSession,
    payment_id: int,
    *,
    paid_at: Optional[datetime] = None,
) -> bool:
    """Атомарно помечает платёж оплаченным. True — мы выиграли гонку.

    1Payment ретраит колбек раз в минуту 10 минут, а кнопка «Проверить оплату»
    может прийти параллельно — зачёт должен пройти ровно один раз.
    """
    result = await db.execute(
        update(OnePaymentPayment)
        .where(
            and_(
                OnePaymentPayment.id == payment_id,
                OnePaymentPayment.is_paid == False,  # noqa: E712
            )
        )
        .values(
            is_paid=True,
            paid_at=paid_at or datetime.utcnow(),
            status=OnePaymentPayment.STATUS_SUCCESS,
            updated_at=datetime.utcnow(),
        )
    )
    await db.commit()
    return (result.rowcount or 0) == 1


async def update_onepayment_payment(
    db: AsyncSession,
    payment: OnePaymentPayment,
    *,
    status: Optional[str] = None,
    status_code: Optional[str] = None,
    is_paid: Optional[bool] = None,
    paid_at: Optional[datetime] = None,
    url: Optional[str] = None,
    provider_order_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    callback_payload: Optional[Dict[str, Any]] = None,
    expires_at: Optional[datetime] = None,
) -> OnePaymentPayment:
    values: Dict[str, Any] = {}
    if status is not None:
        values["status"] = status
    if status_code is not None:
        values["status_code"] = status_code
    if is_paid is not None:
        values["is_paid"] = is_paid
    if paid_at is not None:
        values["paid_at"] = paid_at
    if url is not None:
        values["url"] = url
    if provider_order_id is not None:
        values["provider_order_id"] = provider_order_id
    if metadata is not None:
        values["metadata_json"] = metadata
    if callback_payload is not None:
        values["callback_payload"] = callback_payload
    if expires_at is not None:
        values["expires_at"] = expires_at

    if not values:
        return payment

    values["updated_at"] = datetime.utcnow()
    await db.execute(
        update(OnePaymentPayment)
        .where(OnePaymentPayment.id == payment.id)
        .values(**values)
    )
    await db.commit()
    await db.refresh(payment)
    return payment


async def link_onepayment_payment_to_transaction(
    db: AsyncSession, payment: OnePaymentPayment, transaction_id: int
) -> OnePaymentPayment:
    await db.execute(
        update(OnePaymentPayment)
        .where(OnePaymentPayment.id == payment.id)
        .values(transaction_id=transaction_id, updated_at=datetime.utcnow())
    )
    await db.commit()
    await db.refresh(payment)
    return payment


async def get_pending_recurring_payment_for_binding(
    db: AsyncSession, binding_id: int, *, now: Optional[datetime] = None
) -> Optional[OnePaymentPayment]:
    """Незакрытое рекуррентное списание по привязке (не истёкшее по expires_at)."""
    current = now or datetime.utcnow()
    result = await db.execute(
        select(OnePaymentPayment)
        .where(
            and_(
                OnePaymentPayment.binding_id == binding_id,
                OnePaymentPayment.is_recurring == True,  # noqa: E712
                OnePaymentPayment.is_paid == False,  # noqa: E712
                OnePaymentPayment.status.in_(
                    [OnePaymentPayment.STATUS_INIT, OnePaymentPayment.STATUS_PENDING]
                ),
                (OnePaymentPayment.expires_at.is_(None))
                | (OnePaymentPayment.expires_at > current),
            )
        )
        .order_by(OnePaymentPayment.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# -------------------------------------------------------------------- привязки


async def get_active_onepayment_binding_for_user(
    db: AsyncSession, user_id: int
) -> Optional[OnePaymentBinding]:
    result = await db.execute(
        select(OnePaymentBinding).where(OnePaymentBinding.active_user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_latest_onepayment_binding_for_user(
    db: AsyncSession, user_id: int
) -> Optional[OnePaymentBinding]:
    """Последняя привязка пользователя любого статуса (для меню: FAILED → «переподключить»)."""
    result = await db.execute(
        select(OnePaymentBinding)
        .where(OnePaymentBinding.user_id == user_id)
        .order_by(OnePaymentBinding.created_at.desc(), OnePaymentBinding.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_onepayment_binding_by_id(
    db: AsyncSession, binding_id: int
) -> Optional[OnePaymentBinding]:
    result = await db.execute(
        select(OnePaymentBinding).where(OnePaymentBinding.id == binding_id)
    )
    return result.scalar_one_or_none()


async def get_onepayment_binding_by_id_for_update(
    db: AsyncSession, binding_id: int
) -> Optional[OnePaymentBinding]:
    result = await db.execute(
        select(OnePaymentBinding)
        .where(OnePaymentBinding.id == binding_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def upsert_active_onepayment_binding(
    db: AsyncSession,
    *,
    user_id: int,
    token: str,
    source_payment_id: Optional[int] = None,
) -> OnePaymentBinding:
    """Делает токен активной привязкой пользователя.

    Активная запись уже есть → обновляем токен (банк выдал новый), сбрасываем
    счётчик неудач. Нет → создаём новую с занятым слотом active_user_id.
    Гонка двух колбеков → IntegrityError на уникальном слоте → перечитываем и
    обновляем.
    """
    result = await db.execute(
        select(OnePaymentBinding)
        .where(OnePaymentBinding.active_user_id == user_id)
        .with_for_update()
    )
    existing = result.scalar_one_or_none()
    now = datetime.utcnow()

    if existing is not None:
        existing.token = token
        existing.status = OnePaymentBinding.STATUS_ACTIVE
        existing.failed_attempts = 0
        if source_payment_id is not None:
            existing.source_payment_id = source_payment_id
        existing.updated_at = now
        await db.commit()
        await db.refresh(existing)
        return existing

    binding = OnePaymentBinding(
        user_id=user_id,
        active_user_id=user_id,
        token=token,
        status=OnePaymentBinding.STATUS_ACTIVE,
        source_payment_id=source_payment_id,
        failed_attempts=0,
    )
    db.add(binding)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        result = await db.execute(
            select(OnePaymentBinding)
            .where(OnePaymentBinding.active_user_id == user_id)
            .with_for_update()
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            raise
        existing.token = token
        existing.status = OnePaymentBinding.STATUS_ACTIVE
        existing.failed_attempts = 0
        if source_payment_id is not None:
            existing.source_payment_id = source_payment_id
        existing.updated_at = now
        await db.commit()
        await db.refresh(existing)
        return existing

    await db.refresh(binding)
    logger.info("Создана привязка 1Payment #%s для пользователя %s", binding.id, user_id)
    return binding


async def update_onepayment_binding(
    db: AsyncSession,
    binding: OnePaymentBinding,
    *,
    status: Optional[str] = None,
    token: Optional[str] = None,
    last_charge_at: Optional[datetime] = None,
    last_charge_status: Optional[str] = None,
    last_charged_period_end: Optional[datetime] = None,
    set_last_charged_period_end: bool = False,
    failed_attempts: Optional[int] = None,
    cancelled_at: Optional[datetime] = None,
    set_cancelled_at: bool = False,
    active_user_id: Optional[int] = None,
    set_active_user_id: bool = False,
) -> OnePaymentBinding:
    if status is not None:
        binding.status = status
    if token is not None:
        binding.token = token
    if last_charge_at is not None:
        binding.last_charge_at = last_charge_at
    if last_charge_status is not None:
        binding.last_charge_status = last_charge_status
    if set_last_charged_period_end:
        binding.last_charged_period_end = truncate_seconds(last_charged_period_end)
    if failed_attempts is not None:
        binding.failed_attempts = failed_attempts
    if set_cancelled_at:
        binding.cancelled_at = cancelled_at
    if set_active_user_id:
        binding.active_user_id = active_user_id

    binding.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(binding)
    return binding


async def deactivate_onepayment_binding(
    db: AsyncSession,
    binding: OnePaymentBinding,
    *,
    status: str,
) -> OnePaymentBinding:
    """Освобождает слот и переводит привязку в CANCELLED / FAILED."""
    return await update_onepayment_binding(
        db,
        binding,
        status=status,
        active_user_id=None,
        set_active_user_id=True,
        cancelled_at=datetime.utcnow() if status == OnePaymentBinding.STATUS_CANCELLED else None,
        set_cancelled_at=status == OnePaymentBinding.STATUS_CANCELLED,
    )


async def list_onepayment_bindings_due(
    db: AsyncSession, *, before: datetime, paid_trial_before: Optional[datetime] = None
) -> List[OnePaymentBinding]:
    """Активные привязки, чья платная подписка заканчивается до `before`.

    Подписка должна быть ACTIVE и не триальной — истёкшие переводит в EXPIRED
    `_check_expired_subscriptions`, что естественно ограничивает окно попыток.

    `paid_trial_before` — отдельное (короткое, в часах) окно для суточных
    подписок «за 1 ₽» (`is_paid_trial`): с общим окном в днях их списание ушло
    бы сразу после оплаты. None — суточные подписки идут по общему окну.
    """
    if paid_trial_before is None:
        due_clause = Subscription.end_date <= before
    else:
        due_clause = or_(
            and_(Subscription.is_paid_trial == False, Subscription.end_date <= before),  # noqa: E712
            and_(Subscription.is_paid_trial == True, Subscription.end_date <= paid_trial_before),  # noqa: E712
        )
    result = await db.execute(
        select(OnePaymentBinding)
        .join(Subscription, Subscription.user_id == OnePaymentBinding.user_id)
        .where(
            and_(
                OnePaymentBinding.active_user_id.is_not(None),
                OnePaymentBinding.status == OnePaymentBinding.STATUS_ACTIVE,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.is_trial == False,  # noqa: E712
                due_clause,
            )
        )
        .order_by(Subscription.end_date.asc())
    )
    return list(result.scalars().all())
