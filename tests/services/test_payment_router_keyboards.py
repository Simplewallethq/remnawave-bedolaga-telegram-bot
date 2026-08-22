"""Единая кнопка «Оплатить» в клавиатурах и клампинг суммы для метода auto."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings  # noqa: E402
from app.keyboards.inline import (  # noqa: E402
    get_balance_topup_payment_methods_keyboard,
    get_payment_methods_keyboard,
)
from app.services.tariff_partial_payment_service import (  # noqa: E402
    clamp_invoice_amount,
    get_provider_min_kopeks,
)


def _callbacks(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


@pytest.fixture
def routed_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_ENABLED", True, raising=False)
    monkeypatch.setattr(
        settings,
        "PAYMENT_ROUTER_SURFACES",
        "balance_topup,subscription_cart,tariff_partial,simple_pay,cabinet,miniapp",
        raising=False,
    )
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_PLATEGA", 1, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_WATA", 1, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_YOOKASSA", 1, raising=False)

    monkeypatch.setattr(settings, "PLATEGA_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_UNIVERSAL_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MERCHANT_ID", "m", raising=False)
    monkeypatch.setattr(settings, "PLATEGA_SECRET", "s", raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MIN_AMOUNT_KOPEKS", 10_000, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MAX_AMOUNT_KOPEKS", 5_000_000, raising=False)

    monkeypatch.setattr(settings, "WATA_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "WATA_ACCESS_TOKEN", "t", raising=False)
    monkeypatch.setattr(settings, "WATA_TERMINAL_PUBLIC_ID", "p", raising=False)
    monkeypatch.setattr(settings, "WATA_MIN_AMOUNT_KOPEKS", 10_000, raising=False)
    monkeypatch.setattr(settings, "WATA_MAX_AMOUNT_KOPEKS", 5_000_000, raising=False)

    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "shop", raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "key", raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_SEND_RECEIPT", False, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_MIN_AMOUNT_KOPEKS", 5_000, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_MAX_AMOUNT_KOPEKS", 5_000_000, raising=False)


def test_balance_keyboard_shows_single_auto_button(routed_settings) -> None:
    markup = get_balance_topup_payment_methods_keyboard(50_000, "ru")
    callbacks = _callbacks(markup)
    assert "topup_amount|auto|50000" in callbacks
    assert "topup_amount|platega_universal|50000" not in callbacks


def test_balance_keyboard_falls_back_when_router_disabled(
    routed_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Выключенный рубильник обязан возвращать прежний экран без рестарта."""
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_ENABLED", False, raising=False)
    callbacks = _callbacks(get_balance_topup_payment_methods_keyboard(50_000, "ru"))
    assert "topup_amount|auto|50000" not in callbacks
    assert "topup_amount|platega_universal|50000" in callbacks


def test_payment_methods_keyboard_suppresses_routed_gateways(
    routed_settings,
) -> None:
    callbacks = _callbacks(get_payment_methods_keyboard(50_000, "ru"))
    assert "topup_amount|auto|50000" in callbacks
    for suppressed in ("yookassa", "wata", "platega", "platega_universal"):
        assert f"topup_amount|{suppressed}|50000" not in callbacks


def test_payment_methods_keyboard_keeps_other_methods(
    routed_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_STARS_ENABLED", True, raising=False)
    callbacks = _callbacks(get_payment_methods_keyboard(50_000, "ru"))
    assert "topup_amount|auto|50000" in callbacks
    assert "topup_amount|stars|50000" in callbacks


def test_auto_minimum_is_max_of_gateway_minimums(routed_settings) -> None:
    """Счёт обязан быть оплатим любым шлюзом, который может выпасть."""
    assert get_provider_min_kopeks("auto") == 10_000
    assert clamp_invoice_amount("auto", 3_000) == 10_000
    assert clamp_invoice_amount("auto", 25_000) == 25_000


def test_auto_minimum_is_zero_without_gateways(
    routed_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_PLATEGA", 0, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_WATA", 0, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_YOOKASSA", 0, raising=False)
    assert get_provider_min_kopeks("auto") == 0
