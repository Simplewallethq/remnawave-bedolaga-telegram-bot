"""Публичный API личного кабинета LetoVPNSite (вход без Telegram).

Авторизация: email/пароль или код подписки → JWT (Bearer).
Данные сериализуются через app.services.cabinet_service в контракт фронта.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
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
from app.database.models import (
    PaymentMethod,
    SubscriptionStatus,
    TransactionType,
    User,
    UserStatus,
)
from app.services import cabinet_service
from app.services.cabinet_auth_service import cabinet_auth_service
from app.services.payment_service import PaymentService
from app.services.plan_pricing_service import (
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
    TelegramLoginRequest,
    TopupRequest,
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

    if user.balance_kopeks < price_kopeks:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, detail="Insufficient balance")

    subscription = user.subscription
    if not subscription:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="No subscription to upgrade")

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
        if settings.is_heleket_enabled():
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
) -> Dict[str, Any]:
    ok = await cabinet_service.remove_device(user, device_id)
    if not ok:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Failed to remove device")
    return {"success": True}


@router.post("/devices/reset")
async def reset_devices(
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    ok = await cabinet_service.reset_devices(user)
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


# ── Уведомления (v1: пусто, см. план/риски) ──────────────────────────────

@router.get("/notifications")
async def get_notifications(
    user: User = Depends(get_current_cabinet_user),
) -> Dict[str, Any]:
    # В боте нет per-user ленты уведомлений под формат фронта.
    # v1: возвращаем пустой список (см. раздел "Риски" в плане).
    return {"notifications": []}
