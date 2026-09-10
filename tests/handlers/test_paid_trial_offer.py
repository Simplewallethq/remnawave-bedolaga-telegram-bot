"""A/B «доступ за 1 ₽»: клавиатура главного меню, экран оффера и покупка с раздельным сроком."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings  # noqa: E402
from app.keyboards import inline  # noqa: E402
import app.handlers.subscription.paid_trial_offer as offer_handlers  # noqa: E402
import app.handlers.subscription.tariffs as tariffs_module  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _buttons(keyboard: Any) -> List[tuple]:
    return [(button.text, button.callback_data) for row in keyboard.inline_keyboard for button in row]


def test_main_menu_shows_paid_offer_instead_of_trial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_ACCESS_DAYS", 1, raising=False)
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_PRICE_KOPEKS", 100, raising=False)

    regular = _buttons(inline.get_new_main_menu_keyboard(balance_rub=0, language="ru"))
    assert ("🎁 3 дня бесплатно", "trial_activate") in regular
    assert all(cb != "paid_trial_offer" for _, cb in regular)

    offered = _buttons(inline.get_new_main_menu_keyboard(balance_rub=0, language="ru", paid_trial_offer=True))
    assert all(cb != "trial_activate" for _, cb in offered)
    text, callback = next(item for item in offered if item[1] == "paid_trial_offer")
    assert "1 дн." in text and "1" in text

    # Кнопка не появляется, если подписка уже была.
    used = _buttons(
        inline.get_new_main_menu_keyboard(balance_rub=0, language="ru", trial_used=True, paid_trial_offer=True)
    )
    assert all(cb not in ("paid_trial_offer", "trial_activate") for _, cb in used)


@pytest.mark.anyio
async def test_offer_screen_lists_price_renewal_and_pay_button(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_ACCESS_DAYS", 1, raising=False)
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_PRICE_KOPEKS", 100, raising=False)
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_RENEWAL_PERIOD_DAYS", 30, raising=False)
    plan = SimpleNamespace(id=2, code="solo", display_name="Solo", is_active=True)

    async def fake_resolve(db: Any, user: Any) -> Any:
        return plan, 32_000

    monkeypatch.setattr(offer_handlers.trial_paid_offer_service, "resolve_plan", fake_resolve)
    captured: Dict[str, Any] = {}

    async def fake_edit(callback: Any, caption: str, keyboard: Any, **kwargs: Any) -> None:
        captured["caption"] = caption
        captured["keyboard"] = keyboard

    monkeypatch.setattr(offer_handlers, "edit_or_answer_photo", fake_edit)

    user = SimpleNamespace(id=42, language="ru")
    assert await offer_handlers.render_paid_trial_offer(object(), user, object(), is_callback=True) is True

    assert "Solo" in captured["caption"]
    assert "1 дн." in captured["caption"]
    assert "320" in captured["caption"]
    buttons = _buttons(captured["keyboard"])
    assert any(cb == "paid_trial_offer_pay" for _, cb in buttons)
    assert any(cb == "subscription_tariffs" for _, cb in buttons)


@pytest.mark.anyio
async def test_offer_screen_hidden_without_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve(db: Any, user: Any) -> Any:
        return None

    monkeypatch.setattr(offer_handlers.trial_paid_offer_service, "resolve_plan", fake_resolve)
    user = SimpleNamespace(id=42, language="ru")
    assert await offer_handlers.render_paid_trial_offer(object(), user, object(), is_callback=False) is False


@pytest.mark.anyio
async def test_pay_handler_shows_invoice_with_status_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(offer_handlers.trial_paid_offer_service, "is_offer_available", lambda user: True)
    plan = SimpleNamespace(id=2, code="solo", display_name="Solo", is_active=True)

    async def fake_invoice(db: Any, bot: Any, user: Any) -> Dict[str, Any]:
        return {
            "local_payment_id": 15,
            "payment_url": "https://merchant.1payment.com/pay",
            "snapshot": {"price_kopeks": 100, "access_days": 1},
            "plan": plan,
        }

    monkeypatch.setattr(offer_handlers.trial_paid_offer_service, "create_offer_invoice", fake_invoice)
    captured: Dict[str, Any] = {}

    async def fake_edit(callback: Any, caption: str, keyboard: Any, **kwargs: Any) -> None:
        captured["caption"] = caption
        captured["keyboard"] = keyboard

    remembered = AsyncMock()
    monkeypatch.setattr(offer_handlers, "edit_or_answer_photo", fake_edit)
    monkeypatch.setattr(offer_handlers, "_remember_invoice_message", remembered)

    callback = SimpleNamespace(bot=None, answer=AsyncMock(), message=SimpleNamespace(chat=SimpleNamespace(id=1), message_id=2))
    user = SimpleNamespace(id=42, language="ru")
    await offer_handlers.pay_paid_trial_offer(callback, user, object())

    buttons = captured["keyboard"].inline_keyboard
    assert buttons[0][0].url == "https://merchant.1payment.com/pay"
    assert ("📊 Проверить статус", "check_onepayment_15") in _buttons(captured["keyboard"])
    assert "Автоплатеж" in captured["caption"]
    remembered.assert_awaited_once()
    callback.answer.assert_awaited()


@pytest.mark.anyio
async def test_pay_handler_refuses_when_offer_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(offer_handlers.trial_paid_offer_service, "is_offer_available", lambda user: False)
    invoice = AsyncMock()
    monkeypatch.setattr(offer_handlers.trial_paid_offer_service, "create_offer_invoice", invoice)
    callback = SimpleNamespace(bot=None, answer=AsyncMock())
    await offer_handlers.pay_paid_trial_offer(callback, SimpleNamespace(id=42, language="ru"), object())
    invoice.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get("show_alert") is True


# ------------------------------------------------- finalize_tariff_purchase


class _Db:
    def __init__(self, existing: Any = None) -> None:
        self.existing = existing
        self.added: List[Any] = []
        self.commits = 0

    async def execute(self, statement: Any) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: self.existing)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def refresh(self, *_: Any, **__: Any) -> None:
        return None


def _patch_purchase(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    calls: Dict[str, Any] = {}

    async def fake_subtract(db: Any, user: Any, amount: int, **kwargs: Any) -> bool:
        calls["subtracted"] = amount
        user.balance_kopeks -= amount
        return True

    async def fake_squads(db: Any) -> List[str]:
        return ["squad-1"]

    async def fake_transaction(db: Any, **kwargs: Any) -> SimpleNamespace:
        calls["transaction"] = kwargs
        return SimpleNamespace(id=777, **kwargs)

    async def fake_rays(*_: Any, **__: Any) -> None:
        return None

    class FakeSubscriptionService:
        async def create_remnawave_user(self, db: Any, subscription: Any) -> None:
            calls["remnawave"] = subscription

    monkeypatch.setattr(tariffs_module, "subtract_user_balance", fake_subtract)
    monkeypatch.setattr(tariffs_module, "_all_active_server_uuids", fake_squads)
    monkeypatch.setattr(tariffs_module, "create_transaction", fake_transaction)
    monkeypatch.setattr(tariffs_module, "SubscriptionService", FakeSubscriptionService)
    monkeypatch.setattr("app.services.rays_service.award_rays_for_referral_purchase", fake_rays, raising=False)
    monkeypatch.setattr(settings, "DEFAULT_AUTOPAY_DAYS_BEFORE", 1, raising=False)
    return calls


def _plan() -> SimpleNamespace:
    return SimpleNamespace(id=2, code="solo", display_name="Solo", traffic_limit_gb=0, device_limit=1)


def _user(balance: int = 100) -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        language="ru",
        balance_kopeks=balance,
        has_made_first_topup=False,
        has_had_paid_subscription=False,
    )


@pytest.mark.anyio
async def test_finalize_purchase_with_access_days_creates_daily_paid_trial(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_purchase(monkeypatch)
    db = _Db()
    user = _user()

    started = datetime.utcnow()
    result = await tariffs_module.finalize_tariff_purchase(
        db, user, _plan(), 30, 100, bot=None, access_days=1, description="Доступ Solo на 1 дн."
    )
    assert result is not None
    subscription, transaction, was_trial_conversion = result

    assert was_trial_conversion is False
    assert subscription.is_trial is False
    assert subscription.is_paid_trial is True
    assert subscription.plan_period_days == 30
    assert subscription.plan_id == 2
    assert timedelta(hours=23, minutes=59) <= subscription.end_date - started <= timedelta(days=1, seconds=5)
    assert calls["subtracted"] == 100
    assert calls["transaction"]["description"] == "Доступ Solo на 1 дн."
    assert user.has_had_paid_subscription is True
    assert db.added == [subscription]


@pytest.mark.anyio
async def test_finalize_purchase_replaces_trial_and_keeps_regular_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_purchase(monkeypatch)
    existing = SimpleNamespace(
        is_trial=True,
        status="active",
        start_date=None,
        end_date=None,
        traffic_limit_gb=10,
        device_limit=2,
        connected_squads=[],
        plan_id=None,
        plan_period_days=None,
        is_paid_trial=False,
    )
    db = _Db(existing=existing)

    result = await tariffs_module.finalize_tariff_purchase(db, _user(balance=32_000), _plan(), 30, 32_000)
    assert result is not None
    subscription, _, was_trial_conversion = result

    assert subscription is existing
    assert was_trial_conversion is True
    assert subscription.is_paid_trial is False
    assert subscription.plan_period_days == 30
    assert timedelta(days=29, hours=23) <= subscription.end_date - datetime.utcnow() <= timedelta(days=30)


@pytest.mark.anyio
async def test_finalize_renewal_clears_paid_trial_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_purchase(monkeypatch)
    end = datetime.utcnow() + timedelta(hours=2)

    class Sub:
        def __init__(self) -> None:
            self.end_date = end
            self.status = "active"
            self.plan_period_days = 30
            self.is_paid_trial = True

        def extend_subscription(self, days: int) -> None:
            self.end_date = self.end_date + timedelta(days=days)

    subscription = Sub()
    result = await tariffs_module.finalize_tariff_renewal(_Db(), _user(balance=32_000), subscription, _plan(), 30, 32_000)
    assert result is not None
    renewed, _, old_end = result
    assert old_end == end
    assert renewed.end_date == end + timedelta(days=30)
    assert renewed.is_paid_trial is False
    assert calls["subtracted"] == 32_000
