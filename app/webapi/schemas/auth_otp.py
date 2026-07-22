"""Схемы внутреннего passwordless OTP-API для приложения teleVpn.

teleVpn проксирует свой OTP-флоу в бота через X-API-Key (как /api/devices).
Бот владеет пользователем и подпиской; teleVpn выдаёт свой JWT поверх ответа.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AppOtpRequest(BaseModel):
    email: str = Field(..., max_length=255)


class AppOtpVerifyRequest(BaseModel):
    email: str = Field(..., max_length=255)
    code: str = Field(..., max_length=16)
    # Опционально: идентификатор устройства из приложения. Если передан и это
    # устройство уже получало триал ранее — новый триал не выдаётся (анти-абуз
    # по аналогии с device-deeplink флоу). Старые клиенты без device_id
    # сохраняют прежнее поведение.
    device_id: Optional[str] = Field(default=None, max_length=255)
    # Опционально: сырая строка Google Play Install Referrer
    # ('utm_source=telegram&tg_user_id=123456789'). Пустая строка — no-op.
    install_referrer: Optional[str] = Field(default=None, max_length=512)
    # Опционально: клиент приложения из заголовка x-appName
    # ('letoAndroid/1.2.1'). Пустая строка — no-op.
    app_name: Optional[str] = Field(default=None, max_length=100)


class AppOtpRequestResponse(BaseModel):
    ok: bool = True
    ttl_seconds: int
    resend_after: int


class AppOtpVerifyResponse(BaseModel):
    """Идентификатор пользователя бота, по которому teleVpn строит своё «зеркало»."""

    user_id: int
    email: str
    remnawave_uuid: str = ""
    subscription_url: Optional[str] = None
    subscription_end_date: Optional[str] = None


class AppCabinetTokenRequest(BaseModel):
    """Запрос cabinet-токена для уже известного боту пользователя."""

    user_id: int


class AppCabinetTokenResponse(BaseModel):
    """Cabinet-JWT, которым приложение открывает личный кабинет (/cabinet/*)."""

    token: str
    token_type: str = "cabinet"
    expires_in: int
