import logging
from datetime import datetime
from typing import Optional
from urllib.parse import quote, urlparse, urlunparse
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models import Subscription, User
from app.config import settings

logger = logging.getLogger(__name__)

# ── Incy (iOS/macOS) ─────────────────────────────────────────────────────
# На Apple-платформах вместо Happ используется наше приложение Incy.
# Оно принимает обычную ссылку подписки из Remnawave (без криптоссылки)
# через схему incy://add/{subscription_url}.
INCY_APP_ID = "incy"
INCY_URL_SCHEME = "incy://add/"

_IOS_DEVICE_TYPES = {"ios", "iphone", "ipad", "mac", "macos"}


async def ensure_single_subscription(db: AsyncSession, user_id: int) -> Optional[Subscription]:
    result = await db.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc())
    )
    subscriptions = result.scalars().all()
    
    if len(subscriptions) <= 1:
        return subscriptions[0] if subscriptions else None
    
    latest_subscription = subscriptions[0]
    old_subscriptions = subscriptions[1:]
    
    logger.warning(f"🚨 Обнаружено {len(subscriptions)} подписок у пользователя {user_id}. Удаляем {len(old_subscriptions)} старых.")
    
    for old_sub in old_subscriptions:
        await db.delete(old_sub)
        logger.info(f"🗑️ Удалена подписка ID {old_sub.id} от {old_sub.created_at}")
    
    await db.commit()
    await db.refresh(latest_subscription)
    
    logger.info(f"✅ Оставлена подписка ID {latest_subscription.id} от {latest_subscription.created_at}")
    return latest_subscription


async def update_or_create_subscription(
    db: AsyncSession,
    user_id: int,
    **subscription_data
) -> Subscription:
    existing_subscription = await ensure_single_subscription(db, user_id)
    
    if existing_subscription:
        for key, value in subscription_data.items():
            if hasattr(existing_subscription, key):
                setattr(existing_subscription, key, value)

        existing_subscription.updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(existing_subscription)

        logger.info(f"🔄 Обновлена существующая подписка ID {existing_subscription.id}")
        return existing_subscription

    else:
        subscription_defaults = dict(subscription_data)
        autopay_enabled = subscription_defaults.pop(
            "autopay_enabled", None
        )
        autopay_days_before = subscription_defaults.pop(
            "autopay_days_before", None
        )

        new_subscription = Subscription(
            user_id=user_id,
            autopay_enabled=(
                settings.is_autopay_enabled_by_default()
                if autopay_enabled is None
                else autopay_enabled
            ),
            autopay_days_before=(
                settings.DEFAULT_AUTOPAY_DAYS_BEFORE
                if autopay_days_before is None
                else autopay_days_before
            ),
            **subscription_defaults
        )
        
        db.add(new_subscription)
        await db.commit()
        await db.refresh(new_subscription)
        
        logger.info(f"🆕 Создана новая подписка ID {new_subscription.id}")
        return new_subscription


async def cleanup_duplicate_subscriptions(db: AsyncSession) -> int:
    result = await db.execute(
        select(Subscription.user_id)
        .group_by(Subscription.user_id)
        .having(func.count(Subscription.id) > 1)
    )
    users_with_duplicates = result.scalars().all()
    
    total_deleted = 0
    
    for user_id in users_with_duplicates:
        subscriptions_result = await db.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc())
        )
        subscriptions = subscriptions_result.scalars().all()
        
        for old_subscription in subscriptions[1:]:
            await db.delete(old_subscription)
            total_deleted += 1
            logger.info(f"🗑️ Удалена дублирующаяся подписка ID {old_subscription.id} пользователя {user_id}")
    
    await db.commit()
    logger.info(f"🧹 Очищено {total_deleted} дублирующихся подписок")

    return total_deleted


def get_display_subscription_link(subscription: Optional[Subscription]) -> Optional[str]:
    if not subscription:
        return None

    base_link = getattr(subscription, "subscription_url", None)

    if settings.is_happ_cryptolink_mode():
        crypto_link = getattr(subscription, "subscription_crypto_link", None)
        return crypto_link or base_link

    return base_link


def apply_redirect_template(target_link: str, template: str) -> str:
    """Подставляет ссылку в шаблон redirect-страницы."""

    encoded_link = quote(target_link, safe="")
    replacements = {
        "{subscription_link}": encoded_link,
        "{link}": encoded_link,
        "{subscription_link_raw}": target_link,
        "{link_raw}": target_link,
    }

    replaced = False
    for placeholder, value in replacements.items():
        if placeholder in template:
            template = template.replace(placeholder, value)
            replaced = True

    if replaced:
        return template

    return f"{template}{encoded_link}"


def get_happ_cryptolink_redirect_link(subscription_link: Optional[str]) -> Optional[str]:
    if not subscription_link:
        return None

    template = settings.get_happ_cryptolink_redirect_template()
    if not template:
        return None

    return apply_redirect_template(subscription_link, template)


def is_incy_app(app: Optional[dict]) -> bool:
    """Приложение из app-config.json — это Incy?"""

    if not isinstance(app, dict):
        return False

    if str(app.get("id") or "").strip().lower() == INCY_APP_ID:
        return True

    return str(app.get("urlScheme") or "").strip().lower().startswith("incy://")


def is_ios_device_type(device_type: Optional[str]) -> bool:
    """True для Apple-платформ, где вместо Happ используется Incy."""

    return str(device_type or "").strip().lower() in _IOS_DEVICE_TYPES


def get_raw_subscription_link(subscription: Optional[Subscription]) -> Optional[str]:
    """Обычная ссылка подписки из Remnawave — без happ-криптоссылки.

    Incy добавляет подписку по обычному URL, поэтому режим happ_cryptolink
    (который подменяет ссылку на зашифрованную) для него не применяется.
    """

    if not subscription:
        return None

    return getattr(subscription, "subscription_url", None)


def build_incy_deep_link(subscription_link: Optional[str]) -> Optional[str]:
    """incy://add/{обычная ссылка подписки}."""

    if not subscription_link:
        return None

    link = str(subscription_link).strip()
    if not link:
        return None

    if link.lower().startswith("incy://"):
        return link

    return f"{INCY_URL_SCHEME}{link}"


def get_incy_connect_link(subscription_link: Optional[str]) -> Optional[str]:
    """Ссылка для кнопки «Подключиться» на iOS.

    Telegram не пропускает кастомные схемы в url-кнопках, поэтому incy://
    заворачивается в redirect-страницу (HAPP_CRYPTOLINK_REDIRECT_TEMPLATE),
    если она настроена.
    """

    deep_link = build_incy_deep_link(subscription_link)
    if not deep_link:
        return None

    template = settings.get_happ_cryptolink_redirect_template()
    if not template:
        return deep_link

    return apply_redirect_template(deep_link, template)


def get_incy_button_url(subscription_link: Optional[str]) -> Optional[str]:
    """URL для inline-кнопки Telegram — только http(s).

    Схему incy:// Telegram в url-кнопках не пропускает, поэтому без настроенной
    redirect-страницы кнопку «Подключиться» не показываем (как и для Happ).
    """

    link = get_incy_connect_link(subscription_link)
    if link and link.lower().startswith(("http://", "https://")):
        return link

    return None


def convert_subscription_link_to_happ_scheme(subscription_link: Optional[str]) -> Optional[str]:
    if not subscription_link:
        return None

    parsed_link = urlparse(subscription_link)

    if parsed_link.scheme.lower() == "happ":
        return subscription_link

    if not parsed_link.scheme:
        return subscription_link

    return urlunparse(parsed_link._replace(scheme="happ"))


def resolve_hwid_device_limit(subscription: Optional[Subscription]) -> Optional[int]:
    """Return a device limit value for RemnaWave payloads when selection is enabled."""

    if subscription is None:
        return None

    if not settings.is_devices_selection_enabled():
        forced_limit = settings.get_disabled_mode_device_limit()
        return forced_limit

    limit = getattr(subscription, "device_limit", None)
    if limit is None or limit <= 0:
        return None

    return limit


def resolve_hwid_device_limit_for_payload(
    subscription: Optional[Subscription],
) -> Optional[int]:
    """Return the device limit that should be sent to RemnaWave APIs.

    When device selection is disabled and no explicit override is configured,
    RemnaWave should continue receiving the subscription's stored limit so the
    external panel stays aligned with the bot configuration.
    """

    resolved_limit = resolve_hwid_device_limit(subscription)

    if resolved_limit is not None:
        return resolved_limit

    if subscription is None:
        return None

    fallback_limit = getattr(subscription, "device_limit", None)
    if fallback_limit is None or fallback_limit <= 0:
        return None

    return fallback_limit


def resolve_simple_subscription_device_limit() -> int:
    """Return the effective device limit for simple subscription flows."""

    if settings.is_devices_selection_enabled():
        return int(getattr(settings, "SIMPLE_SUBSCRIPTION_DEVICE_LIMIT", 0) or 0)

    forced_limit = settings.get_disabled_mode_device_limit()
    if forced_limit is not None:
        return forced_limit

    return int(getattr(settings, "SIMPLE_SUBSCRIPTION_DEVICE_LIMIT", 0) or 0)
