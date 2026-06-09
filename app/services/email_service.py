"""Отправка транзакционных писем кабинета (коды подтверждения).

Провайдер — SendGrid (HTTP API v3, без SDK). При SENDGRID_MOCK=true письмо
не отправляется, код пишется в лог (для локальной разработки и E2E).
"""

import logging

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)

SENDGRID_SEND_URL = "https://api.sendgrid.com/v3/mail/send"


class EmailDeliveryError(Exception):
    pass


def is_email_configured() -> bool:
    if settings.SENDGRID_MOCK:
        return True
    return bool(settings.SENDGRID_API_KEY and settings.SENDGRID_FROM_EMAIL)


async def send_otp_email(to_email: str, code: str) -> None:
    """Шлёт письмо с кодом подтверждения регистрации."""
    if settings.SENDGRID_MOCK:
        logger.warning("[SENDGRID MOCK] OTP email | to=%s code=%s", to_email, code)
        return

    if not settings.SENDGRID_API_KEY or not settings.SENDGRID_FROM_EMAIL:
        raise EmailDeliveryError("SendGrid is not configured")

    ttl = settings.CABINET_OTP_TTL_MINUTES
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {
            "email": settings.SENDGRID_FROM_EMAIL,
            "name": settings.SENDGRID_FROM_NAME,
        },
        "subject": "Код подтверждения LetoVPN",
        "content": [
            {
                "type": "text/html",
                "value": (
                    f"<p>Ваш код подтверждения: <b style=\"font-size:20px\">{code}</b></p>"
                    f"<p>Код действует {ttl} минут. Если вы не запрашивали код — "
                    f"просто проигнорируйте это письмо.</p>"
                ),
            }
        ],
    }

    headers = {
        "Authorization": f"Bearer {settings.SENDGRID_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(SENDGRID_SEND_URL, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error("SendGrid error %s: %s", resp.status, body[:500])
                    raise EmailDeliveryError(f"SendGrid status {resp.status}")
    except aiohttp.ClientError as error:
        logger.error("SendGrid request failed: %s", error)
        raise EmailDeliveryError(str(error)) from error
