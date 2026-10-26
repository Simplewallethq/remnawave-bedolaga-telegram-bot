from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, PreCheckoutQuery

from app.branding.context import brand_scope
from app.middlewares.payments_disabled import (
    PaymentsDisabledMiddleware,
    is_payment_callback,
    reject_payment_if_disabled,
)
from app.utils import bot_registry

BOT_ID = 990001


@pytest.fixture(autouse=True)
def _weights():
    yield
    bot_registry.set_payment_weights(BOT_ID, None)


def _callback(data: str) -> MagicMock:
    event = MagicMock(spec=CallbackQuery)
    event.data = data
    event.from_user = SimpleNamespace(id=1)
    event.answer = AsyncMock()
    return event


@pytest.mark.parametrize(
    "data",
    [
        "balance_topup",
        "topup_platega",
        "topup_amount|auto|29000",
        "quick_amount_29000",
        "pay:wata:30",
        "pay_amount:stars:290.0",
        "simple_subscription_wata",
        "simple_subscription_other_payment_methods",
        "subscription_platega_autopay_connect",
        "tariff_pay_partial:solo:30",
        "onepay_card_12",
    ],
)
def test_payment_callbacks_are_blocked(data):
    assert is_payment_callback(data)


@pytest.mark.parametrize(
    "data",
    [
        "main_menu",
        "pay:balance:30",
        "pay_amount:balance:290.0",
        "simple_subscription_pay_with_balance",
        "simple_subscription_confirm_purchase",
        "subscription_platega_autopay",
        "subscription_platega_autopay_cancel",
        "check_platega_5",
        "tariff_buy:solo",
    ],
)
def test_other_callbacks_pass(data):
    assert not is_payment_callback(data)


def test_payments_disabled_only_when_all_weights_zero():
    assert not bot_registry.payments_disabled(BOT_ID)
    bot_registry.set_payment_weights(BOT_ID, {"wata": 1, "platega": 0})
    assert not bot_registry.payments_disabled(BOT_ID)
    bot_registry.set_payment_weights(BOT_ID, {"wata": 0, "platega": 0})
    assert bot_registry.payments_disabled(BOT_ID)
    bot_registry.set_payment_weights(BOT_ID, {})
    assert bot_registry.payments_disabled(BOT_ID)
    assert not bot_registry.payments_disabled(None)


@pytest.mark.anyio
async def test_blocks_payment_callback_in_disabled_bot():
    bot_registry.set_payment_weights(BOT_ID, {"platega": 0, "wata": 0})
    handler = AsyncMock()
    event = _callback("topup_amount|auto|29000")

    with brand_scope(BOT_ID):
        await PaymentsDisabledMiddleware()(handler, event, {})

    handler.assert_not_awaited()
    event.answer.assert_awaited_once()
    assert event.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.anyio
async def test_passes_payment_callback_when_bot_has_gateways():
    bot_registry.set_payment_weights(BOT_ID, {"wata": 1})
    handler = AsyncMock()
    event = _callback("topup_amount|auto|29000")

    with brand_scope(BOT_ID):
        await PaymentsDisabledMiddleware()(handler, event, {})

    handler.assert_awaited_once()
    event.answer.assert_not_awaited()


@pytest.mark.anyio
async def test_rejects_pre_checkout_in_disabled_bot():
    bot_registry.set_payment_weights(BOT_ID, {})
    handler = AsyncMock()
    event = MagicMock(spec=PreCheckoutQuery)
    event.id = "q1"
    event.answer = AsyncMock()

    with brand_scope(BOT_ID):
        await PaymentsDisabledMiddleware()(handler, event, {})

    handler.assert_not_awaited()
    assert event.answer.await_args.kwargs["ok"] is False


@pytest.mark.anyio
async def test_reject_payment_if_disabled_answers_only_in_disabled_bot():
    event = _callback("tariff_buy:solo:30")

    with brand_scope(BOT_ID):
        assert await reject_payment_if_disabled(event, "ru") is False
        event.answer.assert_not_awaited()

        bot_registry.set_payment_weights(BOT_ID, {"platega": 0, "wata": 0})
        assert await reject_payment_if_disabled(event, "ru") is True

    assert event.answer.await_args.kwargs["show_alert"] is True
