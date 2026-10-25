"""Server-to-server customer context for the Tendi support dashboard.

Mounted below ``/cabinet/internal/support`` so the existing HTTPS cabinet
reverse proxy can expose it. Authentication uses the Web API token mechanism
(``X-API-Key``), not a cabinet user's JWT.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.user import (
    get_user_by_id,
    get_user_by_support_account_id,
)
from app.database.models import User
from app.services import cabinet_service
from app.utils.bot_registry import account_scope_for_bot, get_brand_for_bot, is_copycat_bot

from ..dependencies import get_db_session, require_api_token

router = APIRouter()


async def find_support_user(db: AsyncSession, telegram_id: int, prefer: str = "main") -> Tuple[Optional[User], int]:
    """The customer's account for support, whichever bot it lives in.

    Copycat bots keep a separate account per bot (``account_scope_bot_id``), so a
    Telegram user may have several. The bot's own lookup sees only its scope;
    support needs any of them: the main-scope account first (``prefer="copy"`` —
    a copycat one first), then the most recently active. Also returns how many
    accounts the Telegram user has.
    """
    rows = (
        await db.execute(
            select(User.id, User.account_scope_bot_id)
            .where(User.telegram_id == telegram_id)
            .order_by(User.last_activity.desc().nullslast(), User.id.desc())
        )
    ).all()
    if not rows:
        return None, 0
    primary_scope = account_scope_for_bot(None)
    in_main = [r.id for r in rows if r.account_scope_bot_id is None or r.account_scope_bot_id == primary_scope]
    in_copy = [r.id for r in rows if r.id not in in_main]
    ordered = (in_copy + in_main) if prefer == "copy" else (in_main + in_copy)
    return await get_user_by_id(db, ordered[0]), len(rows)


def support_context_with_account(user: User, accounts: int = 1) -> Dict[str, Any]:
    """The support snapshot plus which bot (brand) the account belongs to."""
    context = cabinet_service.build_support_context(user)
    bot_id = getattr(user, "bot_id", None)
    context["account"] = {
        "botId": bot_id,
        "brand": get_brand_for_bot(bot_id).name,
        "isCopycat": is_copycat_bot(bot_id),
        "accounts": accounts,
    }
    return context


@router.get("/users/by-telegram-id/{telegram_id}")
async def get_support_context_by_telegram_id(
    telegram_id: int,
    prefer: str = Query("main", pattern="^(main|copy)$"),
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """Return a sanitized support snapshot for a Telegram user, in any bot."""
    user, accounts = await find_support_user(db, telegram_id, prefer)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return support_context_with_account(user, accounts)


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
    return support_context_with_account(user)
