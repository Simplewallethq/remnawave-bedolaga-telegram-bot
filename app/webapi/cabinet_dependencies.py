"""FastAPI-зависимости авторизации личного кабинета (Bearer JWT)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User, UserStatus
from app.services.cabinet_auth_service import cabinet_auth_service
from app.webapi.dependencies import get_db_session

logger = logging.getLogger(__name__)

LAST_ACTIVITY_TOUCH_INTERVAL = timedelta(hours=1)


def _extract_bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("Authorization")
    if not authorization:
        return None
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credentials:
        return None
    return credentials.strip()


async def get_current_cabinet_user(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> User:
    if not settings.CABINET_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cabinet is disabled",
        )

    token = _extract_bearer_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )

    user_id = cabinet_auth_service.decode_token(token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user = await cabinet_auth_service.get_user_for_token(db, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    if user.status == UserStatus.BLOCKED.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is blocked",
        )

    await _touch_last_activity(db, user)

    return user


async def _touch_last_activity(db: AsyncSession, user: User) -> None:
    """Активность в кабинете/приложении, иначе чистка неактивных считает юзера мёртвым."""
    now = datetime.utcnow()
    if user.last_activity and now - user.last_activity < LAST_ACTIVITY_TOUCH_INTERVAL:
        return
    try:
        user.last_activity = now
        await db.commit()
    except Exception as error:  # активность не должна ломать запрос
        logger.warning("Не удалось обновить last_activity юзера %s: %s", user.id, error)
        await db.rollback()
