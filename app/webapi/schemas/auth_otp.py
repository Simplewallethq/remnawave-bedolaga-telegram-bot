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
