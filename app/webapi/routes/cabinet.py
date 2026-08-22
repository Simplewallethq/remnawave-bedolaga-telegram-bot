"""Публичный API личного кабинета LetoVPNSite (вход без Telegram).

Авторизация: email/пароль или код подписки → JWT (Bearer).
Данные сериализуются через app.services.cabinet_service в контракт фронта.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.database import get_db
from app.database.crud.cabinet_notification import (
    get_missed_notifications,
    get_unread_count,
    list_cabinet_notifications,
    mark_all_read,
    mark_notification_read,
)
from app.database.crud.subscription import (
    create_trial_subscription,
    extend_subscription,
    update_subscription_autopay,
)
from app.database.crud.server_squad import get_active_server_squads
from app.database.crud.subscription_event import record_subscription_renewal_event
from app.database.crud.device_binding_code import get_or_create_binding_code
from app.database.crud.transaction import create_transaction
from app.database.crud.user import (
    create_web_user,
    get_user_by_email,
    get_user_by_id,
    get_user_by_referral_code,
)
from app.database.crud.withdrawal import get_user_withdrawals
from app.database.models import (
    PaymentMethod,
    SubscriptionStatus,
    TransactionType,
    User,
    UserStatus,
)
from app.services import cabinet_service, withdrawal_service
from app.services.cabinet_auth_service import cabinet_auth_service
from app.services.cabinet_notification_service import cabinet_notification_hub
from app.services.payment_service import PaymentService
from app.services.plan_pricing_service import (
    calculate_upgrade_delta,
    get_current_plan_price_for_period,
    get_plan_by_code,
    get_plan_price,
    resolve_pricing_cohort,
)
from app.services.cold_solo_offer_service import cold_solo_offer_service
from app.services.legacy_pro_offer_service import legacy_pro_offer_service
from app.services.subscription_service import SubscriptionService
from app.utils.passwords import hash_password
from app.utils.telegram_webapp import parse_webapp_init_data
from app.utils.validators import validate_email
from app.webapi.cabinet_dependencies import get_current_cabinet_user
from app.webapi.dependencies import get_db_session
from app.webapi.schemas.cabinet import (
    AutoRenewRequest,
    LoginCodeRequest,
    LoginRequest,
    OtpRequestRequest,
    OtpVerifyRequest,
    PurchaseRequest,
    RegisterOtpRequest,
    RegisterRequest,
    ShopRedeemRequest,
    TelegramLoginRequest,
    TopupRequest,
    WithdrawalCreateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _ensure_enabled() -> None:
    if not settings.CABINET_ENABLED:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cabinet is disabled")


async def _ensure_captcha(token: Optional[str], request: Request) -> None:
    """Проверяет hCaptcha-токен; no-op при HCAPTCHA_ENABLED=false."""
    from app.services import captcha_service

    if not settings.HCAPTCHA_ENABLED:
        return
    remote_ip = request.client.host if request.client else None
    if not await captcha_service.verify_captcha(token, remote_ip):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="captcha_failed")


async def _auth_response(db: AsyncSession, user: User) -> Dict[str, Any]:
    token = cabinet_auth_service.issue_token(user)
    # Перечитываем с eager-подпиской: build_user_profile читает user.subscription,
    # иначе ленивая загрузка вне async-сессии → MissingGreenlet.
    full = await get_user_by_id(db, user.id) or user
    return {"token": token, "user": cabinet_service.build_user_profile(full)}


# ── Auth ─────────────────────────────────────────────────────────────────

@router.post("/auth/register-otp")
async def register_otp(
    payload: RegisterOtpRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """Шлёт email-код подтверждения для регистрации (шаг 1 из 2)."""
    from app.database.crud import cabinet_otp as otp_crud
    from app.services import email_service

    _ensure_enabled()
    await _ensure_captcha(payload.captcha_token, request)
    if not settings.CABINET_EMAIL_VERIFICATION:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Email verification is disabled")
    if not email_service.is_email_configured():
        logger.error("SendGrid не сконфигурирован — register-otp недоступен")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Email delivery unavailable")

    email = payload.email.strip().lower()
    if not validate_email(email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid email")

    existing = await get_user_by_email(db, email)
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email already registered")

    active = await otp_crud.get_active_otp(db, email)
    if active:
        age = (datetime.utcnow() - active.created_at).total_seconds()
        retry_after = int(settings.CABINET_OTP_RESEND_SECONDS - age)
        if retry_after > 0:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Code already sent",
                headers={"Retry-After": str(retry_after)},
            )

    code = otp_crud.generate_otp_code()
    await otp_crud.create_otp(db, email, code)
    try:
        await email_service.send_otp_email(email, code)
    except email_service.EmailDeliveryError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Failed to send email")

    return {
        "ok": True,
        "ttlSeconds": settings.CABINET_OTP_TTL_MINUTES * 60,
        "resendAfter": settings.CABINET_OTP_RESEND_SECONDS,
    }


async def _verify_registration_code(db: AsyncSession, email: str, code: Optional[str]) -> None:
    """Проверяет email-код регистрации; 400 invalid_code / 410 code_expired."""
    from app.database.crud import cabinet_otp as otp_crud

    if not code or not code.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="code_required")

    record = await otp_crud.get_active_otp(db, email)
    if not record:
        raise HTTPException(status.HTTP_410_GONE, detail="code_expired")

    if not otp_crud.verify_otp_code(code.strip(), record.hashed_code):
        attempts = await otp_crud.register_failed_attempt(db, record)
        if attempts >= settings.CABINET_OTP_MAX_ATTEMPTS:
            raise HTTPException(status.HTTP_410_GONE, detail="code_expired")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_code")

    await otp_crud.delete_otp(db, record)


@router.post("/auth/register")
async def register(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    _ensure_enabled()

    email = payload.email.strip().lower()
    if not validate_email(email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid email")

    existing = await get_user_by_email(db, email)
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email already registered")

    if settings.CABINET_EMAIL_VERIFICATION:
        await _verify_registration_code(db, email, payload.code)

    referred_by_id = None
    if payload.ref:
        referrer = await get_user_by_referral_code(db, payload.ref.strip())
        if referrer:
            referred_by_id = referrer.id

    user = await create_web_user(
        db,
        email=email,
        password_hash=hash_password(payload.password),
        referred_by_id=referred_by_id,
    )

    if settings.CABINET_EMAIL_VERIFICATION:
        user.email_verified = True
        await db.commit()

    # Триальная подписка + аккаунт в remnawave
    try:
        subscription = await create_trial_subscription(db, user.id)
    except Exception:
        logger.exception("Не удалось создать триальную подписку для %s", email)
        subscription = None

    if subscription is not None:
        await _provision_remnawave(db, subscription)

    return await _auth_response(db, user)


async def _provision_remnawave(db: AsyncSession, subscription) -> bool:
    """Создаёт/обновляет аккаунт в панели RemnaWave для подписки.

    is_configured — это @property (без скобок!). Ошибку логируем с traceback,
    но регистрацию не валим: до-создание возможно позже (lazy).
    """
    service = SubscriptionService()
    if not service.is_configured:
        logger.warning("RemnaWave не сконфигурирован: %s", service.configuration_error)
        return False
    try:
        result = await service.create_remnawave_user(db, subscription)
        if result is None:
            logger.error("create_remnawave_user вернул None для подписки %s", subscription.id)
            return False
        return True
    except Exception:
        logger.exception("Ошибка создания RemnaWave аккаунта для подписки %s", subscription.id)
        return False


async def ensure_remnawave_account(db: AsyncSession, user: User) -> None:
    """Ленивое до-создание аккаунта RemnaWave для веб-юзеров, у которых он отсутствует."""
    if user.remnawave_uuid:
        return
    subscription = getattr(user, "subscription", None)
    if subscription is None:
        return
    created = await _provision_remnawave(db, subscription)
    if created:
        await db.refresh(user)


@router.post("/auth/login")
async def login(
    payload: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    _ensure_enabled()
    await _ensure_captcha(payload.captcha_token, request)
    user = await cabinet_auth_service.authenticate_email(
        db, payload.email.strip().lower(), payload.password
    )
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if user.status == UserStatus.BLOCKED.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="User is blocked")
    return await _auth_response(db, user)


@router.post("/auth/login-code")
async def login_code(
    payload: LoginCodeRequest,
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    _ensure_enabled()
    user = await cabinet_auth_service.authenticate_code(db, payload.code)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid code")
    if user.status == UserStatus.BLOCKED.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="User is blocked")
    return await _auth_response(db, user)


@router.post("/auth/telegram")
async def login_telegram(
    payload: TelegramLoginRequest,
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    _ensure_enabled()
    try:
        webapp_data = parse_webapp_init_data(payload.init_data, settings.BOT_TOKEN)
    except Exception as error:
        logger.warning(f"Невалидный Telegram initData: {error}")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid init data")

    tg_user = (webapp_data or {}).get("user") or {}
    telegram_id = tg_user.get("id")
    if not telegram_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid init data")

    from app.database.crud.user import get_user_by_telegram_id

    user = await get_user_by_telegram_id(db, int(telegram_id))
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")
    return await _auth_response(db, user)


# ── Passwordless login-or-register (email + код) ──────────────────────────

@router.post("/auth/otp/request")
async def auth_otp_request(
    payload: OtpRequestRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """Шлёт email-код для passwordless-входа (login-or-register, шаг 1 из 2).

    В отличие от deprecated /auth/register-otp — без 409 на существующем email
    и без гейта CABINET_EMAIL_VERIFICATION: OTP здесь обязателен всегда.
    """
    from app.database.crud import cabinet_otp as otp_crud
    from app.services import email_service

    _ensure_enabled()
    if not email_service.is_email_configured():
        logger.error("SendGrid не сконфигурирован — /cabinet/auth/otp/request недоступен")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Email delivery unavailable")

    email = payload.email.strip().lower()
    if not validate_email(email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid email")

    active = await otp_crud.get_active_otp(db, email)
    if active:
        age = (datetime.utcnow() - active.created_at).total_seconds()
        retry_after = int(settings.CABINET_OTP_RESEND_SECONDS - age)
        if retry_after > 0:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Code already sent",
                headers={"Retry-After": str(retry_after)},
            )
    else:
        # Капча только на первый запрос кода: повторная отправка (active OTP уже есть,
        # прошёл лишь cooldown) защищена rate-limit'ом выше и капчи не требует.
        await _ensure_captcha(payload.captcha_token, request)

    code = otp_crud.generate_otp_code()
    await otp_crud.create_otp(db, email, code)
    try:
        await email_service.send_otp_email(email, code)
    except email_service.EmailDeliveryError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Failed to send email")

    return {
        "ok": True,
        "ttlSeconds": settings.CABINET_OTP_TTL_MINUTES * 60,
        "resendAfter": settings.CABINET_OTP_RESEND_SECONDS,
    }


@router.post("/auth/otp/verify")
async def auth_otp_verify(
    payload: OtpVerifyRequest,
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """Проверяет код и логинит-или-регистрирует пользователя, возвращает JWT (шаг 2)."""
    _ensure_enabled()

    email = payload.email.strip().lower()
    if not validate_email(email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid email")

    # 400 invalid_code / 410 code_expired — единый контракт с /auth/register.
    await _verify_registration_code(db, email, payload.code)

    user = await get_user_by_email(db, email)
    if user is None:
        referred_by_id = None
        if payload.ref:
            referrer = await get_user_by_referral_code(db, payload.ref.strip())
            if referrer:
                referred_by_id = referrer.id

        # Passwordless: задаём заведомо непригодный для входа пароль.
        user = await create_web_user(
            db,
            email=email,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            referred_by_id=referred_by_id,
        )
        user.email_verified = True
        await db.commit()

        try:
            subscription = await create_trial_subscription(db, user.id)
        except Exception:
            logger.exception("Не удалось создать триальную подписку для %s", email)
            subscription = None
        if subscription is not None:
            await _provision_remnawave(db, subscription)
    else:
        # Существующий пользователь — ленивый до-провижн RemnaWave при отсутствии.
        await ensure_remnawave_account(db, user)

    if user.status == UserStatus.BLOCKED.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="User is blocked")

    return await _auth_response(db, user)


# ── Профиль / подписка / тарифы ──────────────────────────────────────────

@router.get("/me")
async def get_me(
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    return cabinet_service.build_user_profile(user)


@router.get("/subscription")
async def get_subscription(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    # Ленивое до-создание панельного аккаунта (для юзеров, у кого он не создался ранее)
    await ensure_remnawave_account(db, user)
    return {"subscription": cabinet_service.build_subscription(user)}


@router.get("/plans")
async def get_plans(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    return {"plans": await cabinet_service.build_plans(db, cohort=resolve_pricing_cohort(user), user=user)}


@router.get("/plans/public")
async def get_public_plans(
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """Public read-only tariff catalogue for the marketing site."""
    return {"plans": await cabinet_service.build_plans(db, cohort="new")}


@router.post("/subscription/autorenew")
async def set_autorenew(
    payload: AutoRenewRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    if not user.subscription:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No subscription")
    await update_subscription_autopay(db, user.subscription, payload.enabled)
    return {"subscription": cabinet_service.build_subscription(user)}


@router.post("/subscription/purchase")
async def purchase(
    payload: PurchaseRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    """Покупка/смена тарифа с оплатой из баланса.

    ВНИМАНИЕ: упрощённая логика v1 (оплата только с баланса, период = months*30).
    Требует QA против тарифной системы бота перед продом.
    """
    plan = await get_plan_by_code(db, payload.plan_id)
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Plan not found")

    period_days = payload.months * 30
    price_kopeks = await get_plan_price(
        db, plan.id, period_days, cohort=resolve_pricing_cohort(user)
    )
    if price_kopeks is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Price for this plan/period is not configured",
        )

    fixed_offer = None
    offer_price, fixed_offer = await cold_solo_offer_service.get_price_override(
        db,
        user.id,
        plan_code=plan.code,
        period_days=period_days,
    )
    is_legacy_pro_fixed_offer = False
    if offer_price is not None:
        price_kopeks = offer_price
    else:
        offer_price, fixed_offer = await legacy_pro_offer_service.get_price_override(
            db,
            user.id,
            plan_code=plan.code,
            period_days=period_days,
        )
        if offer_price is not None:
            price_kopeks = offer_price
            is_legacy_pro_fixed_offer = True

    subscription = user.subscription
    if not subscription:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="No subscription to upgrade")

    if user.balance_kopeks < price_kopeks:
        method = (payload.method or "").strip().lower()
        if method != "crypto" or not settings.is_heleket_cabinet_enabled():
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, detail="Insufficient balance")

        return await _create_cabinet_tariff_payment(
            db=db,
            user=user,
            plan=plan,
            period_days=period_days,
            price_kopeks=price_kopeks,
            base_price_kopeks=await get_plan_price(
                db, plan.id, period_days, cohort=resolve_pricing_cohort(user)
            ),
            fixed_offer=fixed_offer,
        )

    old_end_date = subscription.end_date

    # Списываем с баланса
    user.subtract_balance(price_kopeks)
    transaction = await create_transaction(
        db=db,
        user_id=user.id,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=price_kopeks,
        description=f"Тариф {plan.display_name} на {payload.months} мес.",
        payment_method=PaymentMethod.MANUAL,
    )

    # Применяем тариф
    subscription.plan_id = plan.id
    subscription.plan_period_days = period_days
    subscription.device_limit = plan.device_limit
    subscription.traffic_limit_gb = plan.traffic_limit_gb
    subscription.is_trial = False
    if is_legacy_pro_fixed_offer:
        squads = await get_active_server_squads(db)
        subscription.connected_squads = [
            squad.squad_uuid
            for squad in squads
            if getattr(squad, "squad_uuid", None)
        ]
        now = datetime.utcnow()
        subscription.status = SubscriptionStatus.ACTIVE.value
        subscription.start_date = now
        subscription.end_date = now + timedelta(days=period_days)
        await db.flush()
    else:
        await extend_subscription(db, subscription, period_days)
    await record_subscription_renewal_event(
        db,
        user_id=user.id,
        subscription_id=subscription.id,
        transaction_id=transaction.id,
        amount_kopeks=price_kopeks,
        occurred_at=transaction.completed_at or transaction.created_at,
        period_days=period_days,
        previous_end_date=old_end_date,
        new_end_date=subscription.end_date,
        payment_method=transaction.payment_method,
        balance_after=user.balance_kopeks,
        source="cabinet",
    )

    user.has_had_paid_subscription = True
    await db.commit()
    if getattr(fixed_offer, "notification_type", None) in legacy_pro_offer_service.NOTIFICATION_TYPES:
        await legacy_pro_offer_service.mark_claimed_after_purchase(db, fixed_offer)
    else:
        await cold_solo_offer_service.mark_claimed_after_purchase(db, fixed_offer)

    # Синхронизация с панелью (is_configured — @property, без скобок!)
    try:
        service = SubscriptionService()
        if service.is_configured:
            await service.update_remnawave_user(db, subscription)
    except Exception:
        logger.exception("Ошибка синхронизации подписки %s", subscription.id)

    await db.refresh(user)
    return {"subscription": cabinet_service.build_subscription(user)}


# ── Баланс / транзакции / пополнение ─────────────────────────────────────

@router.get("/transactions")
async def get_transactions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    return await cabinet_service.build_transactions(db, user, limit=limit, offset=offset)


_CRYPTOBOT_FALLBACK_RATE = 95.0


async def _usd_to_rub_rate() -> float:
    from app.utils.currency_converter import currency_converter

    try:
        rate = await currency_converter.get_usd_to_rub_rate()
    except Exception:
        rate = 0.0
    if not rate or rate <= 0:
        rate = _CRYPTOBOT_FALLBACK_RATE
    return float(rate)


async def _create_cabinet_tariff_payment(
    *,
    db: AsyncSession,
    user: User,
    plan: Any,
    period_days: int,
    price_kopeks: int,
    base_price_kopeks: Optional[int],
    fixed_offer: Any = None,
) -> Dict[str, Any]:
    """Create a cabinet-only Heleket checkout that completes the tariff intent.

    The shared top-up webhook already knows how to consume tariff intent carts.
    Keeping the intent server-side makes the redirect safe: the browser never
    decides which plan to activate or how much balance to deduct.
    """
    from app.services.subscription_auto_purchase_service import (
        auto_purchase_saved_cart_after_topup,
    )
    from app.services.tariff_partial_payment_service import (
        build_partial_breakdown,
        clamp_invoice_amount,
    )
    from app.services.user_cart_service import user_cart_service

    subscription = user.subscription
    now = datetime.utcnow()
    is_active_paid = bool(
        subscription
        and not subscription.is_trial
        and subscription.plan_id is not None
        and subscription.end_date > now
        and subscription.status == SubscriptionStatus.ACTIVE.value
    )

    checkout_period_days = period_days
    checkout_price_kopeks = int(price_kopeks)
    tariff_op = "purchase"

    if is_active_paid and subscription.plan_id == plan.id:
        tariff_op = "renew"
    elif is_active_paid:
        tariff_op = "upgrade"
        checkout_period_days = subscription.plan_period_days or period_days
        new_price = await get_plan_price(
            db,
            plan.id,
            checkout_period_days,
            cohort=resolve_pricing_cohort(user),
        )
        current_price = await get_current_plan_price_for_period(db, subscription, user)
        if new_price is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Price for target plan is not configured",
            )
        checkout_price_kopeks = calculate_upgrade_delta(
            subscription,
            plan,
            new_price,
            current_price,
        )

    offer_id = getattr(fixed_offer, "id", None)
    offer_type = getattr(fixed_offer, "notification_type", None)
    cart_data: Dict[str, Any] = {
        "cart_mode": "tariff",
        "tariff_op": tariff_op,
        "plan_id": plan.id,
        "plan_code": plan.code,
        "period_days": checkout_period_days,
        "total_price": checkout_price_kopeks,
        "intent": True,
        "source": "cabinet",
    }
    if offer_id is not None:
        cart_data.update(
            {
                "offer_id": offer_id,
                "offer_type": offer_type,
                "price_override_kopeks": int(price_kopeks),
            }
        )

    if tariff_op == "purchase":
        partial = build_partial_breakdown(
            user.balance_kopeks,
            checkout_price_kopeks,
            base_price_kopeks,
        )
        if partial:
            cart_data["partial_payment"] = partial

    ttl = max(3600, settings.get_heleket_lifetime() + 300)
    if not await user_cart_service.save_user_cart(user.id, cart_data, ttl=ttl):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to prepare subscription checkout",
        )

    # A prorated downgrade/switch can already be covered by the existing
    # balance even though the full target-plan price is higher. Complete it
    # immediately instead of creating a zero-value invoice.
    shortfall_kopeks = max(0, checkout_price_kopeks - user.balance_kopeks)
    if shortfall_kopeks == 0:
        completed = await auto_purchase_saved_cart_after_topup(db, user)
        if not completed:
            await user_cart_service.delete_user_cart(user.id)
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="Unable to apply subscription change",
            )
        refreshed = await get_user_by_id(db, user.id) or user
        return {"subscription": cabinet_service.build_subscription(refreshed)}

    invoice_amount_kopeks = clamp_invoice_amount("heleket", shortfall_kopeks)
    try:
        return await _create_topup_payment(
            db,
            user,
            "crypto",
            invoice_amount_kopeks,
        )
    except Exception:
        await user_cart_service.delete_user_cart(user.id)
        raise


def _platega_method_code(kind: str) -> Optional[int]:
    """Код метода Platega под наш kind: sbp → 2 (СБП QR), card → 11/10/12."""
    active = settings.get_platega_active_methods()
    preferred = {"sbp": [2], "card": [11, 10, 12]}.get(kind, [])
    for code in preferred:
        if code in active:
            return code
    return None


async def _create_platega_topup(
    ps: PaymentService, db: AsyncSession, user: User, kind: str,
    amount_kopeks: int, description: str,
) -> Optional[str]:
    method_code = _platega_method_code(kind)
    if method_code is None:
        return None
    result = await ps.create_platega_payment(
        db=db, user_id=user.id, amount_kopeks=amount_kopeks,
        description=description,
        language=user.language or settings.DEFAULT_LANGUAGE,
        payment_method_code=method_code,
    )
    return (result or {}).get("redirect_url")


async def _create_topup_payment(
    db: AsyncSession, user: User, kind: str, amount_kopeks: int
) -> Dict[str, Any]:
    """Маппинг 3 методов фронта (card/sbp/crypto) на доступных провайдеров."""
    ps = PaymentService()
    description = settings.get_balance_payment_description(amount_kopeks)

    # Роутер обслуживает и card, и sbp: все три шлюза универсальны и сами
    # показывают карты и СБП на своей странице. crypto не трогаем.
    if kind in ("card", "sbp"):
        from app.services.payment_gateway_router import (
            SOURCE_CABINET,
            payment_gateway_router,
        )

        if payment_gateway_router.is_enabled(SOURCE_CABINET):
            routed = await payment_gateway_router.create_invoice(
                db,
                payment_service=ps,
                user=user,
                amount_kopeks=amount_kopeks,
                source=SOURCE_CABINET,
                description=description,
                language=user.language or settings.DEFAULT_LANGUAGE,
            )
            if routed:
                return {
                    "paymentUrl": routed.payment_url,
                    "amountKopeks": amount_kopeks,
                    "gateway": routed.gateway,
                }
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, detail="Failed to create payment"
            )

    if kind == "card":
        if settings.is_yookassa_enabled():
            result = await ps.create_yookassa_payment(
                db=db, user_id=user.id, amount_kopeks=amount_kopeks, description=description
            )
            url = result.get("confirmation_url") if result else None
        elif settings.is_cloudpayments_enabled():
            result = await ps.create_cloudpayments_payment(
                db=db, user_id=user.id, amount_kopeks=amount_kopeks,
                description=description, telegram_id=user.telegram_id,
                language=user.language or settings.DEFAULT_LANGUAGE,
            )
            url = result.get("payment_url") if result else None
        elif settings.is_wata_enabled():
            result = await ps.create_wata_payment(
                db=db, user_id=user.id, amount_kopeks=amount_kopeks,
                description=description, language=user.language,
            )
            url = result.get("payment_url") if result else None
        elif settings.is_platega_enabled() and _platega_method_code("card") is not None:
            url = await _create_platega_topup(ps, db, user, "card", amount_kopeks, description)
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Card payments unavailable")

    elif kind == "sbp":
        if settings.is_yookassa_enabled() and getattr(settings, "YOOKASSA_SBP_ENABLED", False):
            result = await ps.create_yookassa_sbp_payment(
                db=db, user_id=user.id, amount_kopeks=amount_kopeks, description=description
            )
            url = result.get("confirmation_url") if result else None
        elif settings.is_pal24_enabled():
            result = await ps.create_pal24_payment(
                db=db, user_id=user.id, amount_kopeks=amount_kopeks,
                description=description, language=user.language or settings.DEFAULT_LANGUAGE,
                payment_method="sbp",
            )
            url = (result or {}).get("sbp_url") or (result or {}).get("link_url") or (result or {}).get("payment_url")
        elif settings.is_platega_enabled() and _platega_method_code("sbp") is not None:
            url = await _create_platega_topup(ps, db, user, "sbp", amount_kopeks, description)
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="SBP payments unavailable")

    elif kind == "crypto":
        if settings.is_heleket_cabinet_enabled():
            result = await ps.create_heleket_payment(
                db=db, user_id=user.id, amount_kopeks=amount_kopeks,
                description=description, language=user.language or settings.DEFAULT_LANGUAGE,
            )
            url = result.get("payment_url") if result else None
        elif settings.is_cryptobot_enabled():
            rate = await _usd_to_rub_rate()
            amount_usd = round(amount_kopeks / 100 / rate, 2)
            if amount_usd < 1.0:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"Amount is below minimum ({rate:.0f} RUB)",
                )
            result = await ps.create_cryptobot_payment(
                db=db, user_id=user.id, amount_usd=amount_usd,
                asset=settings.CRYPTOBOT_DEFAULT_ASSET,
                description=description,
                payload=f"balance_{user.id}_{amount_kopeks}",
            )
            url = (
                (result or {}).get("bot_invoice_url")
                or (result or {}).get("mini_app_invoice_url")
                or (result or {}).get("web_app_invoice_url")
            )
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Crypto payments unavailable")
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Unknown payment method")

    if not url:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Failed to create payment")
    return {"paymentUrl": url, "amountKopeks": amount_kopeks}


@router.post("/topup")
async def topup(
    payload: TopupRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    amount_kopeks = int(round(payload.amount * 100))
    if amount_kopeks <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Amount must be positive")
    kind = (payload.method or "").strip().lower()
    return await _create_topup_payment(db, user, kind, amount_kopeks)


# ── Устройства ───────────────────────────────────────────────────────────

@router.post("/device-binding-code")
async def device_binding_code(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    """Код для привязки устройства (вводится в приложении), как в боте.

    Тот же механизм: get_or_create_binding_code — переиспользуем активный код,
    создаём новый только если активного нет.
    """
    if not user.subscription:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No subscription")
    try:
        record = await get_or_create_binding_code(db, user.subscription.id)
    except Exception:
        logger.exception("Не удалось создать код привязки для пользователя %s", user.id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Failed to generate code")

    ttl_hours = max(1, int((record.expires_at - datetime.utcnow()).total_seconds() // 3600))
    return {
        "code": record.code,
        "expiresAt": record.expires_at.isoformat(),
        "ttlHours": ttl_hours,
    }


@router.get("/devices")
async def get_devices(
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    return {"devices": await cabinet_service.build_devices(user)}


@router.delete("/devices/{device_id}")
async def delete_device(
    device_id: str,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    ok = await cabinet_service.remove_device(db, user, device_id)
    if not ok:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Failed to remove device")
    return {"success": True}


@router.post("/devices/reset")
async def reset_devices(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    ok = await cabinet_service.reset_devices(db, user)
    return {"success": ok}


# ── Рефералы ─────────────────────────────────────────────────────────────

@router.get("/referral")
async def get_referral(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    return await cabinet_service.build_referral(db, user)


@router.get("/referral/payouts")
async def get_referral_payouts(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    return {"payouts": await cabinet_service.build_referral_payouts(db, user, limit=limit, offset=offset)}


@router.get("/referral/friends")
async def get_referral_friends(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    return {"friends": await cabinet_service.build_referral_friends(db, user, limit=limit, offset=offset)}


# ── Вывод реферальных рублей ─────────────────────────────────────────────

@router.get("/referral/withdrawals")
async def get_referral_withdrawals(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    withdrawals = await get_user_withdrawals(db, user.id, limit=limit)
    return {"withdrawals": [withdrawal_service.build_withdrawal(w) for w in withdrawals]}


@router.post("/referral/withdrawals")
async def create_referral_withdrawal(
    payload: WithdrawalCreateRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    amount_kopeks = int(round(payload.amount * 100))
    try:
        withdrawal = await withdrawal_service.create_withdrawal(
            db, user, amount_kopeks=amount_kopeks, details=payload.details,
        )
    except withdrawal_service.WithdrawalError as exc:
        raise HTTPException(exc.http_status, detail=exc.code)

    return {
        "withdrawal": withdrawal_service.build_withdrawal(withdrawal),
        "referral": await cabinet_service.build_referral(db, user),
    }


# ── Магазин лучей ─────────────────────────────────────────────────────────

def _ensure_shop_enabled() -> None:
    if not settings.is_rays_shop_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="shop_disabled")


@router.get("/shop")
async def get_shop(
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    _ensure_shop_enabled()
    return cabinet_service.build_shop(user)


@router.get("/shop/claims")
async def get_shop_claims(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    _ensure_shop_enabled()
    from app.database.crud.ray_prize_claim import get_user_claims

    claims = await get_user_claims(db, user.id, limit=limit)
    return {"claims": [cabinet_service.build_prize_claim(c) for c in claims]}


@router.post("/shop/redeem")
async def redeem_shop_prize(
    payload: ShopRedeemRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    _ensure_shop_enabled()
    from sqlalchemy import select

    from app.services.rays_shop_service import (
        PRIZE_KIND_SUBSCRIPTION,
        create_physical_prize_claim,
        redeem_subscription_prize,
    )

    prize = cabinet_service.get_prize_by_cabinet_id(payload.prize_id)
    if prize is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="prize_not_found")

    # Блокируем строку пользователя: параллельные redeem не должны оба пройти
    # проверку баланса лучей (subtract_user_rays проверяет ещё раз, но с
    # блокировкой отказ детерминированный и до побочных действий).
    locked_user = (
        await db.execute(select(User).where(User.id == user.id).with_for_update())
    ).scalar_one()

    if (locked_user.rays_balance or 0) < prize.cost:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, detail="insufficient_rays")

    if prize.kind == PRIZE_KIND_SUBSCRIPTION:
        subscription = await redeem_subscription_prize(db, locked_user, prize)
        if subscription is None:
            # Лучей хватало (проверено под блокировкой) — значит, недоступен план.
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="prize_unavailable")
        # Цифровые призы не создают строк заявок (как в боте) — отдаём
        # синтетическую выполненную заявку для тоста на фронте.
        now = datetime.utcnow()
        claim_payload = {
            "id": f"cl_sub_{int(now.timestamp())}",
            "prizeId": payload.prize_id,
            "rays": prize.cost,
            "status": "fulfilled",
            "createdAt": now.isoformat(),
        }
        return {"claim": claim_payload, "rays": locked_user.rays_balance or 0}

    # Физический приз — нужна заявка с контактом.
    contact = (payload.contact or "").strip()
    if not contact:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="contact_required")
    if not cabinet_service.SHOP_CONTACT_RE.match(contact):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="contact_invalid")

    bot = None
    try:
        if settings.BOT_TOKEN:
            from aiogram import Bot

            bot = Bot(token=settings.BOT_TOKEN)
        claim = await create_physical_prize_claim(
            db, locked_user, prize, bot=bot, contact=contact,
        )
    finally:
        if bot is not None:
            try:
                await bot.session.close()
            except Exception:  # noqa: BLE001
                pass

    if claim is None:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, detail="insufficient_rays")

    return {
        "claim": cabinet_service.build_prize_claim(claim),
        "rays": locked_user.rays_balance or 0,
    }


# ── Уведомления (лента + SSE) ─────────────────────────────────────────────

@router.get("/notifications")
async def get_notifications(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False, alias="unreadOnly"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    items, total = await list_cabinet_notifications(
        db, user.id, limit=limit, offset=offset, unread_only=unread_only
    )
    return {
        "items": [cabinet_service.build_notification(n) for n in items],
        "total": total,
        "unreadCount": await get_unread_count(db, user.id),
    }


@router.get("/notifications/unread-count")
async def get_notifications_unread_count(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    return {"unreadCount": await get_unread_count(db, user.id)}


@router.post("/notifications/read-all")
async def mark_notifications_read_all(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    marked = await mark_all_read(db, user.id)
    return {"success": True, "marked": marked}


@router.post("/notifications/{notification_id}/read")
async def mark_notification_as_read(
    notification_id: int,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    marked = await mark_notification_read(db, user.id, notification_id)
    if not marked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Notification not found")
    return {"success": True, "unreadCount": await get_unread_count(db, user.id)}


@router.post("/notifications/stream-token")
async def issue_notifications_stream_token(
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    return {
        "token": cabinet_auth_service.issue_sse_ticket(user),
        "expiresIn": settings.CABINET_NOTIFICATIONS_SSE_TICKET_TTL_SECONDS,
    }


@router.get("/notifications/stream")
async def stream_notifications(
    request: Request,
    ticket: str = Query(...),
    since: Optional[int] = Query(None, ge=0),
) -> StreamingResponse:
    """SSE-стрим уведомлений. Auth — короткоживущий тикет в query
    (EventSource не умеет Authorization-заголовок). Backfill — по
    ?since=<id> или заголовку Last-Event-ID."""
    _ensure_enabled()
    if not settings.CABINET_NOTIFICATIONS_ENABLED:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Notifications are disabled")

    user_id = cabinet_auth_service.decode_sse_ticket(ticket)
    if not user_id:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired stream ticket"
        )

    since_id = since
    if since_id is None:
        last_event_id = request.headers.get("Last-Event-ID")
        if last_event_id:
            try:
                since_id = int(last_event_id.removeprefix("ntf_"))
            except ValueError:
                since_id = None

    def _format_event(event: Dict[str, Any]) -> str:
        numeric_id = str(event.get("id", "")).removeprefix("ntf_")
        return (
            f"id: {numeric_id}\n"
            "event: notification\n"
            f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        )

    async def event_stream():
        # Не держим сессию на время стрима: короткая — только для backfill.
        if since_id is not None:
            try:
                async for session in get_db():
                    missed = await get_missed_notifications(
                        session, user_id, since_id=since_id, limit=50
                    )
                    for notification in missed:
                        yield _format_event(
                            cabinet_service.build_notification(notification)
                        )
            except Exception:
                logger.error(
                    "Ошибка backfill уведомлений кабинета для пользователя %s",
                    user_id,
                    exc_info=True,
                )

        queue = cabinet_notification_hub.subscribe(user_id)
        try:
            heartbeat = max(5, settings.CABINET_NOTIFICATIONS_HEARTBEAT_SECONDS)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event.get("event") == "close":
                    break
                yield _format_event(event)
        finally:
            cabinet_notification_hub.unsubscribe(user_id, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
