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
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_ACCESS_DAYS", 3, raising=False)

    regular = _buttons(inline.get_new_main_menu_keyboard(balance_rub=0, language="ru"))
    assert ("🎁 3 дня бесплатно", "trial_activate") in regular
    assert all(cb != "paid_trial_offer" for _, cb in regular)

    offered = _buttons(inline.get_new_main_menu_keyboard(balance_rub=0, language="ru", paid_trial_offer=True))
    assert all(cb != "trial_activate" for _, cb in offered)
    assert ("🎁 Активируй 3 дня доступа", "paid_trial_offer") in offered

    # Кнопка не появляется, если подписка уже была.
    used = _buttons(
        inline.get_new_main_menu_keyboard(balance_rub=0, language="ru", trial_used=True, paid_trial_offer=True)
    )
    assert all(cb not in ("paid_trial_offer", "trial_activate") for _, cb in used)


def _offer_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_ACCESS_DAYS", 3, raising=False)
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_PRICE_KOPEKS", 100, raising=False)
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_RENEWAL_PERIOD_DAYS", 30, raising=False)


def _capture_screen(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    captured: Dict[str, Any] = {}

    async def fake_send(message_or_callback: Any, *, is_callback: bool, text: str, keyboard: Any, photo_path: Any) -> None:
        captured["text"] = text
        captured["keyboard"] = keyboard
        captured["is_callback"] = is_callback

    monkeypatch.setattr(offer_handlers, "_send_screen", fake_send)
    return captured


@pytest.mark.anyio
async def test_gate_screen_explains_one_ruble_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    _offer_settings(monkeypatch)
    plan = SimpleNamespace(id=2, code="solo", display_name="Solo", is_active=True)

    async def fake_resolve(db: Any, user: Any) -> Any:
        return plan, 29_000

    monkeypatch.setattr(offer_handlers.trial_paid_offer_service, "resolve_plan", fake_resolve)
    captured = _capture_screen(monkeypatch)

    user = SimpleNamespace(id=42, language="ru")
    assert await offer_handlers.render_paid_trial_offer(object(), user, object(), is_callback=False) is True

    assert "Активируй 3 дня доступа" in captured["text"]
    assert "Доступ бесплатный на 3 дня" in captured["text"]
    assert "оплату в 1 ₽" in captured["text"]
    buttons = _buttons(captured["keyboard"])
    assert buttons[0] == ("✅ Активировать", "paid_trial_offer_terms")
    assert ("🆘 Поддержка", "menu_support") in buttons
    # При /start назад некуда, из главного меню — есть.
    assert all(cb != "main_menu" for _, cb in buttons)

    assert await offer_handlers.render_paid_trial_offer(object(), user, object(), is_callback=True) is True
    assert ("⬅️Назад", "main_menu") in _buttons(captured["keyboard"])


@pytest.mark.anyio
async def test_gate_screen_hidden_without_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve(db: Any, user: Any) -> Any:
        return None

    monkeypatch.setattr(offer_handlers.trial_paid_offer_service, "resolve_plan", fake_resolve)
    user = SimpleNamespace(id=42, language="ru")
    assert await offer_handlers.render_paid_trial_offer(object(), user, object(), is_callback=False) is False


@pytest.mark.anyio
async def test_terms_screen_discloses_recurring_price(monkeypatch: pytest.MonkeyPatch) -> None:
    _offer_settings(monkeypatch)
    monkeypatch.setattr(offer_handlers.trial_paid_offer_service, "is_offer_available", lambda user: True)
    plan = SimpleNamespace(id=2, code="solo", display_name="Solo", is_active=True)

    async def fake_resolve(db: Any, user: Any) -> Any:
        return plan, 29_000

    monkeypatch.setattr(offer_handlers.trial_paid_offer_service, "resolve_plan", fake_resolve)
    captured = _capture_screen(monkeypatch)

    callback = SimpleNamespace(answer=AsyncMock())
    await offer_handlers.show_paid_trial_terms(callback, SimpleNamespace(id=42, language="ru"), object())

    text = captured["text"]
    assert "Активация за 1 ₽" in text
    assert "Первые <b>3 дня — бесплатно</b>" in text
    assert "Solo (290 ₽/мес)" in text
    assert "Отменить можно в любой момент" in text
    buttons = _buttons(captured["keyboard"])
    assert buttons[0] == ("✅ Активировать за 1 ₽", "paid_trial_offer_pay")
    assert ("❔ Как отменить автосписание", "paid_trial_offer_cancel_help") in buttons
    assert buttons[-1][1] == "paid_trial_offer"
    callback.answer.assert_awaited()


@pytest.mark.anyio
async def test_cancel_help_screen_points_to_subscription_management(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_screen(monkeypatch)
    callback = SimpleNamespace(answer=AsyncMock())
    await offer_handlers.show_paid_trial_cancel_help(callback, SimpleNamespace(id=42, language="ru"), object())
    assert "Управление подпиской" in captured["text"]
    buttons = _buttons(captured["keyboard"])
    assert ("🆘 Поддержка", "menu_support") in buttons
    assert buttons[-1][1] == "paid_trial_offer_terms"


@pytest.mark.anyio
async def test_pay_handler_shows_invoice_with_status_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(offer_handlers.trial_paid_offer_service, "is_offer_available", lambda user: True)
    plan = SimpleNamespace(id=2, code="solo", display_name="Solo", is_active=True)

    async def fake_invoice(db: Any, bot: Any, user: Any) -> Dict[str, Any]:
        return {
            "local_payment_id": 15,
            "payment_url": "https://merchant.1payment.com/pay",
            "snapshot": {"price_kopeks": 100, "access_days": 3},
            "plan": plan,
        }

    monkeypatch.setattr(offer_handlers.trial_paid_offer_service, "create_offer_invoice", fake_invoice)
    captured = _capture_screen(monkeypatch)
    remembered = AsyncMock()
    monkeypatch.setattr(offer_handlers, "_remember_invoice_message", remembered)

    callback = SimpleNamespace(bot=None, answer=AsyncMock(), message=SimpleNamespace(chat=SimpleNamespace(id=1), message_id=2))
    await offer_handlers.pay_paid_trial_offer(callback, SimpleNamespace(id=42, language="ru"), object())

    rows = captured["keyboard"].inline_keyboard
    assert rows[0][0].url == "https://merchant.1payment.com/pay"
    buttons = _buttons(captured["keyboard"])
    assert ("📊 Проверить статус", "check_onepayment_15") in buttons
    assert buttons[-1][1] == "paid_trial_offer_terms"
    assert "Оплата — 1 ₽" in captured["text"]
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
    # 1 ₽ за гейт — ещё не платная подписка: офферы после триала должны прийти.
    assert user.has_had_paid_subscription is False
    assert user.has_made_first_topup is False
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

    buyer = _user(balance=32_000)
    result = await tariffs_module.finalize_tariff_purchase(db, buyer, _plan(), 30, 32_000)
    assert result is not None
    subscription, _, was_trial_conversion = result

    assert subscription is existing
    assert was_trial_conversion is True
    assert subscription.is_paid_trial is False
    assert buyer.has_had_paid_subscription is True
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
    renewer = _user(balance=32_000)
    result = await tariffs_module.finalize_tariff_renewal(_Db(), renewer, subscription, _plan(), 30, 32_000)
    assert result is not None
    renewed, _, old_end = result
    assert old_end == end
    assert renewed.end_date == end + timedelta(days=30)
    assert renewed.is_paid_trial is False
    # Первое реальное продление делает пользователя платным.
    assert renewer.has_had_paid_subscription is True
    assert renewer.has_made_first_topup is True
    assert calls["subtracted"] == 32_000
