"""Админка копикетов — отдельный Telegram-бот для своих: сводка по витринам,
список с метриками, карточка витрины (проверка, вкл/выкл) и добавление новой
по токену и картинке.

Включается токеном COPYCAT_ADMIN_BOT_TOKEN, отвечает только
COPYCAT_ADMIN_USER_IDS (см. app/copycat_admin/runtime.py).
"""

from .runtime import copycat_admin_runtime

__all__ = ["copycat_admin_runtime"]
