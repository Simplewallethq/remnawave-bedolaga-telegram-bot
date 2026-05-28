"""Unit tests for /api/plans/for-device classification and legacy-catalog build.

These tests avoid a full DB spin-up: they exercise the route's pure helpers
(`_build_legacy_catalog`, `_build_plan_items`) directly and validate the
classification mapping rules against the existing `user_uses_tariffs` helper.
The DB-fronted parts (CRUD lookups) are covered indirectly by the
get_subscription_by_device_id tests already present elsewhere.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("BOT_TOKEN", "test-token")

from app.config import refresh_period_prices, settings  # noqa: E402
from app.handlers.subscription.purchase import user_uses_tariffs  # noqa: E402
from app.webapi.routes.plans import _build_legacy_catalog, _build_plan_items  # noqa: E402
from app.webapi.schemas.plans import (  # noqa: E402
    LegacyCatalog,
    PlanItem,
)


def _make_user(*, created_at: datetime, subscription: SimpleNamespace | None = None):
    """Build the minimal User-like object user_uses_tariffs reads."""
    return SimpleNamespace(created_at=created_at, subscription=subscription)


def _make_sub(*, plan_id=None, is_trial=False):
    return SimpleNamespace(plan_id=plan_id, is_trial=is_trial)


def test_build_legacy_catalog_uses_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The legacy catalog must derive every field from current settings — so
    operators can change PRICE_*/DEFAULT_*/MAX_DEVICES_LIMIT and have the API
    reflect it without a code change."""
    monkeypatch.setattr(settings, "PRICE_30_DAYS", 99000, raising=False)
    monkeypatch.setattr(settings, "PRICE_90_DAYS", 269000, raising=False)
    monkeypatch.setattr(settings, "PRICE_PER_DEVICE", 5500, raising=False)
    monkeypatch.setattr(settings, "DEFAULT_TRAFFIC_LIMIT_GB", 100, raising=False)
    monkeypatch.setattr(settings, "DEFAULT_DEVICE_LIMIT", 1, raising=False)
    monkeypatch.setattr(settings, "MAX_DEVICES_LIMIT", 20, raising=False)
    refresh_period_prices()

    catalog = _build_legacy_catalog()
    assert isinstance(catalog, LegacyCatalog)

    period_days = {p.period_days for p in catalog.periods}
    assert 30 in period_days and 90 in period_days
    p30 = next(p for p in catalog.periods if p.period_days == 30)
    assert p30.price_kopeks == 99000

    # Traffic addons must be the fallback packages (none disabled by default).
    assert any(a.gb == 5 for a in catalog.traffic_addons)
    assert any(a.gb == 0 for a in catalog.traffic_addons), "unlimited entry must be present"

    assert catalog.device_addon.included == 1
    assert catalog.device_addon.max == 20
    assert catalog.device_addon.price_per_extra_kopeks == 5500
    assert catalog.defaults.default_traffic_gb == 100
    assert catalog.defaults.default_device_limit == 1


def test_build_legacy_catalog_skips_zero_priced_periods(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero-priced periods (e.g. PRICE_5_DAYS=0 by default) are operator's way
    to disable a period — they must not leak into the API response."""
    monkeypatch.setattr(settings, "PRICE_5_DAYS", 0, raising=False)
    monkeypatch.setattr(settings, "PRICE_15_DAYS", 0, raising=False)
    refresh_period_prices()

    catalog = _build_legacy_catalog()
    period_days = {p.period_days for p in catalog.periods}
    assert 5 not in period_days
    assert 15 not in period_days


def test_build_plan_items_maps_full_plan_shape() -> None:
    """Spot-check the plan→PlanItem mapping that's shared between /api/plans
    and /api/plans/for-device."""
    price = SimpleNamespace(period_days=30, price_kopeks=27000)
    plan = SimpleNamespace(
        id=2, code="solo", display_name="Solo",
        device_limit=1, traffic_limit_gb=0, traffic_reset_strategy="NO_RESET",
        custom_app_only=False, priority_support=False,
        sort_order=20, is_active=True, description_md="Solo md",
        prices=[price],
    )
    items = _build_plan_items([plan])
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, PlanItem)
    assert item.code == "solo" and item.display_name == "Solo"
    assert item.traffic_limit_gb == 0 and item.traffic_reset_strategy == "NO_RESET"
    assert item.custom_app_only is False
    assert len(item.prices) == 1 and item.prices[0].price_kopeks == 27000


def test_user_uses_tariffs_new_user_after_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user registered after the cutoff is treated as 'new' even when
    TARIFFS_ENABLED=false. Reused by the route to set user_type='new'."""
    monkeypatch.setattr(settings, "TARIFFS_ENABLED", False, raising=False)
    cutoff = settings.get_tariffs_legacy_cutoff()
    assert cutoff is not None
    user = _make_user(created_at=cutoff + timedelta(days=1), subscription=_make_sub())
    assert user_uses_tariffs(user) is True


def test_user_uses_tariffs_legacy_user_before_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user registered before the cutoff with no plan_id stays on legacy.
    The route maps this to user_type='legacy' and attaches the catalog."""
    monkeypatch.setattr(settings, "TARIFFS_ENABLED", False, raising=False)
    cutoff = settings.get_tariffs_legacy_cutoff()
    assert cutoff is not None
    user = _make_user(created_at=cutoff - timedelta(days=1), subscription=_make_sub(plan_id=None))
    assert user_uses_tariffs(user) is False


def test_user_uses_tariffs_legacy_user_with_plan_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once a legacy user buys a tariff (plan_id set), they're pinned to the
    new flow — even if registered before cutoff. Route returns user_type='new'."""
    monkeypatch.setattr(settings, "TARIFFS_ENABLED", False, raising=False)
    cutoff = settings.get_tariffs_legacy_cutoff()
    assert cutoff is not None
    user = _make_user(created_at=cutoff - timedelta(days=1), subscription=_make_sub(plan_id=42))
    assert user_uses_tariffs(user) is True
