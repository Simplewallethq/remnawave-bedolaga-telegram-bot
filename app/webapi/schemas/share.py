"""Pydantic-схемы публичного эндпоинта страницы «Поделиться доступом»."""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ShareResolveRequest(BaseModel):
    # Токен в теле POST, а не в пути: RequestLoggingMiddleware логирует
    # method+path, так токен не попадает в логи.
    token: str = Field(..., min_length=16, max_length=128)


class ShareStoreLink(BaseModel):
    kind: str  # google_play | apk | app_store | app_store_ru | direct | custom
    url: str


class SharePlatform(BaseModel):
    method: Literal["leto_code", "incy_link", "happ_link"]
    store: List[ShareStoreLink]


class ShareReferral(BaseModel):
    url: str


class ShareResolveResponse(BaseModel):
    status: Literal["active", "not_found", "paused", "slots_full"]
    shareCode: Optional[str] = None
    happLink: Optional[str] = None
    # incy://add/{обычная ссылка подписки} — для платформ с method=incy_link
    incyLink: Optional[str] = None
    rawLink: Optional[str] = None
    # Обычная ссылка подписки из Remnawave (https://.../sub/{uuid}) — всегда
    # именно она, без подмены на happ-криптоссылку: её можно вставить в любой
    # клиент вручную.
    subLink: Optional[str] = None
    platforms: Optional[Dict[str, SharePlatform]] = None
    referral: Optional[ShareReferral] = None
