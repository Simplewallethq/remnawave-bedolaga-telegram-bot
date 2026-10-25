"""Support actions for ChatSupport operators on one customer account.

Mounted below ``/cabinet/internal/support`` next to the read-only context. The
same Web API token (``X-API-Key``) opens it, and every write is additionally
signed with ``SUPPORT_ACTIONS_SECRET``:

    X-Support-Timestamp: <unix seconds>
    X-Support-Signature: hex(HMAC-SHA256(secret, "<timestamp>\\n<raw body>"))

A leaked API token can't change a balance, a request can't be forged or
altered without the secret, and a captured one is refused after five minutes.
The ``key`` inside the signed body runs an action once: a retry or replay gets
the first result back (Redis, 24 hours). Without the secret the write routes
answer 404.

The actions are the admin panel's own (handlers/admin/users.py): the same
balance service, tariff assignment, extension and device reset, so an operator
gets exactly what an admin would. The customer is not notified by the bot: the
operator is already talking to them in the support chat.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security, status
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.transaction import get_user_transactions
from app.database.crud.user import get_user_by_id
from app.database.models import SubscriptionEvent

from ..dependencies import get_db_session, require_api_token
from .support_context import support_context_with_account

logger = logging.getLogger(__name__)
router = APIRouter()

SIGNATURE_WINDOW_SECONDS = 300
IDEMPOTENCY_TTL_SECONDS = 24 * 3600
MAX_BODY_BYTES = 4096
# The admin panel's own cap on one balance change (100 000 ₽).
MAX_BALANCE_KOPEKS = 10_000_000
KEY_PATTERN = re.compile(r"^[A-Za-z0-9-]{8,64}$")

_redis: Optional[redis.Redis] = None


def _redis_client() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL)
    return _redis


def verify_signature(secret: str, ts_header: str, sig_header: str, body: bytes, now: float) -> bool:
    """Timestamp within the window and HMAC of "<timestamp>\\n<body>" matches."""
    try:
        ts = int((ts_header or "").strip())
        given = bytes.fromhex((sig_header or "").strip())
    except ValueError:
        return False
    if abs(now - ts) > SIGNATURE_WINDOW_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{ts}\n".encode() + body, hashlib.sha256).digest()
    return hmac.compare_digest(given, expected)


class ActionBase(BaseModel):
    reason: str = Field(min_length=1, max_length=300)
    operator: str = Field(min_length=1, max_length=100)
    key: str

    @field_validator("reason", "operator")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not KEY_PATTERN.match(value):
            raise ValueError("key must be 8-64 letters, digits or dashes")
        return value


class BalanceAction(ActionBase):
    amount_kopeks: int = Field(alias="amountKopeks")

    @field_validator("amount_kopeks")
    @classmethod
    def _amount(cls, value: int) -> int:
        if value == 0 or abs(value) > MAX_BALANCE_KOPEKS:
            raise ValueError("amount must be non-zero, up to 100 000 ₽")
        return value


class TariffAction(ActionBase):
    plan_id: int = Field(alias="planId", gt=0)
    period_days: int = Field(alias="periodDays", gt=0)


class ExtendAction(ActionBase):
    days: int = Field(gt=0, le=3650)


class DevicesAction(ActionBase):
    pass


async def _signed(request: Request, model: type[BaseModel]) -> Any:
    secret = settings.SUPPORT_ACTIONS_SECRET
    if not secret:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="body too large")
    if not verify_signature(secret, request.headers.get("X-Support-Timestamp", ""), request.headers.get("X-Support-Signature", ""), body, time.time()):
        logger.warning("support action refused: bad or stale signature from %s", request.client.host if request.client else "?")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad signature")
    try:
        return model.model_validate(json.loads(body))
    except (ValueError, ValidationError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)[:300]) from error


async def _once(key: str, run: Callable[[], Awaitable[Dict[str, Any]]]) -> Dict[str, Any]:
    """Run an action once per key; a repeat returns the first result. Redis down — refuse rather than risk a double."""
    client = _redis_client()
    redis_key = f"support:action:{key}"
    try:
        stored = await client.get(redis_key)
        if stored and stored != b"pending":
            return {**json.loads(stored), "duplicate": True}
        if not await client.set(redis_key, "pending", nx=True, ex=IDEMPOTENCY_TTL_SECONDS):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="the same request is in progress")
    except redis.RedisError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="idempotency store unavailable") from error
    try:
        result = await run()
    except BaseException:
        await client.delete(redis_key)
        raise
    await client.set(redis_key, json.dumps(result, default=str), ex=IDEMPOTENCY_TTL_SECONDS)
    return result


async def _account(db: AsyncSession, user_id: int):
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


async def _fresh_context(db: AsyncSession, user_id: int) -> Dict[str, Any]:
    db.expire_all()
    return support_context_with_account(await _account(db, user_id))


def _log(action: str, user_id: int, body: ActionBase, **details: Any) -> None:
    logger.info(
        "support action %s: user=%s operator=%s reason=%s key=%s %s",
        action, user_id, body.operator, body.reason, body.key, details,
    )


# ── Read ──────────────────────────────────────────────────────────────────────

@router.get("/users/{user_id}/history")
async def get_history(
    user_id: int,
    limit: int = Query(30, ge=1, le=100),
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """Balance transactions and subscription events, newest first."""
    await _account(db, user_id)
    transactions = await get_user_transactions(db, user_id, limit=limit)
    events = (
        await db.execute(
            select(SubscriptionEvent)
            .where(SubscriptionEvent.user_id == user_id)
            .order_by(SubscriptionEvent.occurred_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    items: List[Dict[str, Any]] = [
        {
            "at": t.created_at.isoformat() if t.created_at else None,
            "kind": "transaction",
            "type": t.type,
            "amountRub": round((t.amount_kopeks or 0) / 100, 2),
            "description": t.description,
            "paymentMethod": t.payment_method,
            "completed": bool(t.is_completed),
        }
        for t in transactions
    ] + [
        {
            "at": e.occurred_at.isoformat() if e.occurred_at else None,
            "kind": "subscription",
            "type": e.event_type,
            "amountRub": round(e.amount_kopeks / 100, 2) if e.amount_kopeks else None,
            "description": e.message,
            "periodDays": (e.extra or {}).get("period_days") if isinstance(e.extra, dict) else None,
            "source": (e.extra or {}).get("source") if isinstance(e.extra, dict) else None,
        }
        for e in events
    ]
    items.sort(key=lambda item: item["at"] or "", reverse=True)
    return {"items": items[:limit]}


@router.get("/plans")
async def get_plans(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """Active tariffs and the periods they can be assigned for."""
    from app.services.plan_pricing_service import SUPPORTED_PERIOD_DAYS, list_active_plans

    plans = await list_active_plans(db)
    return {
        "plans": [
            {
                "id": plan.id,
                "code": plan.code,
                "name": plan.display_name,
                "trafficLimitGb": plan.traffic_limit_gb or None,
                "deviceLimit": plan.device_limit,
            }
            for plan in plans
        ],
        "periods": list(SUPPORTED_PERIOD_DAYS),
    }


# ── Write (signed) ────────────────────────────────────────────────────────────

@router.post("/users/{user_id}/balance")
async def change_balance(
    user_id: int,
    request: Request,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """Credit (+) or debit (−) the balance, as «Пополнение/Списание администратором»."""
    body: BalanceAction = await _signed(request, BalanceAction)

    async def run() -> Dict[str, Any]:
        from app.services.user_service import UserService

        await _account(db, user_id)
        rub = abs(body.amount_kopeks) / 100
        verb = "Пополнение" if body.amount_kopeks > 0 else "Списание"
        description = f"{verb} поддержкой: {rub:g} ₽ — {body.reason} ({body.operator})"
        ok = await UserService().update_user_balance(db, user_id, body.amount_kopeks, description, admin_id=0, bot=None)
        if not ok:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not enough balance to debit")
        _log("balance", user_id, body, amount_kopeks=body.amount_kopeks)
        return {"ok": True, "context": await _fresh_context(db, user_id)}

    return await _once(body.key, run)


@router.post("/users/{user_id}/tariff")
async def assign_tariff(
    user_id: int,
    request: Request,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """Assign a tariff for a period, free of charge (admin «Назначение тарифа»)."""
    body: TariffAction = await _signed(request, TariffAction)

    async def run() -> Dict[str, Any]:
        from app.handlers.admin.users import _admin_assign_tariff
        from app.services.plan_pricing_service import SUPPORTED_PERIOD_DAYS

        if body.period_days not in SUPPORTED_PERIOD_DAYS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"period must be one of {list(SUPPORTED_PERIOD_DAYS)}")
        await _account(db, user_id)
        if not await _admin_assign_tariff(db, user_id, body.plan_id, body.period_days, 0):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="tariff not assigned: plan inactive or panel error")
        _log("tariff", user_id, body, plan_id=body.plan_id, period_days=body.period_days)
        return {"ok": True, "context": await _fresh_context(db, user_id)}

    return await _once(body.key, run)


@router.post("/users/{user_id}/extend")
async def extend_subscription(
    user_id: int,
    request: Request,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """Extend the current subscription by N days (admin «Продлить подписку»)."""
    body: ExtendAction = await _signed(request, ExtendAction)

    async def run() -> Dict[str, Any]:
        from app.handlers.admin.users import _extend_subscription_by_days

        user = await _account(db, user_id)
        if user.subscription is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no subscription to extend — assign a tariff")
        if not await _extend_subscription_by_days(db, user_id, body.days, 0):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="subscription not extended")
        _log("extend", user_id, body, days=body.days)
        return {"ok": True, "context": await _fresh_context(db, user_id)}

    return await _once(body.key, run)


@router.post("/users/{user_id}/devices/reset")
async def reset_devices(
    user_id: int,
    request: Request,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """Reset the customer's devices in the panel and revoke device links (admin «Сбросить устройства»)."""
    body: DevicesAction = await _signed(request, DevicesAction)

    async def run() -> Dict[str, Any]:
        from app.database.crud.device_link import revoke_all_device_links
        from app.services.remnawave_service import RemnaWaveService

        user = await _account(db, user_id)
        if not user.remnawave_uuid:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="the account is not linked to the panel")
        async with RemnaWaveService().get_api_client() as api:
            ok = await api.reset_user_devices(user.remnawave_uuid)
        if not ok:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="the panel did not reset devices")
        if user.subscription is not None:
            try:
                await revoke_all_device_links(db, user.subscription.id)
            except Exception as error:  # the reset itself succeeded; links are secondary
                logger.error("support devices reset: revoking links of user %s failed: %s", user_id, error)
        _log("devices_reset", user_id, body)
        return {"ok": True, "context": await _fresh_context(db, user_id)}

    return await _once(body.key, run)
