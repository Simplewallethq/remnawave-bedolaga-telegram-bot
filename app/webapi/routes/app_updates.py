"""Публичный манифест автообновления десктопного приложения.

Эндпоинт без авторизации: апдейтер проверяет обновления до логина и когда
токен протух. Отдаём только то, что и так лежит в публичном релизе (версия,
ссылка, хеш), поэтому прятать его за токеном смысла нет.

Данные берутся из настроек (категория «Обновления приложения» в админке),
запросов в БД здесь нет — ответ кешируется на стороне клиента/прокси.
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.config import settings

from ..schemas.app_updates import AppUpdateResponse

logger = logging.getLogger(__name__)

router = APIRouter()

_CACHE_MAX_AGE = 300

# Ведущая числовая часть версии: "1.4.2-beta" → (1, 4, 2). Суффиксы
# предрелизов игнорируем — для канала обновлений их не используем.
_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")


def _parse_version(value: Optional[str]) -> Optional[Tuple[int, ...]]:
    if not value:
        return None
    match = _VERSION_RE.match(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def _is_older(left: Tuple[int, ...], right: Tuple[int, ...]) -> bool:
    length = max(len(left), len(right))
    padded_left = left + (0,) * (length - len(left))
    padded_right = right + (0,) * (length - len(right))
    return padded_left < padded_right


@router.get(
    "/update",
    response_model=AppUpdateResponse,
    summary="Манифест обновления приложения",
    responses={404: {"description": "Канал обновлений для платформы не настроен"}},
)
async def get_app_update(
    response: Response,
    platform: str = Query("windows", description="Платформа приложения (пока только windows)"),
    current: Optional[str] = Query(
        None,
        description="Текущая версия клиента, напр. 1.4.2. Если передана — сервер сам посчитает updateAvailable/mandatory",
    ),
) -> AppUpdateResponse:
    manifest = settings.get_app_update_manifest(platform)
    if not manifest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Update channel is not configured for this platform",
        )

    update_available: Optional[bool] = None
    mandatory: Optional[bool] = None

    current_version = _parse_version(current)
    latest_version = _parse_version(manifest["version"])
    if current_version and latest_version:
        update_available = _is_older(current_version, latest_version)
        min_version = _parse_version(manifest["min_supported_version"])
        mandatory = bool(
            update_available and min_version and _is_older(current_version, min_version)
        )

    response.headers["Cache-Control"] = f"public, max-age={_CACHE_MAX_AGE}"

    return AppUpdateResponse(
        platform=manifest["platform"],
        version=manifest["version"],
        url=manifest["url"],
        mirrors=manifest["mirrors"],
        sha256=manifest["sha256"],
        minSupportedVersion=manifest["min_supported_version"],
        notes=manifest["notes"],
        updateAvailable=update_available,
        mandatory=mandatory,
    )
