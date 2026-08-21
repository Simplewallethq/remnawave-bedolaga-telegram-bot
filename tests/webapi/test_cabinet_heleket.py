from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.user_cart_service import user_cart_service
from app.webapi.routes import cabinet as cabinet_module


def _plan(plan_id: int = 7):
    return SimpleNamespace(id=plan_id, code="plus", display_name="Plus")


def _user(*, balance: int, plan_id: int, is_trial: bool, status: str):
    subscription = SimpleNamespace(
        plan_id=plan_id,
        plan_period_days=30,
        is_trial=is_trial,
        status=status,
        end_date=datetime.utcnow() + timedelta(days=20),
    )
    return SimpleNamespace(
        id=42,
        telegram_id=123456,
        language="ru",
        balance_kopeks=balance,
        subscription=subscription,
    )


def test_heleket_cabinet_flag_does_not_enable_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "HELEKET_ENABLED", False)
    monkeypatch.setattr(settings, "HELEKET_CABINET_ENABLED", True)
    monkeypatch.setattr(settings, "HELEKET_MERCHANT_ID", "merchant-id")
    monkeypatch.setattr(settings, "HELEKET_API_KEY", "payment-key")

    assert settings.is_heleket_enabled() is False
    assert settings.is_heleket_cabinet_enabled() is True
    assert settings.is_heleket_service_enabled() is True


@pytest.mark.anyio("asyncio")
async def test_trial_tariff_checkout_creates_heleket_shortfall_invoice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    async def save_cart(user_id, cart_data, ttl=3600):
        captured.update(user_id=user_id, cart=cart_data, ttl=ttl)
        return True

    create_payment = AsyncMock(
        return_value={"paymentUrl": "https://pay.heleket.com/invoice"}
    )
    monkeypatch.setattr(user_cart_service, "save_user_cart", save_cart)
    monkeypatch.setattr(cabinet_module, "_create_topup_payment", create_payment)

    user = _user(balance=5_000, plan_id=1, is_trial=True, status="trial")
    result = await cabinet_module._create_cabinet_tariff_payment(
        db=SimpleNamespace(),
        user=user,
        plan=_plan(),
        period_days=30,
        price_kopeks=29_000,
        base_price_kopeks=29_000,
    )

    assert result["paymentUrl"].startswith("https://pay.heleket.com/")
    assert captured["cart"]["tariff_op"] == "purchase"
    assert captured["cart"]["source"] == "cabinet"
    assert captured["cart"]["partial_payment"]["shortfall_kopeks"] == 24_000
    create_payment.assert_awaited_once()
    assert create_payment.await_args.args[2:] == ("crypto", 24_000)


@pytest.mark.anyio("asyncio")
async def test_current_paid_plan_checkout_saves_renewal_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    async def save_cart(user_id, cart_data, ttl=3600):
        captured.update(user_id=user_id, cart=cart_data, ttl=ttl)
        return True

    create_payment = AsyncMock(
        return_value={"paymentUrl": "https://pay.heleket.com/renew"}
    )
    monkeypatch.setattr(user_cart_service, "save_user_cart", save_cart)
    monkeypatch.setattr(cabinet_module, "_create_topup_payment", create_payment)

    user = _user(balance=0, plan_id=7, is_trial=False, status="active")
    await cabinet_module._create_cabinet_tariff_payment(
        db=SimpleNamespace(),
        user=user,
        plan=_plan(),
        period_days=30,
        price_kopeks=29_000,
        base_price_kopeks=29_000,
    )

    assert captured["cart"]["tariff_op"] == "renew"
    assert "partial_payment" not in captured["cart"]
    assert create_payment.await_args.args[2:] == ("crypto", 29_000)
