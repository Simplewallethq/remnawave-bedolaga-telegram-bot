"""CRUD для журнала маршрутизации платежей между универсальными шлюзами."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    PaymentRoutingLog,
    PlategaPayment,
    WataPayment,
    YooKassaPayment,
)

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_ISSUED = "issued"
STATUS_FAILED = "failed"
STATUS_PAID = "paid"

# Модель провайдера и колонка со статусом оплаты — для backfill.
_PROVIDER_MODELS = {
    "platega": (PlategaPayment, "is_paid", "paid_at"),
    "wata": (WataPayment, "is_paid", "paid_at"),
    "yookassa": (YooKassaPayment, "is_paid", "captured_at"),
}


async def create_routing_log(
    db: AsyncSession,
    *,
    user_id: Optional[int],
    source: str,
    amount_kopeks: int,
    requested_gateway: str,
    weights: Optional[Dict[str, int]] = None,
) -> PaymentRoutingLog:
    entry = PaymentRoutingLog(
        user_id=user_id,
        source=source,
        amount_kopeks=int(amount_kopeks),
        requested_gateway=requested_gateway,
        status=STATUS_PENDING,
        weights_json=weights or {},
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


async def mark_routing_issued(
    db: AsyncSession,
    log_id: int,
    *,
    gateway: str,
    local_payment_id: Optional[int],
    external_id: Optional[str],
    payment_url: Optional[str],
    fallback_used: bool,
    attempts: Optional[List[Dict[str, Any]]] = None,
    expires_at: Optional[datetime] = None,
) -> None:
    await db.execute(
        update(PaymentRoutingLog)
        .where(PaymentRoutingLog.id == log_id)
        .values(
            gateway=gateway,
            status=STATUS_ISSUED,
            local_payment_id=local_payment_id,
            external_id=external_id,
            payment_url=payment_url,
            fallback_used=bool(fallback_used),
            attempts_json=attempts or [],
            expires_at=expires_at,
            updated_at=datetime.utcnow(),
        )
    )
    await db.commit()


async def mark_routing_failed(
    db: AsyncSession,
    log_id: int,
    *,
    attempts: Optional[List[Dict[str, Any]]] = None,
) -> None:
    await db.execute(
        update(PaymentRoutingLog)
        .where(PaymentRoutingLog.id == log_id)
        .values(
            status=STATUS_FAILED,
            attempts_json=attempts or [],
            updated_at=datetime.utcnow(),
        )
    )
    await db.commit()


async def mark_routing_paid(
    db: AsyncSession,
    *,
    gateway: str,
    local_payment_id: int,
    transaction_id: Optional[int] = None,
    amount_kopeks: Optional[int] = None,
    paid_at: Optional[datetime] = None,
) -> bool:
    """Отмечает факт оплаты. Идемпотентна: повторный вызов ничего не меняет."""

    result = await db.execute(
        select(PaymentRoutingLog)
        .where(
            PaymentRoutingLog.gateway == gateway,
            PaymentRoutingLog.local_payment_id == local_payment_id,
        )
        .order_by(PaymentRoutingLog.id.desc())
        .limit(1)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        return False
    if entry.status == STATUS_PAID:
        return True

    entry.status = STATUS_PAID
    entry.paid_at = paid_at or datetime.utcnow()
    if transaction_id is not None:
        entry.transaction_id = transaction_id
    entry.paid_amount_kopeks = (
        int(amount_kopeks) if amount_kopeks is not None else entry.amount_kopeks
    )
    entry.updated_at = datetime.utcnow()
    await db.commit()
    return True


async def backfill_routing_paid_flags(
    db: AsyncSession,
    *,
    since: datetime,
) -> int:
    """Догоняет пропущенные отметки оплаты по таблицам провайдеров.

    Страховка на случай, если хук в finalize не отработал: расхождения
    самозалечиваются при открытии экрана статистики, фоновый цикл не нужен.
    """

    updated = 0

    for gateway, (model, paid_field, paid_at_field) in _PROVIDER_MODELS.items():
        result = await db.execute(
            select(PaymentRoutingLog.id, PaymentRoutingLog.local_payment_id)
            .where(
                PaymentRoutingLog.gateway == gateway,
                PaymentRoutingLog.status == STATUS_ISSUED,
                PaymentRoutingLog.created_at >= since,
                PaymentRoutingLog.local_payment_id.isnot(None),
            )
        )
        rows = result.all()
        if not rows:
            continue

        by_payment_id = {row.local_payment_id: row.id for row in rows}
        paid_result = await db.execute(
            select(model.id, getattr(model, paid_at_field)).where(
                model.id.in_(list(by_payment_id.keys())),
                getattr(model, paid_field).is_(True),
            )
        )

        for payment_id, paid_at in paid_result.all():
            log_id = by_payment_id.get(payment_id)
            if log_id is None:
                continue
            await db.execute(
                update(PaymentRoutingLog)
                .where(PaymentRoutingLog.id == log_id)
                .values(
                    status=STATUS_PAID,
                    paid_at=paid_at or datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
            )
            updated += 1

    if updated:
        await db.commit()
        logger.info("Backfill журнала роутинга: доотмечено оплат: %s", updated)

    return updated


async def routing_stats(
    db: AsyncSession,
    *,
    since: datetime,
    until: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Сводка по шлюзам: сколько назначено, выставлено, оплачено."""

    conditions = [PaymentRoutingLog.created_at >= since]
    if until is not None:
        conditions.append(PaymentRoutingLog.created_at <= until)

    result = await db.execute(
        select(
            PaymentRoutingLog.requested_gateway,
            func.count(PaymentRoutingLog.id),
            func.count(PaymentRoutingLog.id).filter(
                PaymentRoutingLog.status != STATUS_FAILED
            ),
            func.count(PaymentRoutingLog.id).filter(
                PaymentRoutingLog.status == STATUS_PAID
            ),
            func.count(PaymentRoutingLog.id).filter(
                PaymentRoutingLog.fallback_used.is_(True)
            ),
            func.coalesce(func.sum(PaymentRoutingLog.paid_amount_kopeks), 0),
            func.count(func.distinct(PaymentRoutingLog.user_id)),
        )
        .where(*conditions)
        .group_by(PaymentRoutingLog.requested_gateway)
        .order_by(PaymentRoutingLog.requested_gateway)
    )

    stats: List[Dict[str, Any]] = []
    for row in result.all():
        requested, total, issued, paid, fallbacks, paid_sum, users = row
        stats.append(
            {
                "gateway": requested,
                "requested": int(total or 0),
                "issued": int(issued or 0),
                "paid": int(paid or 0),
                "fallbacks": int(fallbacks or 0),
                "paid_kopeks": int(paid_sum or 0),
                "users": int(users or 0),
            }
        )
    return stats
