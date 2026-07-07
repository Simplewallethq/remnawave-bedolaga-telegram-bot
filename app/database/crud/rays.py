import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import RayTransaction, RayTransactionType, User

logger = logging.getLogger(__name__)


async def add_user_rays(
    db: AsyncSession,
    user: User,
    amount: int,
    *,
    type: RayTransactionType = RayTransactionType.REFERRAL_PURCHASE,
    referral_id: Optional[int] = None,
    source_transaction_id: Optional[int] = None,
    period_days: Optional[int] = None,
    description: Optional[str] = None,
    commit: bool = True,
    count_lifetime: bool = True,
) -> Optional[RayTransaction]:
    """Начисляет лучи владельцу кошелька (рефереру) и пишет строку в журнал.

    Обновляет кэш-балансы (rays_balance и rays_lifetime_earned) и добавляет
    append-only запись RayTransaction. Идемпотентность обеспечивает UNIQUE на
    source_transaction_id: повторное начисление за тот же платёж даёт
    IntegrityError → откат и возврат None (лучи не задваиваются).

    commit=False оставляет изменения во внешней транзакции (db.flush()), чтобы
    начисление лежало в одном коммите с покупкой (паттерн subtract_user_balance).
    """
    try:
        ray_tx = RayTransaction(
            user_id=user.id,
            amount=amount,
            type=type.value,
            referral_id=referral_id,
            source_transaction_id=source_transaction_id,
            period_days=period_days,
            description=description,
        )
        db.add(ray_tx)

        old_balance = user.rays_balance or 0
        user.rays_balance = old_balance + amount
        # lifetime — неубывающий счётчик: растёт только на начислениях.
        # Возвраты (REFUND) заработком не считаются — count_lifetime=False.
        if amount > 0 and count_lifetime:
            user.rays_lifetime_earned = (user.rays_lifetime_earned or 0) + amount
        user.updated_at = datetime.utcnow()

        if commit:
            await db.commit()
            await db.refresh(ray_tx)
        else:
            await db.flush()

        logger.info(
            "✨ Лучи пользователя %s изменены: %s → %s (изменение: %+d, source_tx=%s)",
            user.telegram_id, old_balance, user.rays_balance, amount, source_transaction_id,
        )
        return ray_tx

    except IntegrityError:
        await db.rollback()
        logger.info(
            "✨ Лучи за source_transaction_id=%s уже начислены — пропуск (идемпотентность)",
            source_transaction_id,
        )
        return None
    except Exception as e:
        logger.error(f"Ошибка начисления лучей пользователю {user.id}: {e}")
        await db.rollback()
        return None


async def subtract_user_rays(
    db: AsyncSession,
    user: User,
    amount: int,
    *,
    type: RayTransactionType = RayTransactionType.SPEND,
    description: Optional[str] = None,
    commit: bool = True,
) -> Optional[RayTransaction]:
    """Списывает лучи с кошелька (rays_balance, lifetime не трогается).

    Задел на будущее (призы/обмен) — пока к UI не подключено. Возвращает None,
    если на балансе недостаточно лучей.
    """
    if amount <= 0:
        return None
    if (user.rays_balance or 0) < amount:
        logger.warning(
            "Недостаточно лучей у пользователя %s: баланс %s, требуется %s",
            user.id, user.rays_balance, amount,
        )
        return None
    return await add_user_rays(
        db, user, -amount, type=type, description=description, commit=commit,
    )


async def get_user_ray_transactions(
    db: AsyncSession,
    user_id: int,
    limit: int = 20,
    offset: int = 0,
) -> List[RayTransaction]:
    result = await db.execute(
        select(RayTransaction)
        .where(RayTransaction.user_id == user_id)
        .order_by(RayTransaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def get_user_ray_transactions_count(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(
        select(func.count(RayTransaction.id)).where(RayTransaction.user_id == user_id)
    )
    return int(result.scalar() or 0)
