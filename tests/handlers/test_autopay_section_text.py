"""Раздел «Автоплатеж»: «не настроен» / «активен, сумма раз в период, следующая дата»."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings  # noqa: E402
import app.handlers.balance.onepayment as onepayment_handlers  # noqa: E402
import app.handlers.balance.platega as platega_handlers  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _enable_onepayment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ONEPAYMENT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_PARTNER_ID", "p", raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_PROJECT_ID", "j", raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_API_KEY", "k", raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_RECURRING_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_RECURRING_DAYS_BEFORE", 1, raising=False)
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_RECURRING_HOURS_BEFORE", 1, raising=False)


def _user(subscription: Optional[Any]) -> SimpleNamespace:
    return SimpleNamespace(id=42, language="ru", subscription=subscription, username=None)


def _subscription(**overrides: Any) -> SimpleNamespace:
    sub = SimpleNamespace(
        plan_id=2,
        plan_period_days=30,
        plan=SimpleNamespace(id=2, display_name="Solo"),
        end_date=datetime(2030, 1, 10, 12, 0, 0),
        is_trial=False,
        is_paid_trial=False,
        autopay_enabled=False,
        autopay_days_before=1,
    )
    for key, value in overrides.items():
        setattr(sub, key, value)
    return sub


def _patch_price(monkeypatch: pytest.MonkeyPatch, price: int = 29_000) -> None:
    async def fake_charge(db: Any, user: Any) -> Optional[Dict[str, Any]]:
        sub = user.subscription
        if sub is None:
            return None
        return {"name": "Solo", "price_kopeks": price, "period_days": sub.plan_period_days}

    monkeypatch.setattr(onepayment_handlers, "resolve_renewal_charge", fake_charge)
    monkeypatch.setattr("app.utils.timezone.format_local_datetime", lambda dt, fmt="", **_: dt.strftime("%d.%m.%Y %H:%M"))


@pytest.mark.anyio
async def test_not_configured_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_onepayment(monkeypatch)
    _patch_price(monkeypatch)
    text = await platega_handlers.build_autopay_menu_text(object(), _user(_subscription()), None, None)
    assert text == (
        "Здесь вы можете настроить автоплатеж чтобы всегда оставаться на связи.\n\n"
        "Текущий автоплатеж: не настроен"
    )


@pytest.mark.anyio
async def test_sbp_binding_shows_amount_period_and_next_date(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_onepayment(monkeypatch)
    _patch_price(monkeypatch)
    binding = SimpleNamespace(status="ACTIVE", is_active=True)
    text = await platega_handlers.build_autopay_menu_text(object(), _user(_subscription()), None, binding)

    assert "Текущий автоплатеж: активен" in text
    assert "Сумма: 290 ₽ раз в месяц" in text
    assert "Следующая дата платежа: 09.01.2030 12:00" in text
    assert text.endswith("за 1 дн. до окончания подписки (за вычетом баланса).")


@pytest.mark.anyio
async def test_paid_trial_binding_charges_hours_before_access_end(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_onepayment(monkeypatch)
    _patch_price(monkeypatch)
    binding = SimpleNamespace(status="ACTIVE", is_active=True)
    sub = _subscription(is_paid_trial=True, end_date=datetime(2030, 1, 4, 12, 0, 0))
    text = await platega_handlers.build_autopay_menu_text(object(), _user(sub), None, binding)

    assert "Сумма: 290 ₽ раз в месяц" in text
    assert "Следующая дата платежа: 04.01.2030 11:00" in text
    assert "за 1 ч. до окончания подписки" in text


@pytest.mark.anyio
async def test_failed_binding_is_not_active_and_keeps_note(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_onepayment(monkeypatch)
    _patch_price(monkeypatch)
    binding = SimpleNamespace(status="FAILED", is_active=False)
    text = await platega_handlers.build_autopay_menu_text(object(), _user(_subscription()), None, binding)
    assert "Текущий автоплатеж: не настроен" in text
    assert "требуется переподключение" in text


@pytest.mark.anyio
async def test_platega_subscription_used_when_no_sbp_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_onepayment(monkeypatch)
    _patch_price(monkeypatch)
    platega = SimpleNamespace(amount_kopeks=50_000, next_charge_at=datetime(2030, 2, 1, 9, 0, 0))
    text = await platega_handlers.build_autopay_menu_text(object(), _user(_subscription()), platega, None)
    assert "Текущий автоплатеж: активен" in text
    assert "Сумма: 500 ₽ раз в месяц" in text
    assert "Следующая дата платежа: 01.02.2030 09:00" in text
    assert "за вычетом баланса" not in text


@pytest.mark.anyio
async def test_balance_autopay_used_as_last_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_onepayment(monkeypatch)
    _patch_price(monkeypatch)
    monkeypatch.setattr(settings, "ENABLE_AUTOPAY", True, raising=False)
    sub = _subscription(autopay_enabled=True, autopay_days_before=2, plan_period_days=90)
    text = await platega_handlers.build_autopay_menu_text(object(), _user(sub), None, None)
    assert "Сумма: 290 ₽ раз в 3 месяца" in text
    assert "Следующая дата платежа: 08.01.2030 12:00" in text
    assert "за 2 дн. до окончания подписки" in text
