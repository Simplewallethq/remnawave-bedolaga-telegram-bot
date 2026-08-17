import html
import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.subscription_utils import get_raw_subscription_link


logger = logging.getLogger(__name__)


def format_copyable_code(value: str) -> str:
    """Render a value in Telegram's copyable preformatted block."""
    return f"<pre><code>{html.escape(value, quote=True)}</code></pre>"


async def build_leto_access_code_block(
    db: AsyncSession,
    user,
    texts,
    *,
    copyable: bool = True,
) -> str:
    """Return the active one-time Leto sign-in code, when available."""
    subscription_id = getattr(getattr(user, "subscription", None), "id", None)
    if not subscription_id:
        return ""

    from app.database.crud.device_binding_code import get_or_create_binding_code

    try:
        record = await get_or_create_binding_code(db, subscription_id)
    except Exception as error:
        logger.error(
            "Unable to create Leto access code for user %s: %s",
            getattr(user, "telegram_id", None),
            error,
        )
        return ""

    ttl_hours = max(1, int((record.expires_at - datetime.utcnow()).total_seconds() // 3600))
    label = texts.t(
        "CONNECT_LETO_CODE_LABEL",
        "Ключ для входа в Leto VPN (действует {ttl_hours} ч)",
    ).format(ttl_hours=ttl_hours)
    value = format_copyable_code(record.code) if copyable else record.code
    return f"\n\n{label}\n{value}"


async def build_access_key_section(
    db: AsyncSession,
    user,
    texts,
    label: str,
    *,
    copyable: bool = True,
) -> str:
    """Build a raw subscription key and Leto access code section."""
    link = get_raw_subscription_link(getattr(user, "subscription", None))
    if not link:
        return ""

    value = format_copyable_code(link) if copyable else link
    return f"{label}\n{value}" + await build_leto_access_code_block(
        db,
        user,
        texts,
        copyable=copyable,
    )
