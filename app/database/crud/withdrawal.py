import logging
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import WithdrawalRequest, WithdrawalStatus

logger = logging.getLogger(__name__)


async def create_withdrawal_request(
    db: AsyncSession,
    *,
    user_id: int,
    amount_kopeks: int,
    details: str,
    debit_transaction_id: Optional[int] = None,
    commit: bool = True,
) -> WithdrawalRequest:
    withdrawal = WithdrawalRequest(
        user_id=user_id,
        amount_kopeks=amount_kopeks,
        details=details,
        status=WithdrawalStatus.PENDING.value,
        debit_transaction_id=debit_transaction_id,
    )
    db.add(withdrawal)
    if commit:
        await db.commit()
        await db.refresh(withdrawal)
    else:
        await db.flush()
    return withdrawal


async def get_withdrawal_by_id(db: AsyncSession, withdrawal_id: int) -> Optional[WithdrawalRequest]:
    result = await db.execute(
        select(WithdrawalRequest).where(WithdrawalRequest.id == withdrawal_id)
    )
    return result.scalar_one_or_none()


async def get_user_withdrawals(
    db: AsyncSession,
    user_id: int,
    limit: int = 50,
) -> List[WithdrawalRequest]:
    result = await db.execute(
        select(WithdrawalRequest)
        .where(WithdrawalRequest.user_id == user_id)
        .order_by(WithdrawalRequest.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_user_withdrawal_sums(db: AsyncSession, user_id: int) -> Dict[str, int]:
    """Суммы заявок пользователя по статусам одним GROUP BY (копейки)."""
    result = await db.execute(
        select(
            WithdrawalRequest.status,
            func.coalesce(func.sum(WithdrawalRequest.amount_kopeks), 0),
        )
        .where(WithdrawalRequest.user_id == user_id)
        .group_by(WithdrawalRequest.status)
    )
    by_status = dict(result.all())
    return {
        "pending_kopeks": int(by_status.get(WithdrawalStatus.PENDING.value, 0)),
        "paid_kopeks": int(by_status.get(WithdrawalStatus.PAID.value, 0)),
    }


async def mark_withdrawal_paid(
    db: AsyncSession,
    withdrawal: WithdrawalRequest,
    *,
    processed_by: Optional[int] = None,
    commit: bool = True,
) -> WithdrawalRequest:
    withdrawal.status = WithdrawalStatus.PAID.value
    withdrawal.processed_by = processed_by
    withdrawal.processed_at = datetime.utcnow()
    if commit:
        await db.commit()
        await db.refresh(withdrawal)
    else:
        await db.flush()
    return withdrawal


async def mark_withdrawal_rejected(
    db: AsyncSession,
    withdrawal: WithdrawalRequest,
    *,
    processed_by: Optional[int] = None,
    refund_transaction_id: Optional[int] = None,
    commit: bool = True,
) -> WithdrawalRequest:
    withdrawal.status = WithdrawalStatus.REJECTED.value
    withdrawal.processed_by = processed_by
    withdrawal.processed_at = datetime.utcnow()
    if refund_transaction_id is not None:
        withdrawal.refund_transaction_id = refund_transaction_id
    if commit:
        await db.commit()
        await db.refresh(withdrawal)
    else:
        await db.flush()
    return withdrawal
