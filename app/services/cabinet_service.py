"""Сериализация данных пользователя в контракт фронта LetoVPNSite.

Единый источник построения ответов личного кабинета. Переиспользует
существующие CRUD/сервисы; не дублирует логику Telegram MiniApp.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral import (
    get_referral_earnings_by_user,
    get_user_referral_stats,
)
from app.database.crud.transaction import (
    get_user_transactions,
    get_user_transactions_count,
)
from app.database.models import Subscription, TransactionType, User
from app.services.plan_pricing_service import (
    get_lowest_monthly_price,
    list_active_plans,
)
from app.services.remnawave_service import RemnaWaveService

logger = logging.getLogger(__name__)


# ── Профиль ──────────────────────────────────────────────────────────────

def _account_code(user: User) -> str:
    """Стабильный человекочитаемый код аккаунта для входа в кабинет."""
    return user.referral_code or (
        user.subscription.remnawave_short_uuid
        if user.subscription and user.subscription.remnawave_short_uuid
        else f"user-{user.id}"
    )


def build_user_profile(user: User) -> Dict[str, Any]:
    code = _account_code(user)
    return {
        "id": code,
        "glyphSeed": user.remnawave_uuid or code,
        "subscriptionCode": code,
        "balanceRub": round(user.balance_kopeks / 100, 2),
        "email": user.email,
        "authSource": user.auth_source,
    }


# ── Подписка ─────────────────────────────────────────────────────────────

def build_subscription(user: User) -> Optional[Dict[str, Any]]:
    sub: Optional[Subscription] = user.subscription
    if not sub:
        return None

    plan = getattr(sub, "plan", None)
    plan_id = plan.code if plan else None
    plan_name = plan.display_name if plan else ("Trial" if sub.is_trial else "VPN")

    return {
        "planId": plan_id,
        "planName": plan_name,
        "devices": sub.device_limit,
        "status": sub.actual_status,
        "isTrial": sub.is_trial,
        "expiresAt": sub.end_date.isoformat() if sub.end_date else None,
        "startedAt": sub.start_date.isoformat() if sub.start_date else None,
        "daysLeft": sub.days_left,
        "trafficLimitGb": sub.traffic_limit_gb or None,
        "trafficUsedGb": round(sub.traffic_used_gb or 0.0, 2),
        "autoRenew": bool(sub.autopay_enabled),
        "subscriptionUrl": sub.subscription_url,
    }


# ── Тарифы ───────────────────────────────────────────────────────────────

def _plan_features(plan) -> List[Dict[str, Any]]:
    apps_only = bool(plan.custom_app_only)
    features = [
        {"key": "apps", "on": True},
        {"key": "vpn", "on": not apps_only},
        {"key": "bypass", "on": not apps_only},
    ]
    if plan.priority_support:
        features.append({"key": "priority", "on": True, "highlight": True})
    return features


def _serialize_plan(plan) -> Dict[str, Any]:
    monthly_kopeks = get_lowest_monthly_price(plan)
    traffic_gb = plan.traffic_limit_gb or 0
    return {
        "id": plan.code,
        "name": plan.display_name,
        "price": round(monthly_kopeks / 100) if monthly_kopeks else 0,
        "devices": plan.device_limit,
        "trafficGb": traffic_gb if traffic_gb > 0 else None,
        "unlimited": traffic_gb == 0,
        "popular": bool(plan.priority_support),
        "features": _plan_features(plan),
    }


async def build_plans(db: AsyncSession) -> List[Dict[str, Any]]:
    plans = await list_active_plans(db)
    return [_serialize_plan(plan) for plan in plans]


# ── Транзакции ───────────────────────────────────────────────────────────

_POSITIVE_TYPES = {
    TransactionType.DEPOSIT.value,
    TransactionType.REFUND.value,
    TransactionType.REFERRAL_REWARD.value,
    TransactionType.POLL_REWARD.value,
}

_LABEL_BY_TYPE = {
    TransactionType.DEPOSIT.value: "tx.label.topup",
    TransactionType.SUBSCRIPTION_PAYMENT.value: "tx.label.subscription",
    TransactionType.REFUND.value: "tx.label.refund",
    TransactionType.REFERRAL_REWARD.value: "tx.label.referral",
    TransactionType.POLL_REWARD.value: "tx.label.reward",
    TransactionType.WITHDRAWAL.value: "tx.label.withdrawal",
}

_METHOD_KIND = {
    "yookassa": "card",
    "cloudpayments": "card",
    "wata": "card",
    "platega": "card",
    "mulenpay": "card",
    "pal24": "sbp",
    "cryptobot": "usdt",
    "heleket": "usdt",
    "telegram_stars": "stars",
    "tribute": "card",
    "manual": "balance",
}


def _serialize_transaction(t) -> Dict[str, Any]:
    amount_rub = round(t.amount_kopeks / 100, 2)
    if t.type not in _POSITIVE_TYPES:
        amount_rub = -amount_rub

    method_kind = _METHOD_KIND.get(t.payment_method or "", "balance")

    return {
        "id": f"tx_{t.id}",
        "amount": amount_rub,
        "status": "success" if t.is_completed else "pending",
        "method": {"kind": method_kind},
        "date": t.created_at.isoformat() if t.created_at else None,
        "labelKey": _LABEL_BY_TYPE.get(t.type, "tx.label.topup"),
        "description": t.description,
    }


async def build_transactions(
    db: AsyncSession, user: User, limit: int = 50, offset: int = 0
) -> Dict[str, Any]:
    items = await get_user_transactions(db, user.id, limit=limit, offset=offset)
    total = await get_user_transactions_count(db, user.id)
    return {
        "items": [_serialize_transaction(t) for t in items],
        "total": total,
    }


# ── Рефералы ─────────────────────────────────────────────────────────────

def _referral_link(user: User) -> str:
    code = user.referral_code or ""
    base = (settings.CABINET_BASE_URL or "").rstrip("/")
    if base:
        return f"{base}/?ref={code}"
    bot_username = (settings.get_bot_username() or "").lstrip("@")
    if bot_username:
        return f"https://t.me/{bot_username}?start=ref{code}"
    return f"?ref={code}"


async def build_referral(db: AsyncSession, user: User) -> Dict[str, Any]:
    stats = await get_user_referral_stats(db, user.id)
    invited = stats.get("invited_count", 0) or 0
    earned_kopeks = stats.get("total_earned_kopeks", 0) or 0
    earned_rub = round(earned_kopeks / 100, 2)
    reward_per_friend = round(earned_rub / invited, 2) if invited else 0

    return {
        "code": user.referral_code,
        "link": _referral_link(user),
        "invited": invited,
        "activeReferrals": stats.get("active_referrals", 0) or 0,
        "earnedRub": earned_rub,
        "rewardPerFriendRub": reward_per_friend,
        "commissionPercent": settings.REFERRAL_COMMISSION_PERCENT,
    }


async def build_referral_payouts(
    db: AsyncSession, user: User, limit: int = 50, offset: int = 0
) -> List[Dict[str, Any]]:
    earnings = await get_referral_earnings_by_user(db, user.id, limit=limit, offset=offset)
    payouts: List[Dict[str, Any]] = []
    for e in earnings:
        referral = getattr(e, "referral", None)
        plan_name = None
        if referral and getattr(referral, "subscription", None):
            sub_plan = getattr(referral.subscription, "plan", None)
            plan_name = sub_plan.display_name if sub_plan else None
        payouts.append(
            {
                "id": f"rp_{e.id}",
                "friendIndex": e.referral_id,
                "planName": plan_name,
                "amount": round(e.amount_kopeks / 100, 2),
                "date": e.created_at.isoformat() if e.created_at else None,
            }
        )
    return payouts


# ── Устройства (remnawave HWID) ──────────────────────────────────────────

def _device_icon(platform: str) -> str:
    p = (platform or "").lower()
    if any(k in p for k in ("ios", "iphone", "android", "phone")):
        return "phone"
    if any(k in p for k in ("ipad", "tablet")):
        return "tablet"
    return "laptop"


def _serialize_device(raw: Dict[str, Any]) -> Dict[str, Any]:
    platform = raw.get("platform") or raw.get("osVersion") or "Unknown"
    name = raw.get("deviceModel") or raw.get("userAgent") or platform
    return {
        "id": raw.get("hwid"),
        "name": name,
        "platform": platform,
        "icon": _device_icon(platform),
        "online": True,
    }


async def build_devices(user: User) -> List[Dict[str, Any]]:
    if not user.remnawave_uuid:
        return []
    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            info = await api.get_user_devices(user.remnawave_uuid)
        devices = info.get("devices", []) if isinstance(info, dict) else []
        return [_serialize_device(d) for d in devices]
    except Exception as error:
        logger.warning(f"Не удалось получить устройства пользователя {user.id}: {error}")
        return []


async def remove_device(user: User, device_hwid: str) -> bool:
    if not user.remnawave_uuid:
        return False
    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            return await api.remove_device(user.remnawave_uuid, device_hwid)
    except Exception as error:
        logger.error(f"Ошибка удаления устройства {device_hwid}: {error}")
        return False


async def reset_devices(user: User) -> bool:
    if not user.remnawave_uuid:
        return False
    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            return await api.reset_user_devices(user.remnawave_uuid)
    except Exception as error:
        logger.error(f"Ошибка сброса устройств пользователя {user.id}: {error}")
        return False
