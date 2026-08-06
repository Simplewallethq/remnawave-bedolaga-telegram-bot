from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class DeviceLinkRequest(BaseModel):
    """Request body for POST /api/devices/{device_id}/link."""
    subscription_id: int


class DeviceLinkResponse(BaseModel):
    """Response for successful device link operations."""
    device_id: str
    subscription_id: int
    linked_at: datetime


class DeviceErrorResponse(BaseModel):
    """Error response with detail message."""
    detail: str


class BindByCodeRequest(BaseModel):
    """Request body for POST /api/devices/bind-by-code."""
    # Код привязки/share-код (16 символов) ЛИБО ссылка на подписку
    # ('https://letovpn.com/sub/-BgpfZQ062Td9Fpk'), поэтому длина свободная.
    code: str = Field(..., min_length=1, max_length=512)
    device_id: str = Field(..., min_length=1, max_length=255)
    # Опционально: сырая строка Google Play Install Referrer
    # ('utm_source=telegram&tg_user_id=123456789'). Пустая строка — no-op.
    install_referrer: Optional[str] = Field(default=None, max_length=512)
    # Опционально: клиент приложения из заголовка x-appName
    # ('letoAndroid/1.2.1'). Пустая строка — no-op.
    app_name: Optional[str] = Field(default=None, max_length=100)
