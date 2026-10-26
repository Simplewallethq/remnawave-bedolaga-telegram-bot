import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, PreCheckoutQuery, TelegramObject

from app.branding.context import current_bot_id
from app.localization.texts import get_texts
from app.utils.bot_registry import payments_disabled

logger = logging.getLogger(__name__)

# Входы в оплату деньгами. Покупка с баланса, отмена автоплатежей и проверка
# уже выставленных счетов остаются доступны.
_BLOCKED_EXACT = frozenset(
    {
        "balance_topup",
        "simple_subscription_other_payment_methods",
        "subscription_platega_autopay_connect",
        "10rub_vpn_deposit_bonus:pay",
    }
)
_BLOCKED_PREFIXES = (
    "topup_",
    "quick_amount_",
    "tariff_pay_partial:",
    "onepay_card_",
    "platega_method_",
    # Leto-воронки витринам не шлются, но старые сообщения могли остаться
    "hot_invoice_offer:claim:",
    "cold_solo_offer:claim:",
    "expired_subscription_offer:claim:",
    "legacy_pro_offer:claim:",
)
_ALLOWED_SIMPLE = frozenset(
    {
        "simple_subscription_purchase",
        "simple_subscription_confirm_purchase",
        "simple_subscription_pay_with_balance",
        "simple_subscription_pay_with_balance_disabled",
    }
)


def _disabled_text(language: str | None) -> str:
    return get_texts(language or "ru").t(
        "PAYMENTS_DISABLED_IN_BOT",
        "Оплата в этом боте недоступна.",
    )


async def reject_payment_if_disabled(callback: CallbackQuery, language: str | None) -> bool:
    """Для мест, где счёт выставляется не по кнопке оплаты, а следом за покупкой
    (доплата за тариф, прямой счёт Platega): фильтр колбэков их не видит."""
    bot_id = current_bot_id()
    if not payments_disabled(bot_id):
        return False
    logger.info("Оплата в боте %s выключена: счёт для %s не выставлен", bot_id, callback.from_user.id)
    await callback.answer(_disabled_text(language), show_alert=True)
    return True


def is_payment_callback(data: str) -> bool:
    if data in _BLOCKED_EXACT or data.startswith(_BLOCKED_PREFIXES):
        return True
    if data.startswith(("pay:", "pay_amount:")):
        return data.split(":", 2)[1] != "balance"
    return data.startswith("simple_subscription_") and data not in _ALLOWED_SIMPLE


class PaymentsDisabledMiddleware(BaseMiddleware):
    """Бот с нулевыми весами роутера не принимает оплату ни одним шлюзом."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        bot_id = current_bot_id()
        if not payments_disabled(bot_id):
            return await handler(event, data)

        message = _disabled_text(getattr(data.get("db_user"), "language", None))

        if isinstance(event, PreCheckoutQuery):
            logger.info("Оплата в боте %s выключена: pre-checkout %s отклонён", bot_id, event.id)
            await event.answer(ok=False, error_message=message)
            return None

        if isinstance(event, CallbackQuery) and is_payment_callback(event.data or ""):
            logger.info(
                "Оплата в боте %s выключена: %s от %s", bot_id, event.data, event.from_user.id
            )
            await event.answer(message, show_alert=True)
            return None

        return await handler(event, data)
