"""Внутренний passwordless OTP-API для приложения teleVpn.

Монтируется под /api/auth и защищён X-API-Key (require_api_token), как /api/devices.
В отличие от /cabinet/auth/register — без пароля и без 409 на существующем
пользователе: это единый login-or-register. Бот владеет пользователем и
подпиской (триал + аккаунт RemnaWave); teleVpn выдаёт собственный JWT поверх.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Security, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.device_link import create_device_link, get_device_link
from app.database.crud.subscription import (
    create_trial_denied_subscription,
    create_trial_subscription,
)
from app.database.crud.user import (
    create_web_user,
    get_user_by_email,
    get_user_by_id,
)
from app.utils.passwords import hash_password
from app.utils.validators import validate_email

from app.services.cabinet_auth_service import cabinet_auth_service

from ..dependencies import get_db_session, require_api_token
from ..schemas.auth_otp import (
    AppCabinetTokenRequest,
    AppCabinetTokenResponse,
    AppOtpRequest,
    AppOtpRequestResponse,
    AppOtpVerifyRequest,
    AppOtpVerifyResponse,
)
from .cabinet import (
    _provision_remnawave,
    _verify_registration_code,
    ensure_remnawave_account,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/otp/request", response_model=AppOtpRequestResponse)
async def request_otp(
    payload: AppOtpRequest,
    _=Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AppOtpRequestResponse:
    """Шаг 1: сгенерировать и отправить email-код (login-or-register)."""
    from app.database.crud import cabinet_otp as otp_crud
    from app.services import email_service

    if not email_service.is_email_configured():
        logger.error("SendGrid не сконфигурирован — /api/auth/otp/request недоступен")
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

    code = otp_crud.generate_otp_code()
    await otp_crud.create_otp(db, email, code)
    try:
        await email_service.send_otp_email(email, code)
    except email_service.EmailDeliveryError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Failed to send email")

    return AppOtpRequestResponse(
        ok=True,
        ttl_seconds=settings.CABINET_OTP_TTL_MINUTES * 60,
        resend_after=settings.CABINET_OTP_RESEND_SECONDS,
    )


@router.post("/otp/verify", response_model=AppOtpVerifyResponse)
async def verify_otp(
    payload: AppOtpVerifyRequest,
    _=Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AppOtpVerifyResponse:
    """Шаг 2: проверить код, создать/найти пользователя и подписку, вернуть identity."""
    email = payload.email.strip().lower()
    if not validate_email(email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid email")

    device_id = (payload.device_id or "").strip() or None

    # 400 invalid_code / 410 code_expired — единый контракт с кабинетом.
    await _verify_registration_code(db, email, payload.code)

    user = await get_user_by_email(db, email)
    if user is None:
        # Passwordless: задаём заведомо непригодный для входа пароль.
        user = await create_web_user(
            db,
            email=email,
            password_hash=hash_password(secrets.token_urlsafe(32)),
        )
        user.email_verified = True
        await db.commit()

        # Анти-абуз по устройству: если device_id уже привязывался к какой-либо
        # подписке ранее (таблица device_links не чистится), новый триал не
        # выдаём — создаём disabled-заглушку с used_trial_failed=True и без
        # аккаунта RemnaWave (VPN-доступа нет). Логика идентична device-deeplink
        # флоу в handlers/start.py.
        existing_link = await get_device_link(db, device_id) if device_id else None
        if existing_link is not None:
            logger.info(
                "🚫 OTP: устройство %s уже использовалось (подписка %s) — "
                "триал не выдаётся для %s",
                device_id,
                existing_link.subscription_id,
                email,
            )
            await create_trial_denied_subscription(db, user.id)
        else:
            try:
                subscription = await create_trial_subscription(db, user.id)
            except Exception:
                logger.exception("Не удалось создать триальную подписку для %s", email)
                subscription = None
            if subscription is not None:
                await _provision_remnawave(db, subscription)
                # Фиксируем устройство за выданной подпиской, чтобы повторная
                # OTP-регистрация с этого же устройства уже получила отказ.
                if device_id:
                    try:
                        await create_device_link(db, subscription.id, device_id)
                    except Exception:
                        logger.exception(
                            "Не удалось привязать устройство %s к подписке %s",
                            device_id,
                            subscription.id,
                        )
    else:
        # Существующий пользователь — ленивый до-провижн RemnaWave при отсутствии.
        await ensure_remnawave_account(db, user)

    # Перечитываем с eager-подпиской, чтобы вернуть свежие remnawave_uuid/URL.
    full = await get_user_by_id(db, user.id) or user
    subscription = getattr(full, "subscription", None)

    return AppOtpVerifyResponse(
        user_id=full.id,
        email=full.email,
        remnawave_uuid=full.remnawave_uuid or "",
        subscription_url=getattr(subscription, "subscription_url", None) if subscription else None,
        subscription_end_date=(
            subscription.end_date.isoformat()
            if subscription is not None and subscription.end_date is not None
            else None
        ),
    )


@router.post("/cabinet-token", response_model=AppCabinetTokenResponse)
async def issue_cabinet_token(
    payload: AppCabinetTokenRequest,
    _=Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AppCabinetTokenResponse:
    """Выдать cabinet-JWT для уже известного боту пользователя по его user_id.

    teleVpn вызывает этот эндпоинт от имени уже авторизованного (своим JWT)
    пользователя, чтобы открыть личный кабинет (/cabinet/*) без повторного
    логина по email/паролю. Токен идентичен тому, что выдают /cabinet/auth/*.
    """
    user = await get_user_by_id(db, payload.user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")

    token = cabinet_auth_service.issue_token(user)
    return AppCabinetTokenResponse(
        token=token,
        expires_in=settings.CABINET_JWT_TTL_HOURS * 3600,
    )
