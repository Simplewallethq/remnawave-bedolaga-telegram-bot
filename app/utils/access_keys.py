import html

from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.subscription_utils import get_raw_subscription_link

def format_copyable_code(value: str) -> str:
    """Render a value in Telegram's copyable preformatted block."""
    return f"<pre><code>{html.escape(value, quote=True)}</code></pre>"


async def build_access_key_section(
    db: AsyncSession,
    user,
    texts,
    label: str,
    *,
    copyable: bool = True,
) -> str:
    """Build the user-facing subscription-link section.

    One-time device codes remain supported by the backend for compatibility,
    but are intentionally not rendered in the bot: users should connect via the
    universal subscription link.
    """
    del db, texts
    link = get_raw_subscription_link(getattr(user, "subscription", None))
    if not link:
        return ""

    value = format_copyable_code(link) if copyable else link
    return f"{label}\n{value}"
