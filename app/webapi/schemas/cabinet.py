"""Pydantic-схемы запросов личного кабинета (LetoVPNSite)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=6, max_length=128)
    ref: Optional[str] = Field(default=None, description="Реферальный код пригласившего")
    code: Optional[str] = Field(
        default=None, max_length=12,
        description="Email-код подтверждения (обязателен при CABINET_EMAIL_VERIFICATION)",
    )


class RegisterOtpRequest(BaseModel):
    email: str = Field(..., max_length=255)


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=1, max_length=128)


class LoginCodeRequest(BaseModel):
    code: str = Field(..., max_length=128)


class TelegramLoginRequest(BaseModel):
    init_data: str = Field(..., description="Telegram WebApp initData")


class TopupRequest(BaseModel):
    amount: float = Field(..., gt=0, description="Сумма пополнения в рублях")
    method: str = Field(..., description="card | sbp | crypto")


class PurchaseRequest(BaseModel):
    plan_id: str = Field(..., description="Код тарифа (plan.code)")
    months: int = Field(default=1, ge=1, le=12)


class AutoRenewRequest(BaseModel):
    enabled: bool
