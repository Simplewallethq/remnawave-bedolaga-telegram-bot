"""Конструктор VPN-ботов — отдельный Telegram-бот поверх платформы владельцев.

В боте — быстрый старт и мастер создания бота (токен → поддержка → обложка),
всё управление — в мини-аппе (constructor_web/, API в app/constructor/api.py).
Данные и правила — платформы владельцев (app/services/owner_platform_service.py):
аккаунты владельцев те же, что в боте-платформе OWNER_PLATFORM_BOT_ID.

Включается токеном CONSTRUCTOR_BOT_TOKEN (см. app/constructor/runtime.py).
"""

from .runtime import constructor_runtime

__all__ = ["constructor_runtime"]
