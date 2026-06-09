"""CRUD для email-кодов подтверждения регистрации в веб-кабинете.

Код хранится только как HMAC-SHA256 (секрет — из настроек кабинета).
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import CabinetOtp

OTP_LENGTH = 6


def generate_otp_code() -> str:
    return f"{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}"


def hash_otp_code(code: str) -> str:
    secret = settings.get_cabinet_jwt_secret().encode()
    return hmac.new(secret, code.encode(), hashlib.sha256).hexdigest()


def verify_otp_code(code: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_otp_code(code), hashed)


async def get_active_otp(db: AsyncSession, email: str) -> Optional[CabinetOtp]:
    result = await db.execute(
        select(CabinetOtp).where(
            CabinetOtp.email == email,
            CabinetOtp.expires_at > datetime.utcnow(),
        )
    )
    return result.scalars().first()


async def create_otp(db: AsyncSession, email: str, code: str) -> CabinetOtp:
    """Создаёт новый код, удаляя предыдущие коды этого email."""
    await db.execute(delete(CabinetOtp).where(CabinetOtp.email == email))
    record = CabinetOtp(
        email=email,
        hashed_code=hash_otp_code(code),
        expires_at=datetime.utcnow() + timedelta(minutes=settings.CABINET_OTP_TTL_MINUTES),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def register_failed_attempt(db: AsyncSession, otp: CabinetOtp) -> int:
    otp.attempts = (otp.attempts or 0) + 1
    if otp.attempts >= settings.CABINET_OTP_MAX_ATTEMPTS:
        await db.delete(otp)
    await db.commit()
    return otp.attempts


async def delete_otp(db: AsyncSession, otp: CabinetOtp) -> None:
    await db.delete(otp)
    await db.commit()
