"""Pydantic-схемы публичного манифеста автообновления приложения."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class AppUpdateResponse(BaseModel):
    platform: Literal["windows"]
    version: str
    # Основная ссылка на установщик; mirrors — запасные (напр. GitHub Releases).
    url: str
    mirrors: List[str] = Field(default_factory=list)
    # SHA-256 установщика в нижнем регистре. Апдейтер обязан сверять хеш
    # перед запуском файла, особенно если качал с зеркала.
    sha256: Optional[str] = None
    minSupportedVersion: Optional[str] = None
    notes: Optional[str] = None
    # Заполняются только если клиент передал свою версию в ?current=
    updateAvailable: Optional[bool] = None
    mandatory: Optional[bool] = None
