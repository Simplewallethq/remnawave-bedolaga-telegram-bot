"""CRUD-операции для платежей Platega."""

from __future__ import annotations

import logging
from datetime import datetime
import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PlategaPayment, PlategaSubscription

logger = logging.getLogger(__name__)


async def create_platega_payment(
    db: AsyncSession,
    *,
    user_id: int,
    amount_kopeks: int,
    currency: str,
    description: Optional[str],
    status: str,
    payment_method_code: int,
    correlation_id: str,
    platega_transaction_id: Optional[str],
    redirect_url: Optional[str],
    return_url: Optional[str],
    failed_url: Optional[str],
    payload: Optional[str],
    metadata: Optional[dict[str, Any]] = None,
    expires_at: Optional[datetime] = None,
    subscription_id: Optional[int] = None,
) -> PlategaPayment:
    payment = PlategaPayment(
        user_id=user_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        status=status,
        payment_method_code=payment_method_code,
        correlation_id=correlation_id,
        platega_transaction_id=platega_transaction_id,
        redirect_url=redirect_url,
        return_url=return_url,
        failed_url=failed_url,
        payload=payload,
        metadata_json=metadata or {},
        expires_at=expires_at,
        subscription_id=subscription_id,
    )

    db.add(payment)
    await db.commit()
    await db.refresh(payment)

    logger.info(
        "Создан Platega платеж #%s (tx=%s) на сумму %s копеек для пользователя %s",
        payment.id,
        platega_transaction_id,
        amount_kopeks,
        user_id,
    )

    return payment


async def create_platega_subscription(
    db: AsyncSession,
    *,
    user_id: int,
    platega_subscription_id: str,
    amount_kopeks: int,
    currency: str,
    description: Optional[str],
    status: str,
    redirect_url: Optional[str],
    next_charge_at: Optional[datetime] = None,
    last_callback_payload: Optional[dict[str, Any]] = None,
    active_user_id: Optional[int] = None,
) -> PlategaSubscription:
    subscription = PlategaSubscription(
        user_id=user_id,
        active_user_id=active_user_id,
        platega_subscription_id=platega_subscription_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        status=status,
        redirect_url=redirect_url,
        next_charge_at=next_charge_at,
        last_callback_payload=last_callback_payload,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return subscription


async def get_platega_subscription_by_id(
    db: AsyncSession, subscription_id: int
) -> Optional[PlategaSubscription]:
    result = await db.execute(
        select(PlategaSubscription).where(PlategaSubscription.id == subscription_id)
    )
    return result.scalar_one_or_none()


async def get_platega_subscription_by_id_for_update(
    db: AsyncSession, subscription_id: int
) -> Optional[PlategaSubscription]:
    result = await db.execute(
        select(PlategaSubscription)
        .where(PlategaSubscription.id == subscription_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def get_platega_subscription_by_provider_id(
    db: AsyncSession, platega_subscription_id: str
) -> Optional[PlategaSubscription]:
    result = await db.execute(
        select(PlategaSubscription).where(
            PlategaSubscription.platega_subscription_id == platega_subscription_id
        )
    )
    return result.scalar_one_or_none()


async def get_platega_subscription_by_provider_id_for_update(
    db: AsyncSession, platega_subscription_id: str
) -> Optional[PlategaSubscription]:
    result = await db.execute(
        select(PlategaSubscription)
        .where(PlategaSubscription.platega_subscription_id == platega_subscription_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def get_active_platega_subscription_for_user(
    db: AsyncSession, user_id: int
) -> Optional[PlategaSubscription]:
    result = await db.execute(
        select(PlategaSubscription).where(
            PlategaSubscription.active_user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def update_platega_subscription(
    db: AsyncSession,
    *,
    subscription: PlategaSubscription,
    status: Optional[str] = None,
    platega_subscription_id: Optional[str] = None,
    set_platega_subscription_id: bool = False,
    redirect_url: Optional[str] = None,
    set_redirect_url: bool = False,
    next_charge_at: Optional[datetime] = None,
    set_next_charge_at: bool = False,
    last_callback_payload: Optional[dict[str, Any]] = None,
    cancelled_at: Optional[datetime] = None,
    set_cancelled_at: bool = False,
    active_user_id: Optional[int] = None,
    set_active_user_id: bool = False,
) -> PlategaSubscription:
    if status is not None:
        subscription.status = status
    if set_platega_subscription_id:
        subscription.platega_subscription_id = platega_subscription_id
    if set_redirect_url:
        subscription.redirect_url = redirect_url
    if set_next_charge_at:
        subscription.next_charge_at = next_charge_at
    if last_callback_payload is not None:
        subscription.last_callback_payload = last_callback_payload
    if set_cancelled_at:
        subscription.cancelled_at = cancelled_at
    if set_active_user_id:
        subscription.active_user_id = active_user_id

    subscription.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(subscription)
    return subscription


async def get_or_create_platega_subscription_charge(
    db: AsyncSession,
    *,
    subscription_id: int,
    user_id: int,
    platega_transaction_id: str,
    amount_kopeks: int,
    currency: str,
    description: Optional[str],
    status: str,
    callback_payload: dict[str, Any],
) -> PlategaPayment:
    existing = await get_platega_payment_by_transaction_id(db, platega_transaction_id)
    if existing:
        return existing

    try:
        payment = PlategaPayment(
            user_id=user_id,
            subscription_id=subscription_id,
            platega_transaction_id=platega_transaction_id,
            correlation_id=uuid.uuid4().hex,
            amount_kopeks=amount_kopeks,
            currency=currency,
            description=description,
            payment_method_code=6,
            status=status,
            callback_payload=callback_payload,
            metadata_json={"source": "platega_subscription_charge"},
        )
        db.add(payment)
        await db.commit()
        await db.refresh(payment)
        return payment
    except IntegrityError:
        await db.rollback()
        existing = await get_platega_payment_by_transaction_id(db, platega_transaction_id)
        if existing:
            return existing
        raise


async def get_platega_payment_by_id(
    db: AsyncSession, payment_id: int
) -> Optional[PlategaPayment]:
    result = await db.execute(
        select(PlategaPayment).where(PlategaPayment.id == payment_id)
    )
    return result.scalar_one_or_none()


async def get_platega_payment_by_id_for_update(
    db: AsyncSession, payment_id: int
) -> Optional[PlategaPayment]:
    result = await db.execute(
        select(PlategaPayment)
        .where(PlategaPayment.id == payment_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def get_platega_payment_by_transaction_id(
    db: AsyncSession, transaction_id: str
) -> Optional[PlategaPayment]:
    result = await db.execute(
        select(PlategaPayment).where(
            PlategaPayment.platega_transaction_id == transaction_id
        )
    )
    return result.scalar_one_or_none()


async def get_platega_payment_by_correlation_id(
    db: AsyncSession, correlation_id: str
) -> Optional[PlategaPayment]:
    result = await db.execute(
        select(PlategaPayment).where(
            PlategaPayment.correlation_id == correlation_id
        )
    )
    return result.scalar_one_or_none()


async def update_platega_payment(
    db: AsyncSession,
    *,
    payment: PlategaPayment,
    status: Optional[str] = None,
    is_paid: Optional[bool] = None,
    paid_at: Optional[datetime] = None,
    platega_transaction_id: Optional[str] = None,
    redirect_url: Optional[str] = None,
    callback_payload: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
    expires_at: Optional[datetime] = None,
) -> PlategaPayment:
    if status is not None:
        payment.status = status
    if is_paid is not None:
        payment.is_paid = is_paid
    if paid_at is not None:
        payment.paid_at = paid_at
    if platega_transaction_id and not payment.platega_transaction_id:
        payment.platega_transaction_id = platega_transaction_id
    if redirect_url is not None:
        payment.redirect_url = redirect_url
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if metadata is not None:
        payment.metadata_json = metadata
    if expires_at is not None:
        payment.expires_at = expires_at

    payment.updated_at = datetime.utcnow()

    await db.commit()
    await db.refresh(payment)
    return payment


async def link_platega_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: PlategaPayment,
    transaction_id: int,
) -> PlategaPayment:
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(payment)
    return payment
