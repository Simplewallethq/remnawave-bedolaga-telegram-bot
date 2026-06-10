"""Серверная проверка hCaptcha-токенов (email-вход/регистрация кабинета).

При HCAPTCHA_ENABLED=false проверка пропускается. При сетевой ошибке
siteverify — fail closed: легитимный юзер повторит, бот не пройдёт.
"""

import logging
from typing import Optional

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)

HCAPTCHA_VERIFY_URL = "https://api.hcaptcha.com/siteverify"


async def verify_captcha(token: Optional[str], remote_ip: Optional[str] = None) -> bool:
    if not settings.HCAPTCHA_ENABLED:
        return True
    if not settings.HCAPTCHA_SECRET_KEY:
        logger.warning("HCAPTCHA_ENABLED=true, но HCAPTCHA_SECRET_KEY не задан — капча пропущена")
        return True
    if not token or not token.strip():
        return False

    data = {"secret": settings.HCAPTCHA_SECRET_KEY, "response": token.strip()}
    if remote_ip:
        data["remoteip"] = remote_ip

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(HCAPTCHA_VERIFY_URL, data=data) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error("hCaptcha siteverify error %s: %s", resp.status, body[:500])
                    return False
                result = await resp.json()
    except aiohttp.ClientError as error:
        logger.error("hCaptcha siteverify request failed: %s", error)
        return False

    if not result.get("success"):
        logger.info("hCaptcha rejected token: %s", result.get("error-codes"))
        return False
    return True
