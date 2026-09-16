"""Профиль бренда одного бота.

Основной бот строит профиль из `settings` при каждом обращении: значения
приходят из .env и из БД (админка правит их на лету), кешировать нельзя.
Обычное зеркало — тот же профиль со своей картинкой. Копикет — запись
`copycat: true` в mirror_bots.yaml; пропущенные поля берутся из основного
бота как фолбэк, кроме канала: чужой канал выдал бы витрину с головой.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

DEFAULT_BRAND_NAME = "Leto"

# Ключи записи зеркала, которые проходят из yaml в профиль.
COPYCAT_CONFIG_KEYS = (
    "copycat",
    "name",
    "privacy_url",
    "terms_url",
    "channel_link",
    "channel_id",
    "support",
    "subscription_domain",
    "referral_terms_url",
    "has_own_app",
    "rays_enabled",
)


@dataclass(frozen=True)
class BrandProfile:
    bot_id: Optional[int]
    name: str
    is_copycat: bool
    privacy_url: str
    terms_url: str
    channel_link: Optional[str]
    channel_id: Optional[str]
    support: Optional[str]
    logo: str
    subscription_domain: Optional[str]
    has_own_app: bool
    rays_enabled: bool
    referral_terms_url: Optional[str]

    @property
    def support_url(self) -> Optional[str]:
        from app.branding.support import support_url

        return support_url(self.support)

    @property
    def support_display(self) -> str:
        from app.branding.support import support_display

        return support_display(self.support)

    @property
    def support_display_html(self) -> str:
        from app.branding.support import support_display_html

        return support_display_html(self.support)

    @property
    def channel_enabled(self) -> bool:
        return bool(self.channel_link)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_subscription_domain(value: Any) -> Optional[str]:
    """`https://sub.example.com/` → `sub.example.com`."""
    domain = _clean(value)
    if not domain:
        return None
    if "://" in domain:
        domain = domain.split("://", 1)[1]
    domain = domain.strip("/").split("/", 1)[0]
    return domain or None


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def primary_profile() -> BrandProfile:
    """Бренд основного бота — из settings, свежий на каждый вызов."""
    from app.config import settings

    name = _clean(settings.VPN_BRAND_NAME) or DEFAULT_BRAND_NAME
    channel_enabled = bool(settings.BRAND_CHANNEL_ENABLED)
    channel_link = _clean(settings.CHANNEL_LINK) if channel_enabled else ""
    channel_id = _clean(settings.CHANNEL_SUB_ID) if channel_enabled else ""

    return BrandProfile(
        bot_id=None,
        name=name,
        is_copycat=name.casefold() != DEFAULT_BRAND_NAME.casefold(),
        privacy_url=_clean(settings.PRIVACY_POLICY_URL),
        terms_url=_clean(settings.TERMS_URL),
        channel_link=channel_link or None,
        channel_id=channel_id or None,
        support=_clean(settings.SUPPORT_USERNAME) or None,
        logo=_clean(settings.LOGO_FILE),
        subscription_domain=None,
        has_own_app=bool(settings.BRAND_HAS_OWN_APP),
        rays_enabled=bool(settings.BRAND_RAYS_ENABLED),
        referral_terms_url=_clean(settings.REFERRAL_TERMS_URL) or None,
    )


def is_copycat_config(config: Mapping[str, Any] | None) -> bool:
    """Запись зеркала описывает копикет: `copycat: true` и есть имя."""
    if not isinstance(config, Mapping):
        return False
    if not _as_bool(config.get("copycat"), False):
        return False
    if not _clean(config.get("name")):
        logger.warning(
            "Mirror bot marked copycat without a name (token %s...) — treated as a plain mirror",
            _clean(config.get("token"))[:10],
        )
        return False
    return True


def mirror_profile_from_config(bot_id: int, config: Mapping[str, Any] | None) -> BrandProfile:
    """Профиль зеркала по его записи из mirror_bots.yaml."""
    base = primary_profile()
    config = config or {}
    logo = _clean(config.get("logo")) or base.logo

    if not is_copycat_config(config):
        return replace(base, bot_id=bot_id, logo=logo)

    return BrandProfile(
        bot_id=bot_id,
        name=_clean(config.get("name")),
        is_copycat=True,
        privacy_url=_clean(config.get("privacy_url")) or base.privacy_url,
        terms_url=_clean(config.get("terms_url")) or base.terms_url,
        channel_link=_clean(config.get("channel_link")) or None,
        channel_id=_clean(config.get("channel_id")) or None,
        support=_clean(config.get("support")) or base.support,
        logo=logo,
        subscription_domain=normalize_subscription_domain(config.get("subscription_domain")),
        has_own_app=_as_bool(config.get("has_own_app"), False),
        rays_enabled=_as_bool(config.get("rays_enabled"), False),
        referral_terms_url=_clean(config.get("referral_terms_url")) or None,
    )
