"""Сериализация данных пользователя в контракт фронта LetoVPNSite.

Единый источник построения ответов личного кабинета. Переиспользует
существующие CRUD/сервисы; не дублирует логику Telegram MiniApp.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.device_link import (
    revoke_all_device_links,
    revoke_device_links,
)
from app.database.crud.referral import (
    get_referral_earnings_by_user,
    get_user_referral_stats,
)
from app.database.crud.transaction import (
    get_user_transactions,
    get_user_transactions_count,
)
from app.database.models import (
    RayTransaction,
    ReferralEarning,
    Subscription,
    Transaction,
    TransactionType,
    User,
)
from app.utils.user_utils import (
    get_effective_referral_commission_percent,
    is_rays_shop_available_for,
)
from app.services.plan_pricing_service import (
    get_lowest_monthly_price,
    list_active_plans,
)
from app.services.cold_solo_offer_service import cold_solo_offer_service
from app.services.legacy_pro_offer_service import legacy_pro_offer_service
from app.services.remnawave_service import RemnaWaveService
from app.utils.subscription_utils import (
    build_incy_deep_link,
    convert_subscription_link_to_happ_scheme,
    get_display_subscription_link,
    get_happ_cryptolink_redirect_link,
    get_raw_subscription_link,
)

logger = logging.getLogger(__name__)


# ── Профиль ──────────────────────────────────────────────────────────────

def _subscription_code(user: User) -> Optional[str]:
    """Реальный код подписки (идентификатор в RemnaWave), НЕ реферальный код."""
    sub = user.subscription
    if sub and sub.remnawave_short_uuid:
        return sub.remnawave_short_uuid
    return None


def _account_id(user: User) -> str:
    """Стабильный отображаемый ID аккаунта (не реферальный код)."""
    sub = user.subscription
    if sub and sub.remnawave_short_uuid:
        return sub.remnawave_short_uuid
    if user.remnawave_uuid:
        return user.remnawave_uuid
    return f"user-{user.id}"


def build_user_profile(user: User) -> Dict[str, Any]:
    account_id = _account_id(user)
    return {
        "id": account_id,
        "glyphSeed": user.remnawave_uuid or account_id,
        "subscriptionCode": _subscription_code(user),
        "balanceRub": round(user.balance_kopeks / 100, 2),
        "email": user.email,
        "authSource": user.auth_source,
        "tgUsername": user.username or None,
        "tgId": user.telegram_id,
        "firstName": user.first_name or None,
    }


# ── Подписка ─────────────────────────────────────────────────────────────

def _happ_connect_link(sub: Subscription) -> Optional[str]:
    """Happ-ссылка для кнопки «Подключиться» на сайте.

    Повторяет логику дефолтной кнопки «Подключиться» Telegram-бота: берём
    отображаемую ссылку подписки (крипто-ссылку панели в режиме happ_cryptolink,
    иначе обычный subscription_url) и оборачиваем её в редирект Happ либо
    конвертируем схему в happ://.
    """
    subscription_link = get_display_subscription_link(sub)
    if not subscription_link:
        return None

    redirect_link = get_happ_cryptolink_redirect_link(subscription_link)
    happ_scheme_link = convert_subscription_link_to_happ_scheme(subscription_link)
    return redirect_link or happ_scheme_link or subscription_link


def _incy_connect_link(sub: Subscription) -> Optional[str]:
    """Incy-ссылка для кнопки «Подключиться» на Apple-устройствах.

    Incy добавляет подписку по обычной ссылке из Remnawave, а не по
    криптоссылке — как и в /cabinet/share/resolve. Redirect-страница
    (HAPP_CRYPTOLINK_REDIRECT_TEMPLATE) тут не нужна: она обходит запрет
    Telegram на кастомные схемы в inline-кнопках, а в браузере incy://
    открывается напрямую.
    """
    subscription_link = get_raw_subscription_link(sub) or get_display_subscription_link(sub)
    return build_incy_deep_link(subscription_link)


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
        "happLink": _happ_connect_link(sub),
        "incyLink": _incy_connect_link(sub),
    }


def build_support_context(user: User) -> Dict[str, Any]:
    """Safe server-to-server context for the Tendi operator dashboard.

    Unlike the public cabinet subscription response, this payload deliberately
    excludes subscription/deep links: they grant VPN access and must never be
    copied into a support CRM. The response contains only facts an operator
    needs to identify the customer and understand the current subscription.
    """
    profile = build_user_profile(user)
    sub: Optional[Subscription] = user.subscription
    subscription: Optional[Dict[str, Any]] = None

    if sub:
        plan = getattr(sub, "plan", None)
        subscription = {
            "planCode": plan.code if plan else None,
            "planName": plan.display_name if plan else ("Trial" if sub.is_trial else "VPN"),
            "status": sub.actual_status,
            "isTrial": bool(sub.is_trial),
            "startedAt": sub.start_date.isoformat() if sub.start_date else None,
            "expiresAt": sub.end_date.isoformat() if sub.end_date else None,
            "daysLeft": sub.days_left,
            "deviceLimit": sub.device_limit,
            "trafficLimitGb": sub.traffic_limit_gb or None,
            "trafficUsedGb": round(sub.traffic_used_gb or 0.0, 2),
            "autoRenew": bool(sub.autopay_enabled),
        }

    return {
        "user": {
            "accountId": profile["id"],
            "internalUserId": user.id,
            "status": user.status,
            "email": profile["email"],
            "firstName": profile["firstName"],
            "telegramUsername": profile["tgUsername"],
            "telegramId": profile["tgId"],
            "balanceRub": profile["balanceRub"],
        },
        "subscription": subscription,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
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


async def _serialize_plan(
    db: AsyncSession,
    plan,
    cohort: str = "new",
    user: Optional[User] = None,
) -> Dict[str, Any]:
    monthly_kopeks = get_lowest_monthly_price(plan, cohort)
    traffic_gb = plan.traffic_limit_gb or 0
    payload = {
        "id": plan.code,
        "name": plan.display_name,
        "price": round(monthly_kopeks / 100) if monthly_kopeks else 0,
        "devices": plan.device_limit,
        "trafficGb": traffic_gb if traffic_gb > 0 else None,
        "unlimited": traffic_gb == 0,
        "popular": bool(plan.priority_support),
        "features": _plan_features(plan),
    }

    if user is not None:
        offer_price, offer = await cold_solo_offer_service.get_price_override(
            db,
            user.id,
            plan_code=plan.code,
            period_days=cold_solo_offer_service.PERIOD_DAYS,
        )
        if offer_price is not None and offer is not None:
            payload["activeOffer"] = {
                "type": cold_solo_offer_service.NOTIFICATION_TYPE,
                "periodDays": cold_solo_offer_service.PERIOD_DAYS,
                "priceKopeks": offer_price,
                "priceRub": round(offer_price / 100),
                "originalPriceKopeks": cold_solo_offer_service.ORIGINAL_PRICE_KOPEKS,
                "originalPriceRub": round(cold_solo_offer_service.ORIGINAL_PRICE_KOPEKS / 100),
                "expiresAt": offer.expires_at.isoformat() if offer.expires_at else None,
            }
            payload["yearPriceKopeks"] = offer_price
            payload["yearPriceRub"] = round(offer_price / 100)
        else:
            offer_price, offer = await legacy_pro_offer_service.get_price_override(
                db,
                user.id,
                plan_code=plan.code,
                period_days=legacy_pro_offer_service.PERIOD_DAYS,
            )
            if offer_price is not None and offer is not None:
                payload["activeOffer"] = {
                    "type": getattr(offer, "notification_type", None),
                    "periodDays": legacy_pro_offer_service.PERIOD_DAYS,
                    "priceKopeks": offer_price,
                    "priceRub": round(offer_price / 100),
                    "originalPriceKopeks": legacy_pro_offer_service.ORIGINAL_PRICE_KOPEKS,
                    "originalPriceRub": round(legacy_pro_offer_service.ORIGINAL_PRICE_KOPEKS / 100),
                    "expiresAt": offer.expires_at.isoformat() if offer.expires_at else None,
                }
                payload["yearPriceKopeks"] = offer_price
                payload["yearPriceRub"] = round(offer_price / 100)

    return payload


async def build_plans(
    db: AsyncSession,
    cohort: str = "new",
    user: Optional[User] = None,
) -> List[Dict[str, Any]]:
    plans = await list_active_plans(db)
    return [await _serialize_plan(db, plan, cohort, user=user) for plan in plans]


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


def _friend_display_name(friend: Optional[User]) -> Optional[str]:
    """Имя реферала для фронта. Email не отдаём: у веб-юзеров full_name
    падает в email — это утечка чужого контакта рефереру."""
    if friend is None:
        return None
    parts = [p for p in (friend.first_name, friend.last_name) if p]
    if parts:
        return " ".join(parts)
    if friend.username:
        return friend.username
    return None


async def build_referral(db: AsyncSession, user: User) -> Dict[str, Any]:
    stats = await get_user_referral_stats(db, user.id)
    invited = stats.get("invited_count", 0) or 0
    active = stats.get("active_referrals", 0) or 0
    earned_kopeks = stats.get("total_earned_kopeks", 0) or 0
    earned_rub = round(earned_kopeks / 100, 2)
    reward_per_friend = round(earned_rub / invited, 2) if invited else 0

    from app.services.withdrawal_service import get_withdrawal_summary

    summary = await get_withdrawal_summary(db, user, earned_kopeks=earned_kopeks)

    return {
        "code": user.referral_code,
        "link": _referral_link(user),
        "invited": invited,
        "activeReferrals": active,
        # Фронт (Referrals.jsx) читает activeInvited — дубль для совместимости.
        "activeInvited": active,
        "earnedRub": earned_rub,
        "rewardPerFriendRub": reward_per_friend,
        "commissionPercent": get_effective_referral_commission_percent(user),
        "rays": user.rays_balance or 0,
        "raysLifetime": user.rays_lifetime_earned or 0,
        "balanceRub": summary["available_rub"],
        "pendingWithdrawalRub": summary["pending_rub"],
        "withdrawnRub": summary["withdrawn_rub"],
        "minWithdrawalRub": summary["min_rub"],
        "withdrawalEnabled": summary["enabled"],
    }


async def build_referral_payouts(
    db: AsyncSession, user: User, limit: int = 50, offset: int = 0
) -> List[Dict[str, Any]]:
    earnings = await get_referral_earnings_by_user(db, user.id, limit=limit, offset=offset)

    # Лучи за те же события: RayTransaction связан с транзакцией оплаты
    # реферала через source_transaction_id (уникален — идемпотентность).
    tx_ids = [e.referral_transaction_id for e in earnings if e.referral_transaction_id]
    rays_by_tx: Dict[int, int] = {}
    if tx_ids:
        rows = await db.execute(
            select(RayTransaction.source_transaction_id, RayTransaction.amount).where(
                RayTransaction.user_id == user.id,
                RayTransaction.amount > 0,
                RayTransaction.source_transaction_id.in_(tx_ids),
            )
        )
        for src_id, amount in rows.all():
            rays_by_tx[src_id] = rays_by_tx.get(src_id, 0) + amount

    payouts: List[Dict[str, Any]] = []
    for e in earnings:
        # referral и referral_transaction загружены eager (crud/referral.py);
        # referral.subscription НЕ загружен — трогать нельзя (MissingGreenlet).
        friend = e.referral
        tx = e.referral_transaction
        payouts.append(
            {
                "id": f"rp_{e.id}",
                "friendIndex": e.referral_id,
                "friendName": _friend_display_name(friend),
                "friendUsername": friend.username if friend else None,
                "planName": None,
                "amount": round(e.amount_kopeks / 100, 2),
                "friendPaidRub": round(tx.amount_kopeks / 100, 2) if tx else None,
                "raysEarned": rays_by_tx.get(e.referral_transaction_id, 0),
                "date": e.created_at.isoformat() if e.created_at else None,
            }
        )
    return payouts


async def build_referral_friends(
    db: AsyncSession, user: User, limit: int = 100, offset: int = 0
) -> List[Dict[str, Any]]:
    """Приглашённые друзья с итогами: сколько друг заплатил, сколько реферер
    получил с него рублей (комиссии) и лучей."""
    rows = await db.execute(
        select(User)
        .options(selectinload(User.subscription))
        .where(User.referred_by_id == user.id)
        .order_by(User.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    friends = rows.scalars().all()

    agg = await db.execute(
        select(
            ReferralEarning.referral_id,
            func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0),
            func.coalesce(func.sum(Transaction.amount_kopeks), 0),
        )
        .outerjoin(Transaction, ReferralEarning.referral_transaction_id == Transaction.id)
        .where(ReferralEarning.user_id == user.id)
        .group_by(ReferralEarning.referral_id)
    )
    earn_by_friend = {rid: (earned or 0, paid or 0) for rid, earned, paid in agg.all()}

    rays_rows = await db.execute(
        select(
            RayTransaction.referral_id,
            func.coalesce(func.sum(RayTransaction.amount), 0),
        )
        .where(
            RayTransaction.user_id == user.id,
            RayTransaction.amount > 0,
            RayTransaction.referral_id.isnot(None),
        )
        .group_by(RayTransaction.referral_id)
    )
    rays_by_friend = dict(rays_rows.all())

    items: List[Dict[str, Any]] = []
    for f in friends:
        earned_kopeks, paid_kopeks = earn_by_friend.get(f.id, (0, 0))
        sub = f.subscription
        items.append(
            {
                "id": f"rf_{f.id}",
                "name": _friend_display_name(f),
                "username": f.username or None,
                "createdAt": f.created_at.isoformat() if f.created_at else None,
                "hasSubscription": bool(sub and sub.actual_status == "active"),
                "hasPaid": bool(earned_kopeks) or bool(f.has_had_paid_subscription),
                "paidTotalRub": round(paid_kopeks / 100, 2),
                "earnedRub": round(earned_kopeks / 100, 2),
                "raysEarned": int(rays_by_friend.get(f.id, 0)),
            }
        )
    return items


# ── Магазин лучей ─────────────────────────────────────────────────────────

# Мап код приза (бэк, RAY_PRIZES) ↔ id приза (фронт, shopCatalog.js).
# Цены и состав совпадают 1:1; фронт по id подставляет картинки и оформление.
_PRIZE_ID_BY_CODE = {
    "plus_6m": "pz_sub_plus_6",
    "pro_1y": "pz_sub_pro_12",
    "pro_2y": "pz_sub_pro_24",
    "speaker": "pz_jbl",
    "airpods": "pz_airpods",
    "watch": "pz_watch",
    "iphone": "pz_iphone",
    "macbook": "pz_macbook",
}
_PRIZE_CODE_BY_ID = {v: k for k, v in _PRIZE_ID_BY_CODE.items()}

# TG-юзернейм для заявки на физический приз (форма кабинета).
SHOP_CONTACT_RE = re.compile(r"^@?[a-zA-Z0-9_]{4,64}$")

# Статусы заявки: бэк (RayPrizeClaimStatus) → фронт (ClaimStatus).
_CLAIM_STATUS_MAP = {
    "pending": "pending",
    "completed": "fulfilled",
    "cancelled": "rejected",
}


def get_prize_by_cabinet_id(prize_id: str):
    """RayPrize по фронтовому id (pz_*) либо по коду бэка (plus_6m)."""
    from app.services.rays_shop_service import RAY_PRIZES

    code = _PRIZE_CODE_BY_ID.get(prize_id, prize_id)
    for prize in RAY_PRIZES:
        if prize.code == code:
            return prize
    return None


def build_shop(user: User) -> Dict[str, Any]:
    from app.services.rays_shop_service import PRIZE_KIND_SUBSCRIPTION, RAY_PRIZES

    tiers = sorted([
        (settings.RAYS_TIER_1_MIN_DAYS, settings.RAYS_TIER_1_AMOUNT),
        (settings.RAYS_TIER_2_MIN_DAYS, settings.RAYS_TIER_2_AMOUNT),
        (settings.RAYS_TIER_3_MIN_DAYS, settings.RAYS_TIER_3_AMOUNT),
    ])

    return {
        "enabled": is_rays_shop_available_for(user),
        "rays": user.rays_balance or 0,
        "raysLifetime": user.rays_lifetime_earned or 0,
        "earnRules": [
            {"months": min_days // 30, "rays": amount}
            for min_days, amount in tiers
        ],
        "prizes": [
            {
                "id": _PRIZE_ID_BY_CODE.get(p.code, p.code),
                "code": p.code,
                "kind": "subscription" if p.kind == PRIZE_KIND_SUBSCRIPTION else "goods",
                "rays": p.cost,
                "name": p.catalog_title or p.title,
            }
            for p in RAY_PRIZES
        ],
    }


def build_prize_claim(claim) -> Dict[str, Any]:
    return {
        "id": f"cl_{claim.id}",
        "prizeId": _PRIZE_ID_BY_CODE.get(claim.prize_code, claim.prize_code),
        "rays": claim.cost_rays,
        "status": _CLAIM_STATUS_MAP.get(claim.status, claim.status),
        "contact": claim.contact,
        "createdAt": claim.created_at.isoformat() if claim.created_at else None,
    }


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


async def remove_device(db: AsyncSession, user: User, device_hwid: str) -> bool:
    if not user.remnawave_uuid:
        return False
    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            removed = await api.remove_device(user.remnawave_uuid, device_hwid)
    except Exception as error:
        logger.error(f"Ошибка удаления устройства {device_hwid}: {error}")
        return False

    # Только после успеха в панели: иначе пометим отозванным то, что осталось на месте.
    if removed:
        await _revoke_links(db, [device_hwid])
    return removed


async def reset_devices(db: AsyncSession, user: User) -> bool:
    if not user.remnawave_uuid:
        return False
    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            reset = await api.reset_user_devices(user.remnawave_uuid)
    except Exception as error:
        logger.error(f"Ошибка сброса устройств пользователя {user.id}: {error}")
        return False

    if reset and user.subscription is not None:
        try:
            count = await revoke_all_device_links(db, user.subscription.id)
            logger.info(f"Отозвано привязок при сбросе устройств: {count}")
        except Exception as error:
            logger.error(f"Ошибка отзыва привязок пользователя {user.id}: {error}")
    return reset


async def _revoke_links(db: AsyncSession, device_ids: List[str]) -> None:
    """Отзыв не должен ронять удаление: панель уже отработала."""
    try:
        count = await revoke_device_links(db, device_ids)
        if count:
            logger.info(f"Отозвано привязок устройств: {count}")
    except Exception as error:
        logger.error(f"Ошибка отзыва привязки устройства: {error}")


# ── Уведомления ──────────────────────────────────────────────────────────

def build_notification(notification) -> Dict[str, Any]:
    """Контракт фронта для уведомления кабинета (единый источник —
    cabinet_notification_service.serialize_notification)."""
    from app.services.cabinet_notification_service import serialize_notification

    return serialize_notification(notification)
