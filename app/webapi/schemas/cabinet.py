"""Pydantic-схемы запросов личного кабинета (LetoVPNSite)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


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
    captcha_token: Optional[str] = Field(default=None, max_length=4096, description="hCaptcha token")


class OtpRequestRequest(BaseModel):
    """Passwordless login-or-register: запрос email-кода (шаг 1)."""

    email: str = Field(..., max_length=255)
    captcha_token: Optional[str] = Field(default=None, max_length=4096, description="hCaptcha token")


class OtpVerifyRequest(BaseModel):
    """Passwordless login-or-register: проверка кода + выдача JWT (шаг 2)."""

    email: str = Field(..., max_length=255)
    code: str = Field(..., max_length=12)
    ref: Optional[str] = Field(default=None, description="Реф-код пригласившего (учитывается только для новых)")


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=1, max_length=128)
    captcha_token: Optional[str] = Field(default=None, max_length=4096, description="hCaptcha token")


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
    method: Optional[str] = Field(
        default=None,
        description="Способ доплаты при нехватке баланса: card | sbp | crypto",
    )


class AutoRenewRequest(BaseModel):
    enabled: bool


class WithdrawalCreateRequest(BaseModel):
    """Заявка на вывод реферальных рублей."""

    amount: float = Field(..., gt=0, description="Сумма вывода в рублях")
    details: str = Field(
        ..., min_length=4, max_length=255,
        description="Реквизиты/контакт для выплаты (TG-юзернейм)",
    )


class ShopRedeemRequest(BaseModel):
    """Обмен лучей на приз. Фронт шлёт camelCase (prizeId)."""

    model_config = ConfigDict(populate_by_name=True)

    prize_id: str = Field(..., alias="prizeId", max_length=64)
    contact: Optional[str] = Field(
        default=None, max_length=255,
        description="TG-контакт (обязателен для физических призов)",
    )
