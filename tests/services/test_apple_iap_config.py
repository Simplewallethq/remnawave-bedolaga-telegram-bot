"""Тесты маппинга APPLE_IAP_PRODUCTS: смена тарифа через настройку, без правки кода."""

import pytest

from app.config import settings


def test_default_mapping_has_monthly_and_yearly_solo() -> None:
    products = settings.get_apple_iap_products()

    assert products["com.letovpn.ios.subscription.monthly"] == {
        "plan_code": "solo",
        "period_days": 30,
    }
    assert products["com.letovpn.ios.subscription.yearly"] == {
        "plan_code": "solo",
        "period_days": 365,
    }


def test_changing_plan_code_via_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "APPLE_IAP_PRODUCTS",
        '{"com.letovpn.ios.subscription.monthly": {"plan_code": "plus", "period_days": 30}}',
    )

    products = settings.get_apple_iap_products()

    assert products == {
        "com.letovpn.ios.subscription.monthly": {"plan_code": "plus", "period_days": 30}
    }


def test_invalid_json_falls_back_to_empty_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APPLE_IAP_PRODUCTS", "{not valid json")

    assert settings.get_apple_iap_products() == {}


def test_entry_missing_plan_code_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "APPLE_IAP_PRODUCTS",
        '{"com.letovpn.ios.subscription.monthly": {"period_days": 30}, '
        '"com.letovpn.ios.subscription.yearly": {"plan_code": "solo", "period_days": 365}}',
    )

    products = settings.get_apple_iap_products()

    assert "com.letovpn.ios.subscription.monthly" not in products
    assert products["com.letovpn.ios.subscription.yearly"]["plan_code"] == "solo"
