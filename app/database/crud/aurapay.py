"""CRUD для платежей AuraPay."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AurapayPayment

logger = logging.getLogger(__name__)


async def create_aurapay_payment(
    db: AsyncSession,
    *,
    user_id: int,
    order_id: str,
    amount_kopeks: int,
    description: Optional[str],
    status: str = AurapayPayment.STATUS_PENDING,
    invoice_id: Optional[str] = None,
    url: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    expires_at: Optional[datetime] = None,
) -> AurapayPayment:
    payment = AurapayPayment(
        user_id=user_id,
        order_id=order_id,
        invoice_id=invoice_id,
        amount_kopeks=amount_kopeks,
        currency="RUB",
        description=description,
        status=status,
        url=url,
        metadata_json=metadata or {},
        expires_at=expires_at,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)

    logger.info(
        "Создан AuraPay платеж #%s (%s) для пользователя %s: %s копеек",
        payment.id,
        order_id,
        user_id,
        amount_kopeks,
    )
    return payment


async def get_aurapay_payment_by_id(
    db: AsyncSession, payment_id: int
) -> Optional[AurapayPayment]:
    result = await db.execute(select(AurapayPayment).where(AurapayPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_aurapay_payment_by_order_id(
    db: AsyncSession, order_id: str
) -> Optional[AurapayPayment]:
    result = await db.execute(
        select(AurapayPayment).where(AurapayPayment.order_id == order_id)
    )
    return result.scalar_one_or_none()


async def get_aurapay_payment_by_invoice_id(
    db: AsyncSession, invoice_id: str
) -> Optional[AurapayPayment]:
    result = await db.execute(
        select(AurapayPayment).where(AurapayPayment.invoice_id == invoice_id)
    )
    return result.scalar_one_or_none()


async def mark_aurapay_payment_paid_cas(
    db: AsyncSession,
    payment_id: int,
    *,
    paid_at: Optional[datetime] = None,
) -> bool:
    """Атомарно помечает платёж оплаченным. True — мы выиграли гонку.

    AuraPay ретраит вебхук до 5 раз, а кнопка «Проверить оплату» и
    автосверка могут прийти параллельно — зачёт должен пройти ровно один раз.
    """
    result = await db.execute(
        update(AurapayPayment)
        .where(
            and_(
                AurapayPayment.id == payment_id,
                AurapayPayment.is_paid == False,  # noqa: E712
            )
        )
        .values(
            is_paid=True,
            paid_at=paid_at or datetime.utcnow(),
            status=AurapayPayment.STATUS_PAID,
            updated_at=datetime.utcnow(),
        )
    )
    await db.commit()
    return (result.rowcount or 0) == 1


async def update_aurapay_payment(
    db: AsyncSession,
    payment: AurapayPayment,
    *,
    status: Optional[str] = None,
    invoice_id: Optional[str] = None,
    url: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    callback_payload: Optional[Dict[str, Any]] = None,
) -> AurapayPayment:
    values: Dict[str, Any] = {}
    if status is not None:
        values["status"] = status
    if invoice_id is not None:
        values["invoice_id"] = invoice_id
    if url is not None:
        values["url"] = url
    if metadata is not None:
        values["metadata_json"] = metadata
    if callback_payload is not None:
        values["callback_payload"] = callback_payload

    if not values:
        return payment

    values["updated_at"] = datetime.utcnow()
    await db.execute(
        update(AurapayPayment).where(AurapayPayment.id == payment.id).values(**values)
    )
    await db.commit()
    await db.refresh(payment)
    return payment


async def link_aurapay_payment_to_transaction(
    db: AsyncSession, payment: AurapayPayment, transaction_id: int
) -> AurapayPayment:
    await db.execute(
        update(AurapayPayment)
        .where(AurapayPayment.id == payment.id)
        .values(transaction_id=transaction_id, updated_at=datetime.utcnow())
    )
    await db.commit()
    await db.refresh(payment)
    return payment
