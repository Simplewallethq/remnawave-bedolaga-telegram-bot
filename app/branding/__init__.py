"""Бренд витрины: профиль на бота, контекст текущего бренда, хелперы.

Один процесс обслуживает основной бот, обычные зеркала (тот же бренд, своя
картинка) и копикеты (свой бренд целиком). Профиль резолвится по id бота:
в хендлерах — через контекст апдейта, в фоновых отправках — по user.bot_id.
"""

from app.branding.context import (
    brand_for_interaction,
    brand_scope,
    current_bot_id,
    current_brand,
    use_brand_for_user,
)
from app.branding.profile import (
    DEFAULT_BRAND_NAME,
    BrandProfile,
    is_copycat_config,
    mirror_profile_from_config,
    primary_profile,
)

__all__ = [
    "DEFAULT_BRAND_NAME",
    "BrandProfile",
    "brand_for_interaction",
    "brand_scope",
    "current_bot_id",
    "current_brand",
    "is_copycat_config",
    "mirror_profile_from_config",
    "primary_profile",
    "use_brand_for_user",
]
