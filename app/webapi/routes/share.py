"""Публичный резолв share-токена для страницы «Поделиться доступом» (/s/{token}).

Эндпоинт без авторизации: страница на сайте дергает его при каждом открытии,
чтобы получить актуальное состояние (свежий код/ключ или причину отказа).
ВАЖНО: токены, share-коды и ссылки подписки здесь не логируются.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.device_link import count_device_links
from app.database.crud.share_token import get_by_token
from app.database.models import Subscription
from app.utils.subscription_utils import (
    build_incy_deep_link,
    convert_subscription_link_to_happ_scheme,
    get_display_subscription_link,
    get_raw_subscription_link,
)

from ..dependencies import get_db_session
from ..schemas.share import ShareReferral, ShareResolveRequest, ShareResolveResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Rate limit ───────────────────────────────────────────────────────────
# Простое скользящее окно в памяти процесса. При WEB_API_WORKERS > 1 бюджет
# умножается на число воркеров (по умолчанию воркер один — этого достаточно).
_RATE_LIMIT_MAX = 30
_RATE_LIMIT_WINDOW = 60.0
_rate_buckets: dict[str, deque] = defaultdict(deque)
_RATE_BUCKETS_PRUNE_AT = 10_000


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    bucket = _rate_buckets[ip]
    while bucket and now - bucket[0] > _RATE_LIMIT_WINDOW:
        bucket.popleft()
    if len(bucket) >= _RATE_LIMIT_MAX:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests",
        )
    bucket.append(now)

    if len(_rate_buckets) > _RATE_BUCKETS_PRUNE_AT:
        for key in [k for k, b in _rate_buckets.items() if not b or now - b[-1] > _RATE_LIMIT_WINDOW]:
            _rate_buckets.pop(key, None)


# ── Резолв ───────────────────────────────────────────────────────────────

def _referral(subscription: Subscription) -> ShareReferral | None:
    bot_username = settings.get_bot_username()
    user = getattr(subscription, "user", None)
    referral_code = getattr(user, "referral_code", None) if user else None
    if not bot_username or not referral_code:
        return None
    return ShareReferral(url=f"https://t.me/{bot_username}?start={referral_code}")


@router.post(
    "/resolve",
    response_model=ShareResolveResponse,
    response_model_exclude_none=True,
    summary="Resolve a share-page token into its current state",
)
async def resolve_share_token(
    payload: ShareResolveRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> ShareResolveResponse:
    _check_rate_limit(_client_ip(request))

    record = await get_by_token(db, payload.token)
    # Отозванный/исчерпанный токен неотличим от несуществующего: странице
    # (и любопытным) знать разницу не нужно.
    if record is None or not record.is_active:
        return ShareResolveResponse(status="not_found")

    sub_result = await db.execute(
        select(Subscription)
        .options(selectinload(Subscription.user))
        .where(Subscription.id == record.subscription_id)
    )
    subscription = sub_result.scalar_one_or_none()
    if subscription is None:
        return ShareResolveResponse(status="not_found")

    referral = _referral(subscription)

    if subscription.actual_status not in ("active", "trial"):
        # Продление вернёт actual_status в active — страница оживёт с тем же токеном.
        return ShareResolveResponse(status="paused", referral=referral)

    devices_count = await count_device_links(db, subscription.id)
    device_limit = subscription.device_limit or 1
    if devices_count >= device_limit:
        return ShareResolveResponse(status="slots_full", referral=referral)

    raw_link = get_display_subscription_link(subscription)
    plain_link = get_raw_subscription_link(subscription)
    happ_link = convert_subscription_link_to_happ_scheme(raw_link)
    # Incy добавляет подписку по обычной ссылке из Remnawave, а не по криптоссылке.
    incy_link = build_incy_deep_link(plain_link or raw_link)

    return ShareResolveResponse(
        status="active",
        shareCode=record.share_code,
        happLink=happ_link,
        incyLink=incy_link,
        rawLink=raw_link,
        subLink=plain_link,
        platforms=settings.get_share_platform_map(),
        referral=referral,
    )
