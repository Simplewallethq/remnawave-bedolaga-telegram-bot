import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AppleIapTransaction

logger = logging.getLogger(__name__)


async def get_by_transaction_id(
    db: AsyncSession,
    transaction_id: str,
) -> Optional[AppleIapTransaction]:
    result = await db.execute(
        select(AppleIapTransaction).where(AppleIapTransaction.transaction_id == transaction_id)
    )
    return result.scalar_one_or_none()


async def get_binding_by_original_transaction_id(
    db: AsyncSession,
    original_transaction_id: str,
) -> Optional[AppleIapTransaction]:
    result = await db.execute(
        select(AppleIapTransaction)
        .where(AppleIapTransaction.original_transaction_id == original_transaction_id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_record(
    db: AsyncSession,
    *,
    user_id: int,
    transaction_id: str,
    original_transaction_id: str,
    product_id: str,
    environment: str,
    expires_date: datetime,
    purchase_date: datetime,
    transaction_record_id: Optional[int] = None,
) -> AppleIapTransaction:
    record = AppleIapTransaction(
        user_id=user_id,
        transaction_id=transaction_id,
        original_transaction_id=original_transaction_id,
        product_id=product_id,
        environment=environment,
        expires_date=expires_date,
        purchase_date=purchase_date,
        transaction_record_id=transaction_record_id,
    )
    db.add(record)
    # No commit here on purpose: the unique constraint on transaction_id must be
    # checked (via flush) inside the same DB transaction as the subscription
    # mutation, so a concurrent duplicate raises IntegrityError before anything
    # else is written, and the caller's later commit (create_transaction) commits
    # this row and the subscription update together atomically.
    await db.flush()
    return record
