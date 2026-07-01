from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SubscriptionEvent


logger = logging.getLogger(__name__)


def _datetime_to_iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _payment_method_value(value: Any) -> Any:
    return getattr(value, "value", value)


async def create_subscription_event(
    db: AsyncSession,
    *,
    user_id: int,
    event_type: str,
    subscription_id: Optional[int] = None,
    transaction_id: Optional[int] = None,
    amount_kopeks: Optional[int] = None,
    currency: Optional[str] = None,
    message: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> SubscriptionEvent:
    event = SubscriptionEvent(
        user_id=user_id,
        event_type=event_type,
        subscription_id=subscription_id,
        transaction_id=transaction_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        message=message,
        occurred_at=occurred_at or datetime.utcnow(),
        extra=extra or None,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


async def record_subscription_purchase_event(
    db: AsyncSession,
    *,
    user_id: int,
    subscription_id: Optional[int] = None,
    transaction_id: Optional[int] = None,
    amount_kopeks: Optional[int] = None,
    occurred_at: Optional[datetime] = None,
    period_days: Optional[int] = None,
    was_trial_conversion: bool = False,
    payment_method: Any = None,
    source: Optional[str] = None,
    starts_at: Optional[datetime] = None,
    ends_at: Optional[datetime] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[SubscriptionEvent]:
    payload: Dict[str, Any] = {
        "period_days": period_days,
        "was_trial_conversion": was_trial_conversion,
        "payment_method": _payment_method_value(payment_method),
        "source": source,
        "starts_at": _datetime_to_iso(starts_at),
        "ends_at": _datetime_to_iso(ends_at),
    }
    if extra:
        payload.update(extra)
    payload = {key: value for key, value in payload.items() if value is not None}

    try:
        return await create_subscription_event(
            db,
            user_id=user_id,
            event_type="purchase",
            subscription_id=subscription_id,
            transaction_id=transaction_id,
            amount_kopeks=amount_kopeks,
            message="Subscription purchase",
            occurred_at=occurred_at,
            extra=payload or None,
        )
    except Exception:
        logger.error(
            "Не удалось сохранить событие покупки подписки для пользователя %s",
            user_id,
            exc_info=True,
        )
        try:
            await db.rollback()
        except Exception:
            logger.error("Не удалось выполнить rollback после ошибки записи события покупки", exc_info=True)
        return None


async def record_subscription_renewal_event(
    db: AsyncSession,
    *,
    user_id: int,
    subscription_id: Optional[int] = None,
    transaction_id: Optional[int] = None,
    amount_kopeks: Optional[int] = None,
    occurred_at: Optional[datetime] = None,
    period_days: Optional[int] = None,
    previous_end_date: Optional[datetime] = None,
    new_end_date: Optional[datetime] = None,
    payment_method: Any = None,
    balance_after: Optional[int] = None,
    source: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[SubscriptionEvent]:
    payload: Dict[str, Any] = {
        "period_days": period_days,
        "extended_days": period_days,
        "previous_end_date": _datetime_to_iso(previous_end_date),
        "new_end_date": _datetime_to_iso(new_end_date),
        "payment_method": _payment_method_value(payment_method),
        "balance_after": balance_after,
        "source": source,
    }
    if extra:
        payload.update(extra)
    payload = {key: value for key, value in payload.items() if value is not None}

    try:
        return await create_subscription_event(
            db,
            user_id=user_id,
            event_type="renewal",
            subscription_id=subscription_id,
            transaction_id=transaction_id,
            amount_kopeks=amount_kopeks,
            message="Subscription renewed",
            occurred_at=occurred_at,
            extra=payload or None,
        )
    except Exception:
        logger.error(
            "Не удалось сохранить событие продления подписки для пользователя %s",
            user_id,
            exc_info=True,
        )
        try:
            await db.rollback()
        except Exception:
            logger.error("Не удалось выполнить rollback после ошибки записи события продления", exc_info=True)
        return None


async def list_subscription_events(
    db: AsyncSession,
    *,
    limit: int,
    offset: int,
    event_types: Optional[Iterable[str]] = None,
    user_id: Optional[int] = None,
) -> Tuple[list[SubscriptionEvent], int]:
    base_query = select(SubscriptionEvent)
    filters = []

    if event_types:
        filters.append(SubscriptionEvent.event_type.in_(set(event_types)))
    if user_id:
        filters.append(SubscriptionEvent.user_id == user_id)

    if filters:
        base_query = base_query.where(and_(*filters))

    total_query = base_query.with_only_columns(func.count()).order_by(None)
    total = await db.scalar(total_query) or 0

    result = await db.execute(
        base_query.options(selectinload(SubscriptionEvent.user))
        .order_by(SubscriptionEvent.occurred_at.desc())
        .offset(offset)
        .limit(limit)
    )

    return result.scalars().all(), int(total)
