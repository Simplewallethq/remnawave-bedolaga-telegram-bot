"""Частичная оплата тарифа: баланс + счёт на доплату.

Когда у пользователя 0 < баланс < цены тарифа, вместо «пополните баланс»
показывается разбивка (с баланса / к доплате) и выставляется счёт ровно на
недостающую сумму. Разбивка фиксируется в metadata счёта в момент выставления;
вебхук активирует подписку по зафиксированным числам без пересчёта цены и
повторной валидации оффера (риск ограничен временем жизни счёта ~1 час).

Баланс при этом не резервируется: деньги списываются только в момент успешной
доплаты (вебхук зачисляет счёт на баланс и тут же проводит покупку одной
транзакцией). Брошенный счёт не трогает баланс вообще.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from app.config import settings

logger = logging.getLogger(__name__)

SNAPSHOT_KIND = "tariff_purchase"
SNAPSHOT_VERSION = 1
SNAPSHOT_METADATA_KEY = "tariff_checkout"

_STARS_SNAPSHOT_KEY = "tariff_checkout_stars:{user_id}:{token}"
_STARS_SNAPSHOT_TTL = 86400


def get_provider_min_kopeks(method: str) -> int:
    """Минимальная сумма счёта провайдера по коду метода из callback topup_amount|{method}|…"""
    mins = {
        "yookassa": settings.YOOKASSA_MIN_AMOUNT_KOPEKS,
        "yookassa_sbp": settings.YOOKASSA_MIN_AMOUNT_KOPEKS,
        "mulenpay": settings.MULENPAY_MIN_AMOUNT_KOPEKS,
        "pal24": settings.PAL24_MIN_AMOUNT_KOPEKS,
        "platega": settings.PLATEGA_MIN_AMOUNT_KOPEKS,
        "platega_universal": settings.PLATEGA_MIN_AMOUNT_KOPEKS,
        "wata": settings.WATA_MIN_AMOUNT_KOPEKS,
        "cloudpayments": settings.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS,
        "cryptobot": settings.CRYPTOBOT_MIN_AMOUNT * 100,
        "heleket": 10000,  # захардкожено в app/handlers/balance/heleket.py
        "stars": 0,
    }
    return int(mins.get(method, 0))


def clamp_invoice_amount(method: str, shortfall_kopeks: int) -> int:
    """Сумма счёта на доплату: не меньше минимума провайдера; излишек ляжет на баланс."""
    return max(int(shortfall_kopeks), get_provider_min_kopeks(method))


def build_partial_breakdown(
    balance_kopeks: int,
    price_kopeks: int,
    base_price_kopeks: Optional[int] = None,
) -> Optional[Dict[str, int]]:
    """Разбивка «с баланса / к доплате». None, если частичная оплата неприменима
    (баланс 0 — обычный флоу полной оплаты; баланс >= цены — покупка с баланса)."""
    if balance_kopeks <= 0 or balance_kopeks >= price_kopeks:
        return None
    base = base_price_kopeks if base_price_kopeks is not None else price_kopeks
    return {
        "base_price_kopeks": int(base),
        "discount_kopeks": max(0, int(base) - int(price_kopeks)),
        "final_price_kopeks": int(price_kopeks),
        "balance_planned_kopeks": int(balance_kopeks),
        "shortfall_kopeks": int(price_kopeks) - int(balance_kopeks),
    }


async def build_invoice_checkout_snapshot(
    user_id: int,
    invoice_amount_kopeks: int,
) -> Optional[Dict[str, Any]]:
    """Снимок разбивки для metadata счёта — читается из intent-корзины тарифа.

    Возвращает snapshot только когда пользователь пришёл из экрана частичной
    оплаты тарифа (в корзине есть блок partial_payment). Обычные пополнения
    баланса snapshot не получают.
    """
    try:
        from app.services.user_cart_service import user_cart_service

        cart_data = await user_cart_service.get_user_cart(user_id)
    except Exception as error:
        logger.warning("Не удалось прочитать корзину для snapshot (%s): %s", user_id, error)
        return None

    if not cart_data:
        return None
    if cart_data.get("cart_mode") != "tariff":
        return None
    if (cart_data.get("tariff_op") or "purchase") != "purchase":
        return None
    if not cart_data.get("intent"):
        return None

    partial = cart_data.get("partial_payment")
    if not isinstance(partial, dict):
        return None

    final_price = int(partial.get("final_price_kopeks") or cart_data.get("total_price") or 0)
    if final_price <= 0:
        return None

    return {
        "v": SNAPSHOT_VERSION,
        "kind": SNAPSHOT_KIND,
        "user_id": int(user_id),
        "plan_id": cart_data.get("plan_id"),
        "plan_code": cart_data.get("plan_code"),
        "period_days": cart_data.get("period_days"),
        "base_price_kopeks": int(partial.get("base_price_kopeks") or final_price),
        "offer_type": cart_data.get("offer_type"),
        "offer_id": cart_data.get("offer_id"),
        "final_price_kopeks": final_price,
        "balance_planned_kopeks": int(partial.get("balance_planned_kopeks") or 0),
        "shortfall_kopeks": int(partial.get("shortfall_kopeks") or 0),
        "invoice_amount_kopeks": int(invoice_amount_kopeks),
        "issued_at": datetime.utcnow().isoformat(),
    }


def extract_checkout_snapshot(payment: Any) -> Optional[Dict[str, Any]]:
    """Достаёт snapshot разбивки из metadata_json платёжной записи провайдера.

    Терпимо к str-кодированному JSON (YooKassa хранит metadata и так, и так).
    """
    metadata = getattr(payment, "metadata_json", None)
    if metadata is None:
        return None
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return None
    if not isinstance(metadata, dict):
        return None

    snapshot = metadata.get(SNAPSHOT_METADATA_KEY)
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            return None
    if not isinstance(snapshot, dict):
        return None
    if snapshot.get("kind") != SNAPSHOT_KIND:
        return None
    if int(snapshot.get("final_price_kopeks") or 0) <= 0:
        return None
    return snapshot


async def stash_snapshot_for_stars(user_id: int, snapshot: Dict[str, Any]) -> Optional[str]:
    """У Stars нет своей записи платежа — snapshot кладём в Redis, токен уезжает
    в payload инвойса (лимит payload — 128 байт)."""
    try:
        from app.services.user_cart_service import user_cart_service

        token = uuid.uuid4().hex[:16]
        key = _STARS_SNAPSHOT_KEY.format(user_id=user_id, token=token)
        await user_cart_service.redis_client.setex(
            key, _STARS_SNAPSHOT_TTL, json.dumps(snapshot, ensure_ascii=False)
        )
        return token
    except Exception as error:
        logger.warning("Не удалось сохранить stars-snapshot для %s: %s", user_id, error)
        return None


async def pop_snapshot_for_stars(user_id: int, token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    try:
        from app.services.user_cart_service import user_cart_service

        key = _STARS_SNAPSHOT_KEY.format(user_id=user_id, token=token)
        raw = await user_cart_service.redis_client.get(key)
        if not raw:
            return None
        await user_cart_service.redis_client.delete(key)
        snapshot = json.loads(raw)
        if isinstance(snapshot, dict) and snapshot.get("kind") == SNAPSHOT_KIND:
            return snapshot
        return None
    except Exception as error:
        logger.warning("Не удалось прочитать stars-snapshot для %s: %s", user_id, error)
        return None
