"""Server-to-server customer context for the Tendi support dashboard.

Mounted below ``/cabinet/internal/support`` so the existing HTTPS cabinet
reverse proxy can expose it. Authentication uses the Web API token mechanism
(``X-API-Key``), not a cabinet user's JWT.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Security, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.user import (
    get_user_by_support_account_id,
    get_user_by_telegram_id,
)
from app.services import cabinet_service

from ..dependencies import get_db_session, require_api_token

router = APIRouter()


@router.get("/users/by-telegram-id/{telegram_id}")
async def get_support_context_by_telegram_id(
    telegram_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """Return a sanitized support snapshot for a Telegram user."""
    user = await get_user_by_telegram_id(db, telegram_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return cabinet_service.build_support_context(user)


@router.get("/users/by-account-id/{account_id}")
async def get_support_context_by_account_id(
    account_id: str,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """Return the same support snapshot for a signed-in cabinet account."""
    user = await get_user_by_support_account_id(db, account_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return cabinet_service.build_support_context(user)
