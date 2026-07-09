"""Авторизация пользователей личного кабинета (JWT, email/пароль, код подписки)."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.user import (
    get_user_by_email,
    get_user_by_id,
    get_user_by_referral_code,
    get_user_by_remnawave_uuid,
)
from app.database.models import Subscription, User, UserPromoGroup
from app.utils.passwords import verify_password

logger = logging.getLogger(__name__)

JWT_ALGORITHM = "HS256"
JWT_TYPE = "cabinet"
JWT_SSE_TYPE = "cabinet_sse"


class CabinetAuthService:
    def issue_token(self, user: User) -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user.id),
            "type": JWT_TYPE,
            "iat": int(now.timestamp()),
            "exp": int(
                (now + timedelta(hours=settings.CABINET_JWT_TTL_HOURS)).timestamp()
            ),
        }
        return jwt.encode(
            payload, settings.get_cabinet_jwt_secret(), algorithm=JWT_ALGORITHM
        )

    def decode_token(self, token: str) -> Optional[int]:
        try:
            payload = jwt.decode(
                token,
                settings.get_cabinet_jwt_secret(),
                algorithms=[JWT_ALGORITHM],
            )
        except jwt.PyJWTError as error:
            logger.debug(f"Невалидный JWT кабинета: {error}")
            return None

        if payload.get("type") != JWT_TYPE:
            return None

        sub = payload.get("sub")
        try:
            return int(sub)
        except (TypeError, ValueError):
            return None

    def issue_sse_ticket(self, user: User) -> str:
        """Короткоживущий тикет для SSE-стрима: EventSource не умеет
        Authorization-заголовок, поэтому тикет передаётся query-параметром.
        Короткий TTL ограничивает утечку через URL/логи."""
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user.id),
            "type": JWT_SSE_TYPE,
            "iat": int(now.timestamp()),
            "exp": int(
                (
                    now
                    + timedelta(
                        seconds=settings.CABINET_NOTIFICATIONS_SSE_TICKET_TTL_SECONDS
                    )
                ).timestamp()
            ),
        }
        return jwt.encode(
            payload, settings.get_cabinet_jwt_secret(), algorithm=JWT_ALGORITHM
        )

    def decode_sse_ticket(self, token: str) -> Optional[int]:
        try:
            payload = jwt.decode(
                token,
                settings.get_cabinet_jwt_secret(),
                algorithms=[JWT_ALGORITHM],
            )
        except jwt.PyJWTError as error:
            logger.debug(f"Невалидный SSE-тикет кабинета: {error}")
            return None

        if payload.get("type") != JWT_SSE_TYPE:
            return None

        try:
            return int(payload.get("sub"))
        except (TypeError, ValueError):
            return None

    async def authenticate_email(
        self, db: AsyncSession, email: str, password: str
    ) -> Optional[User]:
        if not email or not password:
            return None
        user = await get_user_by_email(db, email)
        if not user or not user.password_hash:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    async def authenticate_code(
        self, db: AsyncSession, code: str
    ) -> Optional[User]:
        """Вход по коду подписки/реф-коду (для существующих пользователей)."""
        if not code:
            return None

        normalized = code.strip()

        # 1) Реферальный код
        user = await get_user_by_referral_code(db, normalized)
        if user:
            return user

        # 2) UUID пользователя в remnawave
        user = await get_user_by_remnawave_uuid(db, normalized)
        if user:
            return user

        # 3) short uuid подписки
        result = await db.execute(
            select(Subscription)
            .options(
                selectinload(Subscription.user).selectinload(User.subscription),
                selectinload(Subscription.user)
                .selectinload(User.user_promo_groups)
                .selectinload(UserPromoGroup.promo_group),
            )
            .where(Subscription.remnawave_short_uuid == normalized)
        )
        subscription = result.scalar_one_or_none()
        if subscription and subscription.user:
            return subscription.user

        return None

    async def get_user_for_token(
        self, db: AsyncSession, user_id: int
    ) -> Optional[User]:
        return await get_user_by_id(db, user_id)


cabinet_auth_service = CabinetAuthService()
